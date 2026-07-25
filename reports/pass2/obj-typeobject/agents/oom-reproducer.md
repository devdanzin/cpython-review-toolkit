# oom-reproducer — pass 2, Phase 2E (dynamic verification)

**Slice** `obj-typeobject` · **Target** `/home/danzin/projects/cpython` @ `4f3be1b5777`,
`Objects/typeobject.c` (byte-identical to the build-matrix ref `a1d580430c8` — no diff caveat)
**Builds** `~/projects/python_build_matrix/builds/{debug,release}-{gil,ft}-nojit`, all 3.16.0a0, all with
`_testcapi.set_nomemory`
**Job** D-14: the OOM harness produces false clean bills of health.

---

## 1. The harness fix

### 1.1 What was wrong

`run_oom_sweep.py`'s child ran the setup phase's `exec` outside any `try`:

```python
if _SETUP_CODE is not None:
    exec(_SETUP_CODE, _NS)        # unguarded

_testcapi.set_nomemory(n, n+1)
try:
    exec(_PAYLOAD_CODE, _NS)
except MemoryError:
    sys.exit(1)                   # the SAFE outcome
```

A raising setup exits **1** — the identical code — and `classify()` maps 1 to `memory_error`. So a sweep
whose setup was broken reported the **safe** outcome at every index and never armed the allocator at all.
The failure mode of the verification tool was a *perfect score*.

### 1.2 What changed

`plugins/cpython-review-toolkit/scripts/run_oom_sweep.py`

| change | effect |
|---|---|
| setup `exec` **and** both `compile` calls wrapped, `sys.exit(3)` + traceback + `OOM-SWEEP: SETUP FAILED` | exit 3 → new outcome **`setup_error`**, distinct from `memory_error` |
| `classify()` maps 3 → `setup_error`; new `is_harness_error()` | a void result is neither a crash nor safe |
| new `_DRY_RUN_TEMPLATE` / `build_dry_run_script()` / `dry_run()` | unarmed rehearsal; exit 3 = setup broke, 4 = payload broke, other = interpreter died with no injection at all |
| `sweep()` runs `dry_run()` **before** the loop; on failure returns `{"error": …, "dry_run": …}` and never sweeps | a broken snippet fails loudly and immediately, at cost 1 subprocess |
| `sweep()` aborts mid-loop on any `setup_error` and returns an `error` — **no `summary`, no `reproduced` key is emitted** | a caller cannot accidentally read a clean bill off a void run |
| passing `dry_run` recorded on every successful result | a clean sweep now carries its own proof the payload executed |
| `--no-dry-run` escape; when used, the clean verdict reads **"UNVERIFIED: the dry run was skipped, so nothing proves the payload ran at all"** | the escape cannot be used silently |
| `main()` exits 2 when the result carries an `error` | CI/scripts see the failure |

**Bonus fix, same defect family** (found while re-verifying P2-F2, see §3): the child now names any
non-`MemoryError` exception (`OOM-SWEEP-EXC: <type>: <msg> | __context__=<type>`), lifted onto each result
as `exception` and aggregated into a new `payload_exceptions` field. P2-F2's *entire* signature is an
`other_exception`, and the tool previously gave no way to know which exception it was — the finding was
present in the output and unreadable. It is now self-reporting (§3.3).

### 1.3 Test evidence

`tests/test_run_oom_sweep.py` — appended three classes, 16 new tests (the file's existing classes were
left in place; two `setUp`s gained a `dry_run` stub since `sweep()` now calls it):

- `TestSetupErrorIsNotSafe` (6) — exit 3 ≠ `memory_error`; `setup_error` is neither crash nor safe;
  the child guards setup *and* compile; **a raising setup really exits 3** (end-to-end against the running
  interpreter); `sweep()` aborts on `setup_error` and emits **no** `summary`/`reproduced`.
- `TestPayloadExceptionIsNamed` (4) — the child emits the marker, the extractor lifts it, it is quiet when
  absent, and `sweep()` collects `payload_exceptions` **without** turning them into crashes.
- `TestDryRun` (6) — the dry-run script never arms the allocator (no `set_nomemory`, no `_testcapi`);
  exit 3/4 distinguish setup from payload (verified by running all three cases); a failed dry run blocks
  the sweep entirely (`swept == []`); a passing one is recorded; skipping it marks the verdict UNVERIFIED;
  an interpreter-level death is its own phase.

```
$ /home/danzin/venvs/cpython-review-toolkit/bin/python -m unittest discover tests
Ran 812 tests in 1.486s
OK (skipped=2)
```

