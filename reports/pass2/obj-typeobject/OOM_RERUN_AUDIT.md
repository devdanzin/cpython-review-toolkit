# Re-run audit: every campaign OOM verdict D-14 / D-17 could have falsified

Run after PR #30 landed the fixed `run_oom_sweep.py`. Build: `debug-gil-nojit`
(plus `-asan` / `debug-ft-nojit` where the original used them), all at
`a1d580430c8`.

**Headline: one verdict was VOID, two are downgraded to "not certifiable", and
three now stand on evidence they did not previously have.**

---

## 1. Pass-1 uninit-dealloc: "1150 iterations, 0 crashes, the shape is absent"

**750 of those iterations tested nothing.** The FromSpec setup called
`_testcapi.create_type_from_repeated_slots(0)` and `(1)` — **both raise
`SystemError` unconditionally**; they are negative tests, not warm-up. Pre-fix,
a raising setup exited 1, which `classify()` mapped to `memory_error`, so the
sweep scored 100% clean at every index. That is exactly the 300/300, 200/200,
250/250 signature in the pass-1 table.

| build | payload | claimed | real N | crashes | status |
|---|---|---|---|---|---|
| `debug-gil-nojit` | `type()` | 400 | **48** | 0 | valid, denominator was never 400 |
| `debug-ft-nojit` | `type()` | 400 | **43** | 0 | valid, same |
| `debug-gil-nojit` | FromSpec | 300 | — | — | **VOID** — setup raised |
| `debug-gil-nojit-asan` | FromSpec | 200 | — | — | **VOID** — setup raised |
| `debug-ft-nojit` | FromSpec | 250 | — | — | **VOID** — setup raised |

### Corrected FromSpec sweep — the one pass 1 meant to run

Dropping the two always-raising calls and keeping the four that work
(`test_type_from_ephemeral_spec`, `pytype_fromspec_meta`,
`create_type_with_token`, `make_type_with_base`):

| build | max_n | real N | crashes |
|---|---|---|---|
| `debug-gil-nojit` | 300 | **56** | 0 |
| `debug-gil-nojit-asan` | 200 | **51** | 0 |
| `debug-ft-nojit` | 250 | **51** | 0 |

**Verdict: the conclusion survives, but for the first time it is earned.** The
`type_from_slots_or_spec` error-unwind path had in fact **never been swept** —
which matters, because that is the same function pass 2's baseline flagged at
`typeobject.c:5747`. Total real evidence is **249 allocation-failure points**,
not 1150 iterations.

---

## 2. Modules-sample error-path: four clean sweeps

| sweep | recorded | re-run N | thin? | verdict |
|---|---|---|---|---|
| `_zoneinfo` `from_file` | 400 iters, 160 ME | **160** | no | **stands, earned** |
| `_pickle` `UnpicklerMemoProxy.clear` | 60 iters, 4 ME | **26** | no | **stands, earned** |
| `_queue` `SimpleQueue` grow+shrink | 300 iters, 6 ME | **8** | **YES** | **downgraded** |
| `_struct` `pack` after `_clearcache` | 300 iters, 14 ME | **5** | **YES** | **downgraded** |

**`_pickle` is the consequential one.** The CONSIDER on
`_pickle.c:7618-7621` (`UnpicklerMemoProxy.clear` leaving `memo == NULL` with
`memo_size` non-zero) rests on *"I tried and could not reproduce this"*. That
claim was backed by **4** real failure points; it is now backed by **26**. The
finding stays CONSIDER, but the negative behind it is real.

**`_queue` and `_struct` cannot be certified.** De-warming the setup to a bare
import — which is what D-17's verdict text tells you to do — moved them only
7→8 and 5→8. So the cause is **not** over-warming: those payloads simply do not
allocate much. `SimpleQueue` put/get reuses a preallocated ring buffer, and
`struct.pack` allocates a bytes object and little else. The honest statement is
*"clean over N≈8 allocation-failure points, which is not enough to certify"* —
not "no crash in this range". Either needs a substantially richer payload before
its negative means anything.

---

## What this says about the harness fix

Both defects behaved exactly as diagnosed, on real prior results:

- **D-14 turned a broken sweep into a perfect score.** It hid a 750-iteration
  hole in a verdict that was then written up as "the shape is absent" and
  propagated into `CPY-0086`'s lineage.
- **D-17 turned small payloads into confident negatives.** Two of the four
  modules-sample sweeps were certifying on 4–8 real data points while printing
  a 300-iteration clean bill.

Neither produced a false *positive*. Every crash result in the campaign remains
sound; what was damaged was the campaign's ability to say "clean".

**Still outstanding:** the D-14 defect is cloned in **8 harnesses this fix does
not reach**, including five catalog `repro.py` files. Those are the artifacts a
maintainer would run.
