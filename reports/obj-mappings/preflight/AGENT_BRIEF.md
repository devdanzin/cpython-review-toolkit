# Agent brief — obj-mappings slice (READ THIS FIRST, then the briefing)

Run dir: `/home/danzin/projects/cpython-review-toolkit/reports/obj-mappings/`
Also read: `preflight/RUN_CONTEXT.md`, then `preflight/informed_briefing.md`.

## Scope — hard boundary

**Exactly two files:**
- `/home/danzin/projects/cpython/Objects/dictobject.c` (8,597 lines)
- `/home/danzin/projects/cpython/Objects/setobject.c` (3,228 lines)

Target ref `4f3be1b5777`. You may *read* headers (`Include/internal/pycore_dict.h`,
`pycore_setobject.h`, `pycore_critical_section.h`) and other files for context, and you may
follow a call into another file to prove reachability — but a **finding** must be anchored in
one of the two files above. Anything you notice outside them: one line in a
"Noticed outside slice" section, no further work. `Objects/odictobject.c` is **another
slice** (though its bugs are structurally instructive — see below).

## The four lessons from the obj-typeobject pass-2 run — these are not optional

1. **READ the code. Do not just triage scanner output.** In pass 2 the scanners' net-new
   contribution was **one** live finding out of 24 FIX. Everything else came from agents
   *reading regions* — repeatedly from agents working outside their own declared bug class.
   Budget the majority of your effort on reading, with the scanner output as a starting index,
   not as the work.

2. **Report the DENOMINATOR before calling a zero clean.** "0 findings" is meaningless
   without "…out of N candidates examined". If your scanner's population for this slice is 0,
   say so explicitly and state whether that is *structural* (the rule cannot fire on this
   code) or *evidential* (it fired on nothing). Four rules were once certified clean on a
   zero denominator. Every `scanners/*.sample.json` in this run dir is slice-scoped, so its
   `denominators` field is real — quote it.

3. **A guarded twin is a twin only for the threat model its comment addresses.** When you
   cite a correctly-handled sibling as "the fix", read what its comment/guard actually
   defends against. A lock that defends against re-entrancy is not evidence of safety
   against a *different* thread racing.

4. **Verify before you assert a repro.** If you claim a crash, run it and paste the actual
   exit code / ASan frame. Pass 2 recorded one "SIGSEGV after 2 iterations" that does not
   reproduce at all. Say "N/M runs" every time.

## Already recorded — CONFIRM in one line, do not re-litigate

These are in the findings repo (`/home/danzin/projects/cpython-review-findings/reports/`).
Read the relevant `meta.json` before writing about the same site.

| id | site | status | one-line |
|---|---|---|---|
| CPY-0015 | `dictobject.c:5646 dictiter_new` | reproduced | `Py_DECREF` of a never-GC-tracked item-iterator on the error path |
| CPY-0019 | `dictobject.c:8427 frozendict_pair_hash` | reproduced | guardless copy of `tuple_hash` → native stack overflow |
| CPY-0079 | `dictobject.c:4494 copy_lock_held_untracked` | static-confirmed | `assert()` derefs an unchecked allocation — **its clean OOM sweep had a denominator of 2; needs re-running** |
| CPY-0096 | `dictobject.c:1971 insert_split_key` | reproduced | runs arbitrary Python under the non-reentrant `DONT_DETACH` keys mutex |
| CPY-0107 | `dictobject.c:1385 _Py_dict_lookup` | static-confirmed | `compare_unicode_generic` runs `PyObject_RichCompareBool` under `LOCK_KEYS` — the only lock-order inversion found tree-wide |
| CPY-0115 | `dictobject.c:2987 delitem_common` | lead | live `PyDict_Next` cursor vs. mutation |

**Inherited from obj-typeobject pass 2 and owed to *this* slice** — CPY-0096's three further
consequences reproduce on the **default GIL build** and were deliberately left for you:
an ASan heap-buffer-overflow, a `dk_usable` invariant break 5/5 from a single re-entry, and
unbounded recursion → SIGSEGV. If your class touches these, pick them up.

Structurally instructive neighbours in `odictobject.c` (**other slice — do not report, but
hunt their shape here**): CPY-0011 (`odictiter_new` untracked-DECREF, the twin of CPY-0015),
CPY-0028 (`odictiter_reduce` copies a struct *including its `ob_mutex`*), CPY-0030
(`Py_NewRef(failobj)` with no NULL check).

## What this slice is *for* — the hunt list

`dictobject.c` and `setobject.c` are the two densest re-entrancy surfaces in the object
layer. The recurring shape family:

- **User `__hash__` / `__eq__` re-entry.** Every lookup can run arbitrary Python. What
  state was cached in a local across that call? `ma_keys`, `dk_entries`, `so_table`,
  `entry`, `mask`, indices, a borrowed value — all can be stale after the call returns.
  `setobject.c`'s `set_add_entry` has a documented restart loop; find the siblings that
  do not.
- **Iterator invalidation.** `PyDict_Next` / `_PyDict_Next` cursors held across anything
  that can mutate; `di_used` / `si_used` version checks present in some iterators and
  absent in others.
- **Split-table / combined-table transitions** and the `dk_usable` / `dk_nentries`
  invariants around them.
- **Free-threading**: `LOCK_KEYS` / `Py_BEGIN_CRITICAL_SECTION` scope, atomic-vs-plain
  asymmetry on `ma_used` / `dk_refcnt` / `so_used`, and any lock held across a call that
  can run Python (that is CPY-0096's exact shape — hunt the rest).
- **Resize during iteration / during a callback.**

## Environment

```
~/venvs/cpython-review-toolkit/bin/python     # NEVER bare `python` — it is RustPython and silently prints nothing
```
Scripts: `/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts/`
Builds: `~/projects/python_build_matrix/builds/<name>/python` —
`debug-gil-nojit`, `debug-ft-nojit`, `release-gil-nojit`, `release-ft-nojit`, plus `-asan` / `-tsan`.

Traps, all previously paid for:
- Ambient `python` is RustPython → silent empty output. Always name an interpreter.
- **Heredoc-to-stdin mangles multi-line Python.** Write a `.py` file into `repro/` and run it.
- `gh search issues` silently returns nothing here. Use
  `gh api -X GET search/issues -f q='repo:python/cpython <terms>'`.
- `*-ft-*-asan` builds have **no ASan shadow for the object heap** (free-threaded builds route
  PyObjects through mimalloc and CPython leaves `MI_TRACK_ASAN` undefined). An ASan report from
  an FT build can name the *victim* rather than the writer. Use a **GIL** ASan build for
  heap-overflow / UAF evidence.
- Debug builds can hide bugs; check crash clusters on both debug and release.

## Output

Write your report to `reports/obj-mappings/agents/<your-agent-name>.md`.
Reproducers go in `reports/obj-mappings/repro/` as `.py` files, named after the finding.

Structure it as:

1. **Denominator line** — candidates examined / scanner population / lines read.
2. **Findings** — each with: file:line, function, classification (FIX / CONSIDER / POLICY /
   ACCEPTABLE), the mechanism in 2-4 sentences, the **guarded twin** (and what threat model
   that twin's guard actually addresses), reproduction status with N/M runs and build name.
3. **Classes bounded** — what you checked and found clean *here*, with the denominator.
4. **Toolkit feedback** — recall gaps (something you found by reading that the scanner
   missed — this is the highest-value output), precision per rule, concrete tuning proposals.
5. **Noticed outside slice** — one line each.
