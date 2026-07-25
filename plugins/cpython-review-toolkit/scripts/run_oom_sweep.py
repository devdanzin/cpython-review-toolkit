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
* **A broken setup must never read as a clean bill of health.** The setup
  ``exec`` used to run outside any ``try``: a raising setup exited 1, which is
  the harness's *clean MemoryError* code, so ``classify()`` reported the SAFE
  outcome at every index and a wholly broken sweep scored perfectly. Measured
  on ``Objects/typeobject.c`` pass 2: four sweeps reported **400/400
  memory_error**, caused entirely by one ``pickle.dumps`` line in the setup;
  deleting that line turned the same four sweeps into **13 aborts and 2
  SIGSEGVs**. Two defences now exist and both are load-bearing: the setup
  ``exec`` (and both ``compile`` calls) are guarded and exit **3**, a distinct
  ``setup_error`` outcome; and ``sweep()`` runs one *unarmed* dry-run of
  setup+payload before the loop and refuses to sweep if either phase raises.
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

# Below this many *real* allocation-failure points, a clean sweep is not
# evidence and must not be phrased as one. Indices past the payload's last
# allocation return ``completed`` and exercise nothing, so `iterations_run` can
# be 400 while the sweep tested 4 failure paths. Chosen from the obj-typeobject
# pass-2 measurement: over-warmed setups produced 4 / 12 / 5 real points behind
# 220-iteration "clean" verdicts, while their de-warmed variants reached
# 11 / 83 / 11. The threshold separates those two populations.
THIN_EVIDENCE_POINTS = 20

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
#
# Everything before arming exits 3 on failure, NOT 1. Exit 1 is the harness's
# clean-MemoryError code, so an unguarded setup failure used to be reported as
# the SAFE outcome at every index -- a broken sweep scoring a perfect clean
# bill of health.
try:
    _SETUP_CODE = compile(_SETUP, "<oom-setup>", "exec") if _SETUP else None
    _PAYLOAD_CODE = compile(_PAYLOAD, "<oom-payload>", "exec")
except BaseException:
    import traceback

    traceback.print_exc()
    sys.stderr.write("OOM-SWEEP: COMPILE FAILED (unarmed) -- sweep is meaningless\\n")
    sys.exit(3)

# Setup runs BEFORE arming: warm-up/freelist-draining allocations here must not
# consume the injection budget, or the payload never reaches the failure path.
if _SETUP_CODE is not None:
    try:
        exec(_SETUP_CODE, _NS)
    except BaseException:
        import traceback

        traceback.print_exc()
        sys.stderr.write("OOM-SWEEP: SETUP FAILED (unarmed) -- sweep is meaningless\\n")
        sys.exit(3)

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
except BaseException as _exc:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
    # Any other exception is also a non-crash outcome; distinguish it from
    # MemoryError so the caller can see unexpected error types.
    #
    # NAME the exception. An allocation failure that surfaces as something
    # other than MemoryError is the exception-clobber bug shape -- CPython
    # replacing a live MemoryError with an unnarrowed PyErr_Format. Reporting
    # a bare "other_exception" hides exactly the finding worth having.
    try:
        sys.stderr.write(
            "OOM-SWEEP-EXC: %s: %s | __context__=%s\\n"
            % (
                type(_exc).__name__,
                _exc,
                type(_exc.__context__).__name__ if _exc.__context__ else None,
            )
        )
    except BaseException:
        pass
    sys.exit(2)
finally:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
sys.exit(0)
"""


# Unarmed rehearsal of setup+payload. No _testcapi, no set_nomemory: it answers
# only "does this snippet run at all?". Distinct exit codes name the phase that
# failed, so a broken sweep is reported as broken instead of as clean.
_DRY_RUN_TEMPLATE = """\
import sys
import traceback

_SETUP = {setup!r}
_PAYLOAD = {payload!r}
_NS = {{"__name__": "__main__"}}

try:
    _SETUP_CODE = compile(_SETUP, "<oom-setup>", "exec") if _SETUP else None
    _PAYLOAD_CODE = compile(_PAYLOAD, "<oom-payload>", "exec")
except BaseException:
    traceback.print_exc()
    sys.stderr.write("OOM-SWEEP DRY-RUN: COMPILE FAILED\\n")
    sys.exit(3)

if _SETUP_CODE is not None:
    try:
        exec(_SETUP_CODE, _NS)
    except BaseException:
        traceback.print_exc()
        sys.stderr.write("OOM-SWEEP DRY-RUN: SETUP FAILED\\n")
        sys.exit(3)

try:
    exec(_PAYLOAD_CODE, _NS)