796 → **812**, no regressions. `ruff format` + `ruff check` → *All checks passed!*;
`mypy plugins/cpython-review-toolkit/scripts/run_oom_sweep.py` → *Success: no issues found*.

### 1.4 Live A/B against a real build

`repro/oom/d14_demo.py` renders the **pre-fix** child verbatim alongside the fixed one and runs both on
`debug-gil-nojit` with a deliberately raising setup (`pickle.dumps(lambda: 1)` — the same shape that caused
the original 400/400):

```
setup (deliberately raising): 'import pickle\nclass C: pass\npickle.dumps(lambda: 1)\n'

PRE-FIX  child, sweep 0..20 : {'memory_error': 20}     <-- perfect clean bill of health
FIXED    child, sweep 0..20 : {'setup_error': 20}

FIXED dry_run(): ok=False phase=setup rc=3
  File "<oom-setup>", line 3, in <module>
  _pickle.PicklingError: Can't pickle <function <lambda> ...>: it's not found as __main__.<lambda>
  OOM-SWEEP DRY-RUN: SETUP FAILED

FIXED sweep() top-level keys: ['dry_run', 'error', 'payload', 'python', 'setup']
sweep error: unarmed dry run failed in the setup phase (exit 3) — the sweep would have tested nothing.
```

Note the fixed `sweep()` result has **no `summary`, no `reproduced`, no `outcome_counts`** — there is
nothing for a downstream reader to mistake for a result.

**Status: uncommitted.** Both files are ` M` on `main`. The fix is not yet on any branch or PR.

---

## 2. Re-verification: P2-F2 — `type_update_dict:6714`

### VERDICT: CONFIRMED, on 4 builds, with the stated control holding exactly

`Objects/typeobject.c:6713-6719` converts *every* `_PyDict_SetItem_LockHeld` failure into an unnarrowed
`PyErr_Format(PyExc_AttributeError, …)`. A `MemoryError` from the insertion is destroyed.

Payload `for _nm in NAMES: setattr(T, _nm, 1)` (48 fresh names, `NAMES` prebuilt in the unarmed setup);
control is the identical loop against an **instance**. `repro/oom/p2f2_setup.py`, `repro/oom/p2f2_probe.py`.

| build | TYPE path | INSTANCE control, same index |
|---|---|---|
| `debug-gil-nojit` | n=1, 2 → `AttributeError: type object 'T' has no attribute 'z031'`, `__context__=None` | `MemoryError` |
| `release-gil-nojit` | n=1, 2 → same | `MemoryError` |
| `debug-ft-nojit` | n=1 → same | `MemoryError` |
| `release-ft-nojit` | n=1 → same | `MemoryError` |

At n=0 **both** paths raise `MemoryError` — the n=0 allocation is upstream of the conversion. That is the
sharpest possible form of the control: the same script, the same build, one index apart, one path
propagating and one destroying.

**Strengthened beyond the ledger's claim.** P2-F2 is not payload-specific. Two unrelated payloads written
for *other* regions reproduce it incidentally, and the fixed tool now names them without any hand-written
probe:

```
cold_lookup  n=1  AttributeError: type object 'LLeaf' has no attribute 'q002' | __context__=None
cold_lookup  n=2  AttributeError: type object 'LLeaf' has no attribute 'q016' | __context__=None
cold_lookup  n=3  AttributeError: type object 'LLeaf' has no attribute 'q016' | __context__=None
p2_watch     n=13 AttributeError: type object 'W3'    has no attribute 'b'    | __context__=None
```

Three types, three attribute names, four payloads. The trigger is simply *the first insertion of a new key
into a type's dict while an allocation fails* — i.e. any `SomeClass.newattr = …`.

---

## 3. Re-verification: P2-F16 — `mro_implementation_unlocked:3503` (CPY-0014)

### VERDICT: CONFIRMED, FT-only, both claimed routes, deterministic

Sweeps via the fixed tool, `dry_run=ok` on every one.

**Route A — `class Victim(M1, M2): pass`** (`mini_setup.py` + `mini_class.py`):

| build | outcome_counts | crash |
|---|---|---|
| `debug-ft-nojit` | `{memory_error: 20, segv: 1, completed: 19}` | **n=10**, 3/3 identical re-runs |
| `debug-gil-nojit` | `{memory_error: 17, completed: 23}` | none |

C stack at the fault (`faulthandler`, no gdb):
```
_Py_Dealloc+0x167
PyList_New+0x199
PyType_Ready+0x109
```

