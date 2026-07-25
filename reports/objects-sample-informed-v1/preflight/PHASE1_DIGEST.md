# Phase 1 digest — structural + temporal orientation

Both preflight agents' full reports are in `<run>/agents/include-graph-mapper.md` and
`<run>/agents/git-history-context.md`. This is the condensed hand-off every Phase 2 agent reads.

## Structural (include-graph-mapper)

`sym` = files tree-wide referencing the type's C-API prefix (real fan-in).
Tier ≠ reachability here: four INTERNAL-tier files are syntax-level Python types.

| file | tier | sym | Python surface | family |
|---|---|---|---|---|
| tupleobject.c | PUBLIC (8 ABI) | **277** | `()`, `tuple()`, `*args` | F2 |
| funcobject.c | CPYTHON | 38 | `def`, `lambda`, `__code__=` | F7 |
| genericaliasobject.c | PUBLIC (2) | 33 | `list[int]`, `__class_getitem__` | **F1**, F2, F3 |
| structseq.c | PUBLIC (7) | 28 | `os.stat()`, `sys.version_info` | – |
| descrobject.c | PUBLIC (7) | 23 | `type.__dict__`, `property()` | F2, F5 |
| capsule.c | PUBLIC (13, no LIMITED_API guard) | 23 | C-extension CAPI + `repr()` | – |
| weakrefobject.c | PUBLIC (4) | 22 | `weakref.ref(o, cb)` — callbacks at teardown | F2, F3 |
| cellobject.c | CPYTHON | 14 | closures, `cell_contents` (writable) | F2, F7 |
| lazyimportobject.c | INTERNAL | 11 | `lazy import x`, `sys.set_lazy_imports()` | **F8** |
| interpolationobject.c | INTERNAL | 10 | `t"{x}"` | F4 |
| templateobject.c | INTERNAL | 9 | `t"..."`, `Template.__iter__` | F3, F4 |
| iterobject.c | PUBLIC (4, no guard) | 7 | `iter(seq)`, `iter(c, sentinel)`, `anext()` | **F3** |
| unionobject.c | INTERNAL | 6 | `int \| str` **and** `typing.Union[...]` | **F1**, F2 |
| odictobject.c | CPYTHON | 5 | `collections.OrderedDict` | F2, F3, F6 |

Families: F1 parameter-walk (genericalias/union) · F2 container hash/richcompare ·
F3 iterator · F4 t-string · F5 descriptor · F6 odict/dict duality · F7 function/cell · F8 lazy import.

**Three things that change triage:**
1. **CPY-0002 widens.** `_Py_make_parameters`/`_Py_subs_parameters` live in `genericaliasobject.c:186,406`,
   are declared in `pycore_unionobject.h:18-19`, and are called from `unionobject.c:332,349` — so the
   unguarded recursion is reachable via `typing.Union[...]`, not only `list[...]`.
2. **Two sibling hunts escape the 14-file scope** — `tupleobject.c:367` names `frozendict_pair_hash`
   (actually at `Objects/dictobject.c:8415`), and `odictobject.c` delegates richcompare to
   `dictobject.c`. Report those as **scope escapes**, not clean negatives.
3. **Guarded-twin map (measured `Py_EnterRecursiveCall` / `Py_ReprEnter` counts):**
   `tupleobject.c` 0/1 (`:298` — repr guarded, hash not = CPY-0001) · `odictobject.c` 0/1 (`:1448`) ·
   `descrobject.c` 2/0 (`:300`, call-path only, not repr/hash) · **all 11 other sample files: 0/0.**

## Temporal (git-history-context)

Full clone confirmed (132,320 commits back to 1990; `Objects/` = 9,203 commits).

**Bug-fix-density watchlist** (bug-fix commits / crash-safety subset / fixes since 2024, `--follow`):
tupleobject 96/37/13 · descrobject 60/30/8 · funcobject 57/34/12 · weakrefobject 53/24/7 ·
structseq 42/19/6 · odictobject 42/17/12 · genericalias 26/8/10 · iterobject 21/7/4 ·
cellobject 18/6/3 · unionobject 18/6/6 · capsule 6/4/2 · templateobject 2/1/2 ·
interpolation 1/0/1 · **lazyimport 0/0/0**.

**Recency-weighted the order flips: `genericaliasobject.c` is #1** (5 distinct 2026 fixes in 1070 lines).
`cellobject.c` and `capsule.c` are dormant (0 commits in 18 months) — deprioritize.
2025 was the peak fix-year in 36 years (29); 2026 is already at 26 in seven months.

**Clusters (a cluster predicts the same shape recurs unfixed elsewhere):**
- odict reentrancy→UAF — fixed 2015, 2024, 2026, same shape via a different method each time
- genericalias error paths — 4 fixes in 5 months, two in the *same function*
- FT lazy-init races — 25+ since 2025
- recursion-guard asymmetry — repr guarded, hash never; 7 of 9 sample files have **zero** guard calls
- PyErr_Clear-on-success — an open upstream sweep (gh-146102), 2 of N commits done

**Top fix-propagation leads (left-behind siblings — direct work for Phase 2):**
1. `68abf17fa92` (gh-153298, **7 days before HEAD**) wrapped `ga_parameters`' lazy init in
   `Py_BEGIN_CRITICAL_SECTION`. `descrobject.c:descr_get_qualname` is structurally identical and
   unfixed — **TSAN-0043 now has a week-old guarded twin.**
2. `_Py_subs_parameters` fixed twice two months apart; a "fix various refleaks" sweep touched the
   function and missed a NULL deref two lines away.
3. odict `copy()` UAF fixed; `odict_repr` (the site of the *2015* version of the same bug) still has
   no `od_state` snapshot.
4. `244300162d2` (2026-05-20) *added* the comment "update also frozendict_pair_hash() which copied
   this code" above `tuple_hash` — the copy-paste is now formalized and the guard is absent in both.
   Confirms CPY-0001.
5. gh-144330 fixed classmethod/staticmethod init-bypass in `funcobject.c`; no sweep followed.

**Resolved open question from RUN_CONTEXT:** `scan_pyerr_clear`'s 0 across all of `Objects/` is a
**scoping gap, not an upstream fix** — gh-146102 (2026-06-15) treats `PyErr_Clear()` on a *success*
path as a bug, and the scanner only looks at the destructor family.
