#!/usr/bin/env python3
"""Dense out-of-memory sweep — turn a static candidate into a reproduced crash.

CPython's ``_testcapi.set_nomemory(start, stop)`` installs a counting allocator
across all three pymalloc domains (RAW / MEM / OBJ) that fails every allocation
from the ``start``-th onward. Sweeping ``start`` densely over ``0..N`` exercises
each allocation-failure path in a snippet in turn, which is exactly what drives
the unchecked-allocation, half-constructed-object, and exception-clobber bugs
this toolkit finds statically.

Methodology (learned from real crashes — do not "optimize" these away):

* **Dense sweep.** Try *every* integer in the range, never a sparse sample: a
  crash window is often exactly one allocation wide.
* **One allocation fails per iteration** (``--width 1``, the default). This is
  what makes the sweep index mean "the failure path of allocation n". Arming
  ``set_nomemory(n)`` with no stop -- which this script did until measured
  otherwise -- fails allocation n *and every one after it*, so any payload that
  allocates more than once dies at the first index reached and the sweep reports
  ``n=1`` with ``lost sys.stderr`` no matter where the real defect is. That is a
  false positive that reads exactly like a crash. Measured: a payload reported
  at ``n=1`` unbounded had its real failure at ``n=127`` once bounded.
  ``--width 0`` restores the unbounded behaviour for the rare case where a
  sustained allocation famine is the thing under test.
* **One subprocess per iteration.** A segfault kills the interpreter, so an
  in-process loop would only ever see the first crash. Isolation also keeps a
  corrupted heap from poisoning later iterations.
* **Setup runs unarmed.** ``--setup`` code executes *before*
  ``set_nomemory()``, in the same namespace as the payload. Arming first means
  freelist-draining / import / warm-up allocations burn the injection budget,
  so the interesting path is never reached: the ``odictiter_new`` crash
  (``K=1`` → SIGABRT) only reproduces with the setup phase unarmed.
* **Exit codes are the signal.** 139 / -11 = SIGSEGV, 134 / -6 = SIGABRT
  (assertion / fatal error), 1 = a clean ``MemoryError`` (the *safe* path),
  0 = the snippet completed before the allocator budget ran out.
* **Exit 1 is not automatically safe.** A sanitizer-instrumented build reports
  a *fatal* error and exits 1 as well, which would read as the SAFE outcome and
  invert the result. The child's stderr is checked for a sanitizer report
  before an exit code of 1 is believed.
* ``faulthandler`` is enabled before arming, and the hooks are removed in a
  ``finally`` so teardown itself does not fault.

The interpreter under test must be a CPython build with ``_testcapi`` available
(a normal ``./configure && make`` checkout has it; a debug or ASan build gives
the best diagnostics). Point ``--python`` at it.

Usage:
    python run_oom_sweep.py --python ~/cpython/python --code 'x = {}.copy()'
    python run_oom_sweep.py --python ~/cpython/python --script repro.py --max-n 400
    python run_oom_sweep.py --python ~/cpython/python \\
        --setup 'from collections import OrderedDict; od = OrderedDict(a=1)' \\
        --code 'iter(od.items())' --max-n 40
"""

import argparse
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

# Exit-code -> classification. Negative values are POSIX signal returns.
SIGSEGV_CODES = frozenset({139, -11})
SIGABRT_CODES = frozenset({134, -6})

# A sanitizer-instrumented build prints its report to stderr and then exits
# with its own ``exitcode`` (1 by default for ASan/TSan/UBSan). Exit 1 is also
# the harness's "clean MemoryError" code, so without this check a fatal ASan
# report is misread as the *safe* outcome — the result is inverted.
_SANITIZER_ERROR_RE = re.compile(
    r"(?:^|\W)(?:ERROR|WARNING):\s*(\w*Sanitizer)\b|"
    r"\bAddressSanitizer:\s*(?:SEGV|heap-|stack-|global-|attempting)|"
    r"\bSUMMARY:\s*(\w*Sanitizer)\b|"
    r"\bruntime error:\s"
)
# LeakSanitizer fires at exit on *any* leak, including the ones an OOM path
# legitimately strands. It is a real signal but it is not a crash, so it gets
# its own outcome rather than being folded into either bucket.
_LEAK_ONLY_RE = re.compile(r"LeakSanitizer")