except BaseException:
    traceback.print_exc()
    sys.stderr.write("OOM-SWEEP DRY-RUN: PAYLOAD FAILED\\n")
    sys.exit(4)
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
    if returncode == 3:
        # The UNARMED pre-payload phase (compile or setup) raised. Nothing was
        # ever injected, so this is not a result about the code under test -- it
        # is a broken harness invocation. Kept distinct from exit 1 precisely
        # because it used to be indistinguishable from a clean MemoryError.
        return "setup_error"
    if returncode < 0:
        return f"signal_{-returncode}"
    return f"exit_{returncode}"


def is_crash(outcome: str) -> bool:
    """True if the outcome represents a genuine crash (not a handled error)."""
    return outcome in ("segv", "abort", "sanitizer_error") or outcome.startswith(
        "signal_"
    )


_PAYLOAD_EXC_RE = re.compile(r"^OOM-SWEEP-EXC: (.+)$", re.MULTILINE)


def extract_payload_exception(stderr: str) -> str | None:
    """Return the payload's non-MemoryError exception, as the child named it.

    An allocation failure that surfaces as something *other* than MemoryError
    is the exception-clobber shape (an unnarrowed ``PyErr_Format`` overwriting a
    live MemoryError). ``other_exception`` alone does not say which, so the
    child names it and this lifts it onto the result.
    """
    if not stderr:
        return None
    m = _PAYLOAD_EXC_RE.search(stderr)
    return m.group(1).strip() if m else None


def is_harness_error(outcome: str) -> bool:
    """True if the outcome says the sweep never tested anything.

    ``setup_error`` means the unarmed pre-payload phase raised, so no allocation
    was ever injected. It is neither a crash nor a clean ``MemoryError``: it is
    a *void* result, and reporting it as either would be a lie.
    """
    return outcome == "setup_error"


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


def build_dry_run_script(payload: str, *, setup: str = "") -> str:
    """Render the UNARMED rehearsal child: no _testcapi, no injection."""
    return _DRY_RUN_TEMPLATE.format(payload=payload, setup=setup)