**Route B — `__bases__` assignment.** In the wider `p2_bases.py` sweep on `debug-ft-nojit`, both routes
fault in one run, at the two payload lines that correspond to them:

| index | payload line | C stack | runs |
|---|---|---|---|
| n=30 | line 13 `class Sub(M1, M2): pass` | `PyList_New+0x199 → _Py_Dealloc`, under `PyType_Ready` | 5/5 SIGSEGV |
| n=44 | line 17 `Sub.__bases__ = (NB1, NB2)` | `PyList_New+0x199 → _Py_Dealloc`, under `PyObject_SetAttr` | 5/5 SIGSEGV |

`PyList_New` freeing a list whose `ob_item`/`ob_size` were never written — CPY-0014 — reached from two
ordinary Python constructs. **Both GIL builds clean at every index**, as the record says.

**Recorded for whoever re-runs this:** the crash **does not reproduce under gdb** — the child exits 1
(clean `MemoryError`) with gdb attached, and running the child from a **file** instead of `-c` also shifts
the index out of the window. This is the uninitialized-dealloc class behaving exactly as the toolkit's own
guidance warns: whether the garbage faults depends on what the allocator last put there. Reproduce with
`python -c "$(cat child.py)"`, and use `faulthandler`'s C stack rather than gdb.

---

## 4. Fresh sweeps of the pass-2 regions

`repro/oom/sweep_regions.py`, `max_n=220`, `width=1`, dry-run verified on every case.
Full JSON: `repro/oom/sweep_debug-gil-nojit.json`, `repro/oom/sweep_debug-ft-nojit.json`.

| region | typeobject.c | `debug-gil-nojit` | `debug-ft-nojit` |
|---|---|---|---|
| MRO / C3 | 3217–3702 | 62 ME · **13 abort** @ 52-56,64-66,70,71,79-81 | 62 ME · **13 abort** · **2 segv** @ 30,44 |
| watchers | 971–1481 | 95 ME · 1 exc · **3 abort** @ 18-20 | 93 ME · 1 exc · **3 abort** @ 16-18 |
| wide (all regions) | — | 157 ME · 1 exc · **6 abort** @ 151-153,161-163 | 161 ME · **6 abort** · **1 segv** @ 147 |
| `__class__` assignment | 7482–7846 | 4 ME · clean | 4 ME · clean |
| pickle / `__reduce_ex__` | 7848–8406 | 12 ME · clean | 14 ME · clean |
| lookup cache + getattro/setattro | 6140–6848 | 5 ME · clean | 5 ME · clean |
| managed static types | 228–522 | 57 ME · clean | 51 ME · clean |

The FT MRO row — **13 aborts and 2 SIGSEGVs** — independently reproduces the exact figures D-14 records
as the post-fix result.

### 4.1 Every abort is one defect, and it is already recorded

All 22 aborts on each build carry the identical assertion:

```
Objects/typeobject.c:11938: update_one_slot: Assertion `!PyErr_Occurred()' failed.
```

gdb pins the same chain from three independent payloads (`p2_bases`, `p2_watch`, `mini_bases`):

```
#8  update_one_slot            Objects/typeobject.c:11938   assert(!PyErr_Occurred())
#9  update_slots_callback      Objects/typeobject.c:12080
#10 update_subclasses          Objects/typeobject.c:12356
#11 update_slot                Objects/typeobject.c:12124
#12 update_all_slots           Objects/typeobject.c:12177
#13 type_set_bases_unlocked    Objects/typeobject.c:1967
#15 type_setattro              Objects/typeobject.c:6807     <-- X.__bases__ = (...)
```

That is **CPY-0070** (`res = add_all_subclasses(...)` at `:1966` never tested; `update_all_slots` on the
very next line *is* tested). **No new crash class was found.** What is new is reachability breadth:
CPY-0070's record cites one payload (`C.__bases__ = (B,)`, aborts at n=5,6,7). It in fact fires from
**any `__bases__` assignment** — 22 indices, 3 payloads, on both GIL and FT debug builds, including the
watcher payload where the `__bases__` line is incidental. Worth widening the record's `sites`/reachability
the same way P2-F16 widens CPY-0014's.

### 4.2 The clean rows prove much less than they look — a *second* false-clean mechanism

`setclass`, `pickle` and `lookup-getattro` are "clean" over 220 indices, but their `memory_error` counts
are **4, 12 and 5**. Those counts *are* the denominator: with `width=1`, indices past the payload's last
allocation are `completed` and test nothing. Three regions were each certified on **4–12 allocation-failure
points**.

The cause is over-warming: `p2_setup.py` warms the very paths it later tests (`_ = dobj.__reduce_ex__(2)`,
`_ = LEAF.__mro__`, `_ = sleaf.m()`), so the payload reuses caches instead of allocating. I built cold
variants (`repro/oom/cold_setup.py` — imports and freelist drain only, class *definitions* never exercised)
and re-swept `debug-gil-nojit` at `max_n=400`:

| payload | warm denominator | cold denominator | crashes |
|---|---|---|---|
| `__class__` assignment | 4 | **11** | none |
| pickle / `__reduce_ex__` | 12 | **83** | none |
| lookup / getattro / setattro | 5 | **11** (8 ME + 3 clobbered) | none (3 × P2-F2) |

Still clean — but now on a denominator worth quoting. **This is the mirror image of D-14 and it survives
the D-14 fix**: D-14 makes a *broken* setup look clean; over-warming makes a *working* setup test almost
nothing, and the dry run passes happily either way. Both produce false confidence. The honest reporting
unit is the `memory_error` + crash count, never `iterations_run`.

**Proposed toolkit follow-up (D-17):** `sweep()` should compute
`allocation_failure_points = iterations_run − completed` and refuse to phrase a clean result as
"handled cleanly" when that number is small (say < 20), saying instead "clean over N=4 failure points —
too thin to certify". A 400-iteration sweep reporting 4 real failure points currently reads as 400 pieces
of evidence and is 4.

---

## 5. Retrospective scope of D-14

### 5.1 What structurally *cannot* be affected

- **Every crash / positive result.** A false clean cannot manufacture a SIGSEGV, SIGABRT or sanitizer
  report. All `reproduced` catalog records stand: CPY-0011, 0014, 0015, 0016, 0017, 0070, 0073, and every
  reproduced pass-1/pass-2 finding.
- **Every sweep run without `--setup`** — the unguarded `exec` is inside `if _SETUP_CODE is not None`.
- **Every sweep predating the `--setup` feature.** `--setup` was added in `d2cf789` (v0.8, 2026-07-24);
  the v0.7 harness had no setup phase, so v0.7-era sweeps are exempt by construction.
- Records whose negative status has a *non-sweep* reason: **CPY-0018** (no Python-level trigger; explicitly
  refuses to report a clean sweep) and **CPY-0008** (Windows-only host).

### 5.2 MUST re-run — clean negative, setup used, setup not provably safe

1. **`reports/obj-typeobject/agents/uninitialized-dealloc-auditor.md` §4.1 — the 1150-iteration verdict.**
   Highest exposure in the campaign. Five sweeps, all clean, all with a setup phase, underwriting:
   *"the `dealloc-of-uninitialized-object` shape is absent from the pass-1 construction surface of
   `Objects/typeobject.c`"*. **Three of the five rows are 100 % `memory_error` with 0 `completed`** — that
   is D-14's exact signature (a payload that never runs cannot reach a `completed` index). The setup
   scripts (`tn_setup.py`, `fs_setup.py`) lived in a scratch dir and are **not preserved in the tree**, so
   the claim cannot be audited without re-running it. Pass 2 has already found FT SIGSEGVs and 22 aborts in
   overlapping regions with different payloads, which weakens the verdict independently.
2. **`reports/modules-sample-informed-v1/agents/error-path-analyzer.md` — four clean sweeps**, all with an
   inferable setup: `_zoneinfo.ZoneInfo.from_file` (400 iters, 160/240), `_queue.SimpleQueue` (300, 6/294),
   `struct.Struct.pack` after `_clearcache()` (300, 14 ME / **286 `other_exception`**), and
   `_pickle` `UnpicklerMemoProxy.clear/copy` (60, 4/56) — the last of which is the stated reason a finding
   sits at *"CONSIDER pending a working trigger"*. The `struct` row is doubly worth redoing: 286 unnamed
   `other_exception`s is precisely what the §1.2 bonus fix now makes readable, and 6/300 and 4/60 are
   §4.2-thin denominators regardless of D-14.

### 5.3 Re-run for hygiene — clean negative, setup used, setup trivially non-raising

`CPY-0011` §3's deliberately-recorded false negative (setup = one import + one construct), `CPY-0017`'s
"60/60 clean" analysis (setup = a template + `iter()` warm-up), `CPY-0014` §3's GIL control (marshal warm-up
+ freelist drain). Each setup is a couple of statements that cannot raise, so D-14 is very unlikely to be
the cause — and in each case the record *already* diagnoses a different cause (freelist / warm-up).
Low priority.

**Discharged by this run:** pass 2 §4.2's post-fix zero rows for `p2_setclass` / `p2_pickle` /
`p2_super_lookup`. I re-swept all three with the fixed harness on both debug builds, plus cold variants at
`max_n=400`. Still clean; denominators now measured and stated (§4.2). No re-run needed.

### 5.4 Affected by a *different* defect, not D-14

**CPY-0079** is the only catalog record whose status (`static-confirmed (repro pending)`) rests on a clean
sweep — *"An OOM sweep over `{"a":1}.copy()` … produced 2 memory_error / 28 completed and no crash"*. Its
`repro.py` shows `--code` only, **no `--setup`**, so D-14 is exempt. Its weakness is §4.2's: **2 failure
points**. It should be re-run with a wider range and a payload that reaches
`typeobject.c:4900`'s caller — but for the denominator reason, not this one.

### 5.5 The same defect, cloned outside `run_oom_sweep.py` — 8 more harnesses

The fix does **not** reach these. Each maps exit 1 to "clean MemoryError" with an unguarded setup `exec`:

| harness | risk |
|---|---|
| `cpython-review-findings/reports/CPY-{0011,0014,0015,0016,0017}/repro.py` — one shared `_CHILD` template, 5 copies | Unguarded setup `exec`; parent prints `no crash in 0..N`. All five setups are currently non-raising and all five records are `reproduced`, so **no current result is wrong** — but these are the artifacts a maintainer runs, and any future edit false-cleans silently. They also still use unbounded `set_nomemory({k})`, the width defect fixed in `909dbf4`. **Regenerate from the fixed harness.** |
| `reports/obj-typeobject/repro/scratch/sweep1.py` (+ `mk.py`, `mkb.py`) | Unguarded; falls through to `print("no crash in 1..N")`. This is the harness behind pass 1's `n=127` typeobject results — those were *crashes*, so not falsified. |
| `reports/pass2/obj-typeobject/repro/uninit-dealloc/probe_depth.py` | Unguarded, and its binary search is literally `while run(hi) == 1`. Ironically the tool that **exposed** D-14 inherits it. |
| `reports/objects-sample-informed-v1/repro/oom_sweep.py` | Warm-up catches `Exception` (not `BaseException`) and only *prints* `WARMUP-FAIL` without aborting; crash detection is `WIFSIGNALED`-only, so a child that raises (exit 2) is silently non-crash. Same direction as D-14. Underwrites the **"4,400 injections, 4 crashes, range [60,200) produced zero crashes"** negative → **that negative needs a re-run.** |

### 5.6 The lesson worth writing into the design notes

The `--setup` feature was requested in
`reports/objects-sample-informed-v1/agents/uninitialized-dealloc-auditor.md:441-447`, and that same request
predicted the exit-code collision:

> *"Suggest adding a `--setup` argument that is executed before `_testcapi.set_nomemory(start)`. Also:
> **classify exit code 1 carefully** — the stock harness maps it to `memory_error`, but an
> ASan-instrumented build also exits 1 on a sanitizer report"*

The ASan half was implemented (`_SANITIZER_ERROR_RE`). The setup half was not. **The warning about exit-1
overloading was written down, acted on once, and the second instance of the identical hazard shipped in the
same commit.** Meanwhile every agent that produced a clean sweep had been told, verbatim in four separate
informed briefings, that *"a clean OOM sweep is not an exoneration"* — and four sweeps still reported
400/400 clean and were believed. Documentation of a hazard is not a defence against it; only a distinct
observable outcome is. That is the argument for `setup_error` being its own outcome rather than a note in
the docstring.

---

## Artifacts

All paths absolute.

**Toolkit (uncommitted, branch `main`)**
- `/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts/run_oom_sweep.py`
- `/home/danzin/projects/cpython-review-toolkit/tests/test_run_oom_sweep.py`

**Reproducers** (all under `/home/danzin/projects/cpython-review-toolkit/reports/pass2/obj-typeobject/repro/oom/`)
- `d14_demo.py` — pre-fix vs fixed A/B on a raising setup
- `p2f2_setup.py`, `p2f2_probe.py` — P2-F2 type-vs-instance differential
- `sweep_regions.py`, `p2_static_setup.py`, `p2_static.py` — the 7-region sweep driver
- `cold_setup.py`, `cold_setclass.py`, `cold_pickle.py`, `cold_lookup.py` — de-warmed variants (§4.2)
- `detail.py` — re-run one index, print the stderr *head* (the assertion line, which `run_one`'s tail-keep truncates)
- `sweep_debug-gil-nojit.json`, `sweep_debug-ft-nojit.json` — full sweep records