# Wrapper executed in the child. It runs the (unarmed) setup, arms the counting
# allocator, runs the payload, and reports a clean MemoryError distinctly from
# a crash. Setup and payload share one namespace so setup can build the objects
# the payload exercises.
_HARNESS_TEMPLATE = """\
import faulthandler
import sys

faulthandler.enable()

import _testcapi

_SETUP = {setup!r}
_PAYLOAD = {payload!r}
_NS = {{"__name__": "__main__"}}

# Compile both BEFORE arming. Compilation allocates heavily; leaving it inside
# the armed window burns the first few indices on the compiler and can fault
# there instead of in the payload.
_SETUP_CODE = compile(_SETUP, "<oom-setup>", "exec") if _SETUP else None
_PAYLOAD_CODE = compile(_PAYLOAD, "<oom-payload>", "exec")

# Setup runs BEFORE arming: warm-up/freelist-draining allocations here must not
# consume the injection budget, or the payload never reaches the failure path.
if _SETUP_CODE is not None:
    exec(_SETUP_CODE, _NS)

_testcapi.set_nomemory({start}{stop_arg})
try:
    exec(_PAYLOAD_CODE, _NS)
except MemoryError:
    # The allocation failure was handled correctly: this is the SAFE outcome.
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
    sys.exit(1)
except BaseException:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
    # Any other exception is also a non-crash outcome; distinguish it from
    # MemoryError so the caller can see unexpected error types.
    sys.exit(2)
finally:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
sys.exit(0)
"""


def detect_sanitizer_report(stderr: str) -> str | None:
    """Return ``"sanitizer_error"`` / ``"sanitizer_leak"`` for sanitizer output.

    ``None`` when the child's stderr carries no sanitizer report. A non-leak
    report is a *fatal* error regardless of the exit code — an ASan build exits
    1, which the harness would otherwise read as a clean ``MemoryError``.
    """
    if not stderr:
        return None
    if not _SANITIZER_ERROR_RE.search(stderr):
        return None
    # A leak-only report is not a crash: OOM injection strands allocations by
    # construction, so LeakSanitizer would flag every safe path.
    hits = [m.group(0) for m in _SANITIZER_ERROR_RE.finditer(stderr)]
    if all(_LEAK_ONLY_RE.search(h) for h in hits):
        return "sanitizer_leak"
    return "sanitizer_error"


def classify(returncode: int, stderr: str = "") -> str:
    """Map a child exit code (plus its stderr) to an outcome name.

    ``stderr`` is inspected first: a sanitizer report is fatal whatever the
    exit code says, and ASan's default exit code collides with the harness's
    ``MemoryError`` code.
    """
    sanitizer = detect_sanitizer_report(stderr)
    if sanitizer == "sanitizer_error":
        return "sanitizer_error"
    if returncode in SIGSEGV_CODES:
        return "segv"
    if returncode in SIGABRT_CODES:
        return "abort"
    if sanitizer == "sanitizer_leak":
        return "sanitizer_leak"
    if returncode == 0:
        return "completed"
    if returncode == 1:
        return "memory_error"
    if returncode == 2:
        return "other_exception"
    if returncode < 0:
        return f"signal_{-returncode}"
    return f"exit_{returncode}"


def is_crash(outcome: str) -> bool:
    """True if the outcome represents a genuine crash (not a handled error)."""
    return outcome in ("segv", "abort", "sanitizer_error") or outcome.startswith(
        "signal_"
    )


def build_child_script(payload: str, n: int, *, setup: str = "", width: int = 1) -> str:
    """Render the child harness for one sweep iteration.

    ``width`` allocations fail starting at #n. ``width=1`` isolates a single
    allocation's failure path; ``width=0`` omits the stop argument entirely, so
    every allocation from n onward fails.
    """
    stop_arg = "" if width <= 0 else f", {n + width}"
    return _HARNESS_TEMPLATE.format(
        payload=payload, start=n, setup=setup, stop_arg=stop_arg
    )