def dry_run(
    python: str, payload: str, *, setup: str = "", timeout: float = 60.0
) -> dict:
    """Rehearse setup+payload once with NO allocation injection at all.

    Returns ``{"ok": True, ...}`` when both phases run clean. Otherwise ``ok``
    is False and ``phase`` names what broke:

    * ``"setup"``   — the unarmed setup (or a ``compile``) raised. Every sweep
      index would have been reported as the safe ``memory_error`` before this
      check existed; the whole sweep is void.
    * ``"payload"`` — the payload raises even with memory freely available, so
      the sweep would measure the exception path, not an allocation-failure
      path.
    * ``"interpreter"`` — the child died some other way (a signal, a crash
      without any injection). Worth knowing, but it is not an OOM finding.
    """
    script = build_dry_run_script(payload, setup=setup)
    try:
        proc = subprocess.run(
            [python, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "phase": "interpreter",
            "returncode": None,
            "stderr": "dry run timed out",
        }
    except OSError as e:
        return {
            "ok": False,
            "phase": "interpreter",
            "returncode": None,
            "stderr": str(e),
        }

    stderr = proc.stderr[-4000:]
    if proc.returncode == 0:
        return {"ok": True, "phase": None, "returncode": 0, "stderr": stderr}
    phase = {3: "setup", 4: "payload"}.get(proc.returncode, "interpreter")
    return {
        "ok": False,
        "phase": phase,
        "returncode": proc.returncode,
        "stderr": stderr,
    }


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
    result = {
        "n": n,
        "outcome": classify(proc.returncode, proc.stderr),
        "returncode": proc.returncode,
        # Keep the tail: a faulthandler traceback lands at the end.
        "stderr": proc.stderr[-4000:],
    }
    exc = extract_payload_exception(proc.stderr)
    if exc:
        result["exception"] = exc
    return result


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
    run_dry_run: bool = True,
) -> dict:
    """Densely sweep the failing-allocation index and collect the outcomes.

    ``stop_after`` > 0 stops once that many distinct crashes are found (0 =
    sweep the whole range). ``setup`` runs unarmed before each iteration's
    payload. ``width`` is how many consecutive allocations fail per iteration
    (1 = isolate one allocation, 0 = unbounded legacy behaviour).

    ``run_dry_run`` (default True) rehearses setup+payload once with no
    injection before the loop starts. A sweep whose setup raises tests nothing,
    and used to report the safe ``memory_error`` at every index; the rehearsal
    turns that into an immediate, loud ``error``. Turn it off only when the
    payload is *meant* to raise unarmed.
    """
    err = check_interpreter(python)
    if err:
        return {"error": err, "python": python}

    dry: dict | None = None
    if run_dry_run:
        dry = dry_run(python, payload, setup=setup, timeout=max(timeout, 60.0))
        if not dry["ok"]:
            return {
                "error": (
                    f"unarmed dry run failed in the {dry['phase']} phase "
                    f"(exit {dry['returncode']}) — the sweep would have tested "
                    "nothing. Fix the snippet, or pass run_dry_run=False / "
                    "--no-dry-run if the failure is intended."
                ),
                "python": python,
                "payload": payload,
                "setup": setup,
                "dry_run": dry,
            }

    results: list[dict] = []
    crashes: list[dict] = []
    outcome_counts: dict[str, int] = {}
    setup_errors: list[dict] = []
    # Non-MemoryError exceptions the payload raised: the exception-clobber shape.
    payload_exceptions: list[dict] = []

    for n in range(start_n, max_n):
        r = run_one(python, payload, n, timeout=timeout, setup=setup, width=width)
        results.append(r)
        outcome_counts[r["outcome"]] = outcome_counts.get(r["outcome"], 0) + 1
        if is_harness_error(r["outcome"]):
            # The unarmed phase raised: nothing was injected, and it would raise
            # identically at every remaining index. Stop and say so rather than
            # accumulate a range of void results.
            setup_errors.append(r)
            return {
                "error": (
                    f"setup failed at index {n} (exit {r['returncode']}) — the "
                    "unarmed phase raised, so no allocation was injected and "
                    "the sweep tested nothing"
                ),
                "python": python,
                "payload": payload,
                "setup": setup,
                "dry_run": dry,
                "setup_error": r,
            }
        if r.get("exception"):
            payload_exceptions.append({"n": n, "exception": r["exception"]})
        if is_crash(r["outcome"]):
            crashes.append(r)
            if stop_after and len(crashes) >= stop_after:
                break

    first_crash = crashes[0] if crashes else None
    # D-17: the real denominator of a clean sweep is NOT `iterations_run`. Every
    # index past the payload's last allocation returns `completed` and tests
    # nothing, so a 400-iteration sweep over a payload with 4 allocations is 4
    # pieces of evidence. Measured on obj-typeobject pass 2: three regions were
    # certified clean over 4, 12 and 5 real failure points while printing a
    # 220-iteration clean verdict.
    failure_points = len(results) - outcome_counts.get("completed", 0)
    thin = failure_points < THIN_EVIDENCE_POINTS
    return {
        "python": python,
        "payload": payload,
        "setup": setup,
        # Proof that setup+payload actually run: a clean sweep is only
        # meaningful if this says the snippet executed unarmed.
        "dry_run": dry,
        "range": {"start": start_n, "stop": max_n},
        # Recorded because it changes what a result MEANS: width=1 isolates one
        # allocation's failure path, width=0 fails everything from n onward.
        "width": width,
        "iterations_run": len(results),
        # The honest denominator: indices where an allocation actually failed.
        # `iterations_run` counts indices past the payload's last allocation,
        # which exercise nothing. Read this, not `iterations_run`, before
        # calling a sweep clean.
        "allocation_failure_points": failure_points,
        "outcome_counts": outcome_counts,
        "crashes": crashes,
        "first_crash": first_crash,
        # An allocation failure surfacing as a NON-MemoryError exception is the
        # exception-clobber shape, not a clean handle. Surfaced separately
        # because it is not a crash and would otherwise vanish into the
        # `other_exception` count.
        "payload_exceptions": payload_exceptions,
        "reproduced": bool(crashes),
        "summary": {
            "total_crashes": len(crashes),
            "crash_indices": [c["n"] for c in crashes],
            "crash_outcomes": sorted({c["outcome"] for c in crashes}),
            "allocation_failure_points": failure_points,
            "thin_evidence": thin,
            "verdict": (
                "REPRODUCED — allocation failure crashes the interpreter"
                if crashes
                else (
                    "no crash in this range — UNVERIFIED: the dry run was "
                    "skipped, so nothing proves the payload ran at all"
                    if dry is None
                    else (
                        f"clean over N={failure_points} allocation-failure "
                        f"points — TOO THIN TO CERTIFY (want "
                        f"{THIN_EVIDENCE_POINTS}). The payload allocates too "
                        f"little, or the setup warms the paths under test; "
                        f"move warm-up out of --setup and re-run"
                        if thin
                        else (
                            f"no crash over N={failure_points} "
                            f"allocation-failure points (all handled cleanly)"
                        )
                    )
                )
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
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help=(
            "skip the unarmed setup+payload rehearsal. The rehearsal exists "
            "because a raising setup used to exit 1 -- the clean-MemoryError "
            "code -- and score a perfect sweep. Skip it only when the payload "
            "is MEANT to raise without injection."
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
            run_dry_run=not args.no_dry_run,
        )
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        if "error" in result:
            sys.exit(2)
    except Exception as e:  # noqa: BLE001
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
