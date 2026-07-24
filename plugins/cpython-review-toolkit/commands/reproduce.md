---
description: "Turn a static allocation-failure finding into a reproduced crash via dense OOM injection"
argument-hint: "[finding-or-snippet] [--python <cpython-build>]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Reproduce a Crash (dense OOM injection)

Take a *static* candidate — from `uninitialized-dealloc-auditor`, `pyerr-clear-auditor`, `null-safety-scanner`, `error-path-analyzer`, or a `known-issues` `present` verdict — and try to turn it into a **reproduced crash with evidence** on a locally-built CPython.

**Arguments:** "$ARGUMENTS"

## Phase 0: Find a usable interpreter

The harness needs a CPython build providing `_testcapi.set_nomemory`. Check, in order:
1. An explicit `--python <path>` argument.
2. A source checkout's `./python` (e.g. `~/projects/cpython/python`).
3. A build matrix (e.g. `~/projects/python_build_matrix/builds/*/python`) — prefer a **debug** or **ASan** build; they turn silent corruption into loud assertions.

Verify: `<python> -c "import _testcapi; print(hasattr(_testcapi,'set_nomemory'))"`.
If none is available, report that and stop — do not fabricate a result.

## Phase 1: Choose the target

If the argument names a finding (file:line, function, or a scanner finding id), read that code and write the **smallest stdlib-only snippet** that reaches it. If the argument is already a snippet or a script path, use it directly.

State explicitly which line you are trying to reach — Phase 4 depends on it.

## Phase 2: Sweep

Dispatch the **oom-reproducer** agent, or run directly:

```bash
python <plugin_root>/scripts/run_oom_sweep.py --python <build> --code '<snippet>' --max-n 300
```

Dense by default. Use `--stop-after 1` for existence proof, `--start-n` to skip startup allocations, `--script` for a longer payload.

## Phase 3: Confirm and minimize

On a crash: re-run to confirm determinism, then shrink the payload to the minimum that still crashes, and capture the faulthandler traceback and the crash index.

## Phase 4: Report honestly

```markdown
# Reproduction Report

## Target
[static finding + file:line + which agent surfaced it]

## Interpreter
[path, build flavor, version]

## Sweep
- Range: 0..N (dense) | Outcomes: {...}
- **Verdict**: REPRODUCED at index K  /  not reproduced in this range

## Evidence
[exit code + faulthandler traceback]

## Assessment
[Did the payload reach the flagged line? What is proven / not proven?]
```

**A negative result is a real result**: "every allocation failure on this path was handled cleanly" must be reported plainly, and does not by itself refute the static finding (the payload may not have reached the line).

## Phase 5: Record

For a reproduced crash, add/update the record in the `cpython-review-findings` repo — `repro.py`, `evidence.txt`, status `reproduced`, the crash index, the interpreter build, and `found_by` (the static agent that predicted it). Then regenerate its index.

## Usage

```
/cpython-review-toolkit:reproduce Objects/templateobject.c:225
/cpython-review-toolkit:reproduce 'import json; json.loads("[1,2,3]")' --python ~/projects/cpython/python
```