def run_one(
    python: str,
    payload: str,
    n: int,
    *,
    timeout: float = 30.0,
    setup: str = "",
    width: int = 1,
) -> dict:
    """Run the payload once with ``width`` allocations failing, starting at #n.

    ``setup`` executes unarmed in the payload's namespace beforehand.

    ``width=1`` (the default) fails exactly ONE allocation, which is what makes
    the sweep index mean "the failure path of allocation n". ``width=0`` restores
    the old unbounded behaviour, where every allocation from n onward fails --
    see the module docstring for why that produces false positives.
    """
    script = build_child_script(payload, n, setup=setup, width=width)
    try:
        proc = subprocess.run(
            [python, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"n": n, "outcome": "timeout", "returncode": None, "stderr": ""}
    except OSError as e:
        return {
            "n": n,
            "outcome": "spawn_error",
            "returncode": None,
            "stderr": str(e),
        }
    return {
        "n": n,
        "outcome": classify(proc.returncode, proc.stderr),
        "returncode": proc.returncode,
        # Keep the tail: a faulthandler traceback lands at the end.
        "stderr": proc.stderr[-4000:],
    }


def check_interpreter(python: str) -> str | None:
    """Return an error string if the interpreter can't run the harness."""
    probe = (
        "import _testcapi, sys; "
        "sys.exit(0 if hasattr(_testcapi, 'set_nomemory') else 3)"
    )
    try:
        proc = subprocess.run(
            [python, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"cannot run interpreter {python!r}: {e}"
    if proc.returncode == 3:
        return (
            f"{python!r} has _testcapi but no set_nomemory() — "
            "use a CPython build that provides it"
        )
    if proc.returncode != 0:
        return (
            f"{python!r} cannot import _testcapi (needed for OOM injection). "
            "Use a CPython source build (./configure && make), not a "
            "distro python without the test C extensions. "
            f"stderr: {proc.stderr.strip()[:300]}"
        )
    return None


def sweep(
    python: str,
    payload: str,
    *,
    max_n: int = 200,
    start_n: int = 0,
    timeout: float = 30.0,
    stop_after: int = 0,
    setup: str = "",
    width: int = 1,
) -> dict:
    """Densely sweep the failing-allocation index and collect the outcomes.

    ``stop_after`` > 0 stops once that many distinct crashes are found (0 =
    sweep the whole range). ``setup`` runs unarmed before each iteration's
    payload. ``width`` is how many consecutive allocations fail per iteration
    (1 = isolate one allocation, 0 = unbounded legacy behaviour).
    """
    err = check_interpreter(python)
    if err:
        return {"error": err, "python": python}

    results: list[dict] = []
    crashes: list[dict] = []
    outcome_counts: dict[str, int] = {}

    for n in range(start_n, max_n):
        r = run_one(python, payload, n, timeout=timeout, setup=setup, width=width)
        results.append(r)
        outcome_counts[r["outcome"]] = outcome_counts.get(r["outcome"], 0) + 1
        if is_crash(r["outcome"]):
            crashes.append(r)
            if stop_after and len(crashes) >= stop_after:
                break

    first_crash = crashes[0] if crashes else None
    return {
        "python": python,
        "payload": payload,
        "setup": setup,
        "range": {"start": start_n, "stop": max_n},
        # Recorded because it changes what a result MEANS: width=1 isolates one
        # allocation's failure path, width=0 fails everything from n onward.
        "width": width,
        "iterations_run": len(results),
        "outcome_counts": outcome_counts,
        "crashes": crashes,
        "first_crash": first_crash,
        "reproduced": bool(crashes),
        "summary": {
            "total_crashes": len(crashes),
            "crash_indices": [c["n"] for c in crashes],
            "crash_outcomes": sorted({c["outcome"] for c in crashes}),
            "verdict": (
                "REPRODUCED — allocation failure crashes the interpreter"
                if crashes
                else "no crash in this range (all failures handled cleanly)"
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dense OOM-injection sweep to reproduce allocation-failure crashes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              run_oom_sweep.py --python ./python --code 'import json; json.loads("[1,2]")'
              run_oom_sweep.py --python ./python --script repro.py --max-n 500 --stop-after 1
              run_oom_sweep.py --python ./python \\
                  --setup 'from collections import OrderedDict; od = OrderedDict(a=1)' \\
                  --code 'iter(od.items())' --max-n 40
            """
        ),
    )
    parser.add_argument(
        "--python",
        required=True,
        help="CPython interpreter to test (must provide _testcapi.set_nomemory)",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--code", help="Python snippet to run under OOM injection")
    src.add_argument("--script", help="Path to a Python file to run")
    setup_src = parser.add_mutually_exclusive_group()
    setup_src.add_argument(
        "--setup",
        help=(
            "Python snippet run BEFORE arming set_nomemory, sharing the "
            "payload's namespace. Put warm-up / freelist-draining / import "
            "work here so it does not consume the injection budget."
        ),
    )
    setup_src.add_argument(
        "--setup-script", help="Path to a Python file to run as the setup phase"
    )
    parser.add_argument(
        "--max-n", type=int, default=200, help="sweep 0..max-n (default 200)"
    )
    parser.add_argument(
        "--start-n", type=int, default=0, help="first allocation index (default 0)"
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="per-iteration timeout seconds"
    )
    parser.add_argument(
        "--stop-after",
        type=int,
        default=0,
        help="stop after N crashes (0 = full sweep)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1,
        help=(
            "consecutive allocations to fail per iteration (default 1, which "
            "isolates one allocation's failure path; 0 = unbounded, every "
            "allocation from n onward fails -- see the module docstring)"
        ),
    )
    args = parser.parse_args()

    if args.script:
        try:
            payload = Path(args.script).read_text(encoding="utf-8")
        except OSError as e:
            json.dump({"error": f"cannot read script: {e}"}, sys.stdout, indent=2)
            sys.stdout.write("\n")
            sys.exit(2)
    else:
        payload = args.code

    setup = args.setup or ""
    if args.setup_script:
        try:
            setup = Path(args.setup_script).read_text(encoding="utf-8")
        except OSError as e:
            json.dump({"error": f"cannot read setup script: {e}"}, sys.stdout, indent=2)
            sys.stdout.write("\n")
            sys.exit(2)

    try:
        result = sweep(
            args.python,
            payload,
            max_n=args.max_n,
            start_n=args.start_n,
            timeout=args.timeout,
            stop_after=args.stop_after,
            setup=setup,
            width=args.width,
        )
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
