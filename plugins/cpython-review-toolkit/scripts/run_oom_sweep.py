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
* **One subprocess per iteration.** A segfault kills the interpreter, so an
  in-process loop would only ever see the first crash. Isolation also keeps a
  corrupted heap from poisoning later iterations.
* **Exit codes are the signal.** 139 / -11 = SIGSEGV, 134 / -6 = SIGABRT
  (assertion / fatal error), 1 = a clean ``MemoryError`` (the *safe* path),
  0 = the snippet completed before the allocator budget ran out.
* ``faulthandler`` is enabled before arming, and the hooks are removed in a
  ``finally`` so teardown itself does not fault.

The interpreter under test must be a CPython build with ``_testcapi`` available
(a normal ``./configure && make`` checkout has it; a debug or ASan build gives
the best diagnostics). Point ``--python`` at it.

Usage:
    python run_oom_sweep.py --python ~/cpython/python --code 'x = {}.copy()'
    python run_oom_sweep.py --python ~/cpython/python --script repro.py --max-n 400
"""

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

# Exit-code -> classification. Negative values are POSIX signal returns.
SIGSEGV_CODES = frozenset({139, -11})
SIGABRT_CODES = frozenset({134, -6})

# Wrapper executed in the child. It arms the counting allocator, runs the
# payload, and reports a clean MemoryError distinctly from a crash.
_HARNESS_TEMPLATE = """\
import faulthandler
import sys

faulthandler.enable()

import _testcapi

_PAYLOAD = {payload!r}

_testcapi.set_nomemory({start})
try:
    exec(compile(_PAYLOAD, "<oom-payload>", "exec"), {{"__name__": "__main__"}})
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


def classify(returncode: int) -> str:
    """Map a child exit code to an outcome name."""
    if returncode in SIGSEGV_CODES:
        return "segv"
    if returncode in SIGABRT_CODES:
        return "abort"
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
    return outcome in ("segv", "abort") or outcome.startswith("signal_")


def run_one(python: str, payload: str, n: int, *, timeout: float = 30.0) -> dict:
    """Run the payload once with allocation #n (and onward) failing."""
    script = _HARNESS_TEMPLATE.format(payload=payload, start=n)
    try:
        proc = subprocess.run(
            [python, "-c", script],
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
        "outcome": classify(proc.returncode),
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
            [python, "-c", probe], capture_output=True, text=True, timeout=60
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
) -> dict:
    """Densely sweep the failing-allocation index and collect the outcomes.

    ``stop_after`` > 0 stops once that many distinct crashes are found (0 =
    sweep the whole range).
    """
    err = check_interpreter(python)
    if err:
        return {"error": err, "python": python}

    results: list[dict] = []
    crashes: list[dict] = []
    outcome_counts: dict[str, int] = {}

    for n in range(start_n, max_n):
        r = run_one(python, payload, n, timeout=timeout)
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
        "range": {"start": start_n, "stop": max_n},
        "iterations_run": len(results),
        "outcome_counts": outcome_counts,
        "crashes": crashes,
        "first_crash": first_crash,
        "reproduced": bool(crashes),
        "summary": {
            "total_crashes": len(crashes),
            "crash_indices": [c["n"] for c in crashes],
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

    try:
        result = sweep(
            args.python,
            payload,
            max_n=args.max_n,
            start_n=args.start_n,
            timeout=args.timeout,
            stop_after=args.stop_after,
        )
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
