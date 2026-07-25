# git-history-analyzer — `Objects/typeobject.c` PASS 2, Phase 3 (runs LAST)

CPython `/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0).
Cross-references all 16 static agent reports, the 19 FIX / 6 CONSIDER / 3 POLICY ledger, and my own
Phase-0 preflight (`preflight/history_context.md`).

**Clone health — verified directly, not inferred.** `git rev-parse --is-shallow-repository` → `false`.
132,320 commits, oldest 1990-08-09. `Objects/typeobject.c` carries 1,282 commits (1,342 with
`--follow`). No shallow clone, no commit cap, no script timeout. **Every temporal claim below is
sound.**

**Relationship to the preflight.** The preflight was a *where-to-look* document and ran first. This
one is a *what-does-history-say-about-what-we-found* document. It does not restate the preflight's
region table, density ranking, or watchlist. Deliverable 4 supersedes preflight §3.1's provisional
triage of the `lookup_tp_dict` group with a structural verdict.

---

# 1. FIX-PROPAGATION YIELD — new unfixed instances of already-fixed patterns

This is the run's most actionable output. Four bug patterns were extracted from named commits, then
swept tree-wide. **Two swept clean of new instances; two produced net-new defects.** The headline is
H-1, where the fix was written, reviewed, and closed unmerged four years ago.

## H-1 · [FIX] `recurse_down_subclasses` — the fix was written in 2022 and closed **unmerged**

- **Original commit**: `3a4c15bb9815` (bpo-46417, GH-**30795**, 2022-01-22) — *"Cleanup typeobject.c code"*
- **The fix that was never merged**: PR **gh-30775**, *"bpo-46417: Fix type `recurse_down_subclasses()`"* — `state: closed`, **`merged_at: null`**
- **Bug pattern**: borrowed `tp_subclasses` iterated with a live `PyDict_Next` cursor while the loop body reaches arbitrary Python
- **Location**: `/home/danzin/projects/cpython/Objects/typeobject.c:12362-12404` — borrow at **:12369**, cursor at **:12377**, Python-reaching call at **:12386**
- **Classification: FIX** · **Confidence: MEDIUM-HIGH**

**Two independent sweep lanes converged on this site.** The `Py_SET_TYPE`/`PyDict_Next` lane and the
`_PyCStackRef`-migration lane reached it separately, from different premises.

gh-30775's own PR body states the fix and the reason, verbatim:

> Type `recurse_down_subclasses()` now calls `_PyType_GetSubclasses()` to iterate on a **copy** of
> subclasses, **because its loop can modify subclasses**: hold strong references to subclasses.

It was closed without merging. What landed instead was `3a4c15bb9815` (GH-30795), a *cleanup* commit
— and that commit is the `git blame` origin of **three** pass-2 findings: P2-F9 (`:1201`), P2-F16
(`:3503`), and this one (`:12386`). CPY-0069 was already recorded as a regression of gh-30775; that
is consistent, because gh-30775 never landed, so nothing it would have fixed was ever fixed.

**Current code**, with the comment whose reasoning is incomplete:

```c
    // It is safe to use a borrowed reference because update_subclasses() is
    // only used with update_slots_callback() which doesn't modify
    // tp_subclasses.
    PyObject *subclasses = lookup_tp_subclasses(type);  // borrowed ref
    ...
    while (PyDict_Next(subclasses, &i, NULL, &ref)) {
        PyTypeObject *subclass = type_from_ref(ref);
        if (subclass == NULL) { continue; }
        /* Avoid recursing down into unaffected classes */
        PyObject *dict = lookup_tp_dict(subclass);
        if (dict != NULL && PyDict_Check(dict)) {
            int r = PyDict_Contains(dict, attr_name);      /* :12386 */
```

**Why the comment does not cover the code.** It reasons only about the *callback*. `PyDict_Contains`
at `:12386` is not the callback. A type's `tp_dict` may hold a non-string key — CPython emits a
`RuntimeWarning` but permits it — so a key with a colliding hash and a Python `__eq__` turns `:12386`
into a call into user code. Verified live on the in-tree build:

```
$ ./python -c "class K:
    def __hash__(self): return hash('spam')
    def __eq__(self, other): print('  __eq__ CALLED with', repr(other)); return False
X = type('X', (), {K(): 1}); print('spam' in X.__dict__)"
<string>:6: RuntimeWarning: non-string key in the __dict__ of class X
  __eq__ CALLED with 'spam'
False
```

**The free is synchronous and needs no GC pass.** Re-entrant Python reaches `type_dealloc` →
`remove_all_subclasses` → `remove_subclass:9772`, which does `PyDict_DelItem` on the very dict being
walked and then, at `:9790-9791`, `if (PyDict_Size(subclasses) == 0) clear_tp_subclasses(base);` =
`Py_CLEAR(self->tp_subclasses)`. The type is the sole owner, so the dict is freed immediately; the
next `PyDict_Next` reads freed memory. Same mechanism as the ASan-confirmed `:1201` defect.

**`TYPE_LOCK` does not help.** `BEGIN_TYPE_LOCK()` / `ASSERT_TYPE_LOCK_HELD()` expand to nothing on
the default GIL build (`typeobject.c:184-195`). This is reachable single-threaded.

**Guarded twin, same file, 10,500 lines up.** `mro_hierarchy_for_complete_type:1835-1852` takes the
`_PyType_GetSubclasses(type)` snapshot — the exact remedy gh-30775 proposed — carrying the comment
that names the hazard outright:

> *Otherwise `type->tp_subclasses` might be altered in the middle of the loop, for example, through a
> custom `mro()`, by invoking `type_set_bases` on some subclass of the type which in turn calls
> `remove_subclass`/`add_subclass` on this type.*

**Prior art — partial, and it matters.** Open issue **gh-151377** names this function and this
`PyDict_Next` loop, but exclusively as a *cross-thread* TSan race between `type_setattro` and
`type_new`/`type_dealloc`. The single-threaded `PyDict_Contains` → user `__eq__` → synchronous
`clear_tp_subclasses` route is not described, and gh-151377's proposed locking would not close it —
the same distinction P2-F9 already drew. **File as a comment on gh-151377, not as a novel issue.**

## H-2 · [FIX] `_Py_dict_lookup` runs a user `__eq__` under `LOCK_KEYS` — the sibling the gh-152238 revert never swept

- **Original fix**: `9626ef87f42` (gh-**152238**, 2026-06-26) — *"Revert gh-150490 and gh-152200 … it violates locking discipline and results in deadlocks"*
- **Bug pattern**: arbitrary Python executed while holding a raw, non-recursive `PyMutex` taken `_Py_LOCK_DONT_DETACH`
- **Location**: `/home/danzin/projects/cpython/Objects/dictobject.c:1385` (lock) → `:1165-1168` (the user call)
- **Classification: FIX** · **Confidence: HIGH** · free-threaded builds only

The rule being propagated is written in-tree at `Objects/dictobject.c:218-227`:

```c
// gh-151593: The _Py_LOCK_DONT_DETACH flag ensures that the outer critical
// section is not dropped if there is some contention on the keys lock.
// It also means that it will be important that LOCK_KEYS() is essentially the
// "inner-most" code and that we don't call Py_DECREF() or similar while
// holding the keys lock.
//
// We are not allowed to acquire other locks within LOCK_KEYS(). For example,
// PyType_Modified() must not be called within LOCK_KEYS() since it acquires
// the type lock.
```

P2-F6 is the violation this comment names. **`_Py_dict_lookup` is a second violation of the same
rule, in the same file, that the revert never touched:**

```c
        else {
            INCREF_KEYS_FT(dk);
            LOCK_KEYS_IF_SPLIT(dk, kind);              /* :1385 */
            ix = unicodekeys_lookup_generic(mp, dk, key, hash);
            UNLOCK_KEYS_IF_SPLIT(dk, kind);
```
→ `compare_unicode_generic`, `Objects/dictobject.c:1165-1168`:
```c
    if (unicode_get_hash(ep->me_key) == hash) {
        PyObject *startkey = ep->me_key;
        Py_INCREF(startkey);
        int cmp = PyObject_RichCompareBool(startkey, key, Py_EQ);
```
`startkey` is exact-`str` and `key` is not (asserted at `:1163`), so `unicode_richcompare` returns
`NotImplemented` and `do_richcompare` dispatches the reflected `type(key).__eq__` — **arbitrary
Python under a non-detaching, non-recursive mutex.**

Reachability is end-to-end: `insertdict:2027-2047` routes every non-`str` key on a split table down
this path and says so in its own comment; `make_dict_from_instance_attributes:7342-7354` proves the
materialized `__dict__` shares the class's `dk_mutex`.

Two deadlocks, both FT-only (`LOCK_KEYS` is a no-op at `:257` under the GIL):

1. **Same-thread self-deadlock**, no race window:
   ```python
   class C: pass
   c = C(); c.a = 1
   d = c.__dict__                       # split; ma_keys is CACHED_KEYS(C)
   class K:
       def __hash__(self): return hash('a')
       def __eq__(self, o):
           C().zz = 1                   # -> insert_split_key -> LOCK_KEYS (same mutex)
           return False
   d[K()] = 1                           # insertdict -> _Py_dict_lookup:1385
   ```
2. **Cross-thread ABBA.** `insert_split_key:1955-1958` documents the mandatory order — *"we must
   always acquire the type lock first"* — then takes `types.mutex` (`:1959`) and `LOCK_KEYS`
   (`:1962`). `_Py_dict_lookup:1385` takes `LOCK_KEYS` **without** the type lock and then runs Python
   that can want it. Neither waiter detaches, so nothing recovers.

**Why it was missed.** `git blame`: the violating region is `d5df25268b03` (2024-04-25, *"gh-112075:
`_Py_dict_lookup` needs to lock shared keys"*); the prohibiting comment is `bef570622263`
(2026-06-25). **The rule was written 26 months after the violation and was never applied backwards.**

**Prior art: none.** Four `gh api search/issues` forms returned only the gh-150490/151593/152200/152238
cycle, all closed.

## H-3 · [FIX] `tracemalloc` `TABLES_LOCK` — `PyObject_Hash(co_filename)` re-enters a non-detaching lock

- **Same pattern as H-2**, different lock family. `/home/danzin/projects/cpython/Python/tracemalloc.c:39`
- **Confidence: HIGH**

`tracemalloc.c:132` uses `PyObject_Hash` as the hash function for `tracemalloc_filenames`, and
`co_filename` is only `PyUnicode_Check`-validated (`Objects/codeobject.c:461`) — a `str` subclass
with a Python `__hash__` is accepted. Two regions take `TABLES_LOCK` and reach that hash **without
setting the reentrancy flag every other region sets** (compare `:516`, `:572`, `:723`, `:1326`, `:1361`):

- `_PyTraceMalloc_TraceRef` — `:1292` lock → `:1305 traceback_new` → `:257` → `Python/hashtable.c:142`
  → `PyObject_Hash`. Any allocation inside the Python `__hash__` re-enters `tracemalloc_alloc:539` →
  `TABLES_LOCK()` → hang. `get_reentrant()` at `:1287` is 0.
- `PyTraceMalloc_Track` (public C API) — `:1225` → `:1229` → `:445 traceback_new` → same.

**The file already knows the hazard and applied the fix exactly once.** `Python/tracemalloc.c:893-895`
hoists one call out of the lock *"since it calls `_PyEval_StopTheWorldAll()` which would lead to a
deadlock with `TABLES_LOCK()` which doesn't detach"*. That is the guarded twin; the two regions above
are the unswept siblings.

Secondary (not a deadlock): `:128-133` and `:291` ignore a `-1` from `PyObject_Hash`, leaving a live
exception inside `PyMem_Malloc`.

**Prior art:** 7 closed tracemalloc-locking issues (gh-128679, gh-139116, gh-144763 families). None
covers `PyObject_Hash` re-entrancy — they address GIL detachment and `stop()` races.

## H-4 · [CONSIDER] `_PyInterpreterState_DeleteExceptMain` self-deadlocks on `HEAD_LOCK` — known, closed **for lack of a reproducer**

- `/home/danzin/projects/cpython/Python/pystate.c:1088-1107` · **Confidence: HIGH (static) / needs a repro**

`HEAD_LOCK(runtime)` is held across `PyInterpreterState_Clear(interp)` (`:1101`), which re-enters the
same non-recursive mutex three ways: `_Py_FOR_EACH_TSTATE_BEGIN` (`pystate.c:840`),
`_Py_ClearExecutorDeletionList` → `optimizer.c:287`, and `zapthreads` → `tstate_delete_common`
(`pystate.c:1931`). The region also runs `_PySys_Audit` and `Py_CLEAR(interp->audit_hooks)` — arbitrary
Python with `__del__` — under the mutex.

**Guarded twin 250 lines above:** `interpreter_clear:842-846` explicitly drops the lock around
`PyThreadState_Clear(p)` with the comment *"Must be called without HEAD_LOCK held as it can deadlock
if any finalizer tries to acquire that lock."*

**Prior art: gh-84139**, *"Deadlock in `_PyInterpreterState_DeleteExceptMain` with `HEAD_LOCK(runtime)`"*,
`type-crash` — **closed `not_planned` 2022-07-30**: *"Closing as 3.8 is not supported anymore and
there is no reproducer, this can be re-opened once you provide a reproducer."* The sole caller is
`PyOS_AfterFork_Child` (`posixmodule.c:786`) and `:1093-1097` skips the main interpreter, so the shape
is: create a subinterpreter, then `os.fork()`. **The ask here is a reproducer, not a report.**

## H-5 · [CONSIDER] Pattern-A propagation into `Objects/object.c` — four unguarded siblings of a debug-only twin

- **Original fix**: `3bfc9c831ad9` (gh-120198) — see §2, it is a defence *deletion*
- **Confidence: MEDIUM** (`object.c:1848`) / **LOW-MEDIUM** (the three `tp_name` reads)

`PyObject_CallFinalizer` (`Objects/object.c:577-592`) captures `tp = Py_TYPE(self)`, runs the user's
`__del__` via `tp->tp_finalize(self)` at `:588`, then dereferences the capture at `:589-590` — verified
first-hand. A `__del__` doing `self.__class__ = B` invalidates it. Three more in the same file read
`tp->tp_name` after a `PyDict_GetItemRef` on an instance `__dict__` that can hold a hostile key:
`:1996`, `:1741`, `:1875`; at `:1848` the stale pointer is additionally INCREF'd onto the operand stack.

**The twin is weaker than it looks.** `_Py_Dealloc`'s `Py_INCREF(type)` / *"Make sure that
`type->tp_name` remains valid"* (`object.c:3308`) sits inside **`#ifdef Py_DEBUG`** — it proves CPython
considers the shape real, but gives release builds nothing. `_PyObject_GenericSetAttrWithDict` (`:2037`/`:2112`)
is the genuine unconditional twin, and the three getattr-side siblings lack it.

**Held at CONSIDER**: a heap type is always in a cycle through its own `tp_mro`, so the free needs a
GC pass — trivially forced with `gc.collect()` inside the `__eq__`/`__del__` on the reachable paths,
but not driven end-to-end here. **Prior art: 0 hits on two query forms.**

---

# 2. THE "DEFENCE DELETED" CLASS — a novel temporal query

**Method.** All 1,282 commits touching `Objects/typeobject.c` were diffed at `-U0` and scored for a
*net* deletion of a guard (strong reference, stack-ref pin, recursion guard, critical section, type
lock, stop-the-world, NULL check, assert, atomic, or a re-fetch of a mutable type field), plus deletion
of a comment stating *why* a guard was there. Script:
`…/scratchpad/hist/defence_deleted.py`; ranked output `defence_deleted.json`.

**Yield: 174 commits with a net defence deletion, 97 since 2015.** The query is productive, as
predicted. Ranked results, each checked for whether the deletion **still stands at HEAD**:

| Commit | Date | What was deleted | Still deleted? | Verdict |
|---|---|---|---|---|
| `3bfc9c831ad9` gh-120198 | 2024-07-11 | `oldto = Py_TYPE(self)` re-fetch + its 2-line comment; 4 critical sections | **YES** | **P2-F4** — confirmed |
| `5d21d884b6ff` gh-111926 | 2024-03-29 | strong MRO ref in `PyType_IsSubtype` | **YES** | see below — *later re-covered* |
| `8a7eb8b2ab2d` gh-145500 | 2026-03-04 | `_PyType_GetMRO` itself (the strong-ref helper) | **YES** | dead code by then; see below |
| `120b891e4dff` gh-124153 | 2024-10-10 | 4 NULL checks, 4 asserts, 1 strong ref | **YES** | blames **P2-C3** `:6002` |
| `67fbfb42bd5d` gh-131586 | 2025-03-26 | `lookup_maybe_method` INCREF/DECREF pair in `type_mro_modified` | **YES** | created **P2-F5**'s call site |
| `eecafc33800c` | 2025-04-28 | 9 stop-the-worlds, 7 asserts, 4 re-entrancy comments | **NO** — relanded `fbbbc10055e0` | the gh-127266 revert |
| `5a1618a2c8c1` gh-118362 | 2024-05-06 | 7 net `Py_INCREF` | **YES** | borrowed→stackref conversion, compensated |
| `00257c746c44` gh-119462 | 2024-06-19 | *"In free-threaded builds readers can race with the lock-free portion"* | **YES** | comment only |

**The strongest single result of this section.**

`5d21d884b6ff` (gh-111926, 2024-03-29) replaced

```c
#ifdef Py_GIL_DISABLED
    PyObject *mro = _PyType_GetMRO(a);      /* strong ref, TryIncrefCompare + TYPE_LOCK fallback */
    int res = is_subtype_with_mro(mro, a, b);
    Py_XDECREF(mro);
    return res;
#else
    return is_subtype_with_mro(lookup_tp_mro(a), a, b);
#endif
```
with the bare `return is_subtype_with_mro(a->tp_mro, a, b);` still present at `Objects/typeobject.c:2931`.
Its message is explicit that this was a knowing trade: *"Read the MRO in a **thread-unsafe** way in
`PyType_IsSubtype` to avoid locking. **Fixing this is tracked in #117306.**"*

Then `8a7eb8b2ab2d` (gh-145500, 2026-03-04) **deleted `_PyType_GetMRO` entirely** — I confirmed it had
zero callers by then, so this was dead-code removal, but it removed the tool the tracking issue
existed to reapply.

Then **gh-117306 was closed as `completed` on 2026-06-09** with the comment: *"I presume this was
fixed by commit `5d21d884b6ff…`, please re-open if that was incorrect."* — **citing the very commit
that introduced the problem.**

**Honest verdict: the code is nevertheless safe today, by a different mechanism.** `set_tp_mro`
(`typeobject.c:650-676`) now does `type_lock_prevent_release(); types_stop_world();` around the
non-initial store, and calls `PyUnstable_Object_EnableDeferredRefcount(mro)`. Writer-side
stop-the-world plus deferred reclamation covers the unsynchronized read. That protection arrived with
`fbbbc10055e0` (gh-127266, 2025-05-27) — **13 months after** the unsafe read was introduced and for an
unrelated reason. So: **not a defect, but a mis-closed tracking issue**, and the 13-month window was
real. Worth one line to a maintainer; not worth a bug report.

**The other productive rows.** `120b891e4dff` (*"Simplify `PyType_GetBaseByToken`"*) net-deleted four
NULL checks and four asserts and is the blame origin of **P2-C3** — the recursion finding pass 1 ruled
ACCEPTABLE and pass 2 overturned. `67fbfb42bd5d` (*"Avoid refcount contention"*) replaced
`type_mro_modified`'s explicit `lookup_maybe_method` + `Py_INCREF`/`Py_DECREF` pair with the new
`has_custom_mro` helper — which is precisely the Python-reaching call **P2-F5** survives while holding
a borrowed `bases`. A perf commit put the hazard there.

---

# 3. CROSS-REFERENCE — when was each finding's line last touched, and for what?

`git blame -L N,N` on every ledger site, with the blamed commit's subject classified. Full table
produced by `…/scratchpad/hist/blame_findings.py`.

**Headline, reproducing pass 1's strongest result: of the 30 ledger sites blamed, 22 sit in lines
whose last-touching commit was auditing something else — 8 named a *different* defect class, 6 were
explicit performance/cleanup/refactor work naming no defect class at all, and 8 predate the modern
free-threading era entirely.** Pass 1 measured 6-of-9; pass 2 measures the same phenomenon at scale.

| ID | Site | Blamed to | Age | What that commit was actually auditing |
|---|---|---|---|---|
| **P2-F1b** | `:12622` | `18203a6bc9cc` *"gh-111789: Use `PyDict_GetItemRef()` in Objects/"* | 984d | **The sharpest case in the run.** `PyDict_GetItemRef` *is* the borrowed-reference remedy. The sweep converted the borrowed **return value** to strong and never asked whether the **arguments** stay alive across the call — which is the defect. Identical shape to preflight §3.1's `ef3ceab09d2d` at `:8706`/`:8731`. |
| **P2-F5** | `:1299` | `f95a1b3c53bd` 2010 svnmerge | 5921d | Predates the concept |
| P2-F5 call | `has_custom_mro` | `67fbfb42bd5d` *"Avoid refcount contention"* | 486d | **Perf.** Introduced the Python-reaching call (§2) |
| **P2-F1** | `:12656` | `ef25febcf2ed` *"specialize LOAD_SUPER_ATTR_METHOD"* | 1187d | **Perf** (16 files) |
| **P2-F2** | `:6714` | `dee63cb35971` *"Fix a few bugs in `type_setattro` **error paths**"* | 761d | An **error-path audit** that touched this exact line and did not narrow the conversion |
| **P2-F4** | `:7825` | `3bfc9c831ad9` gh-120198 | 744d | The **defence-deleting** commit itself (§2) |
| **P2-F9** | `:1201` | `3a4c15bb9815` *"Cleanup typeobject.c code"* | 1645d | **Cleanup** — and the commit that superseded gh-30775 (§H-1) |
| **P2-F16** | `:3503` | `3a4c15bb9815` (same) | 1645d | **Cleanup** |
| **NEW-A** | `:12386` | `3a4c15bb9815` (same) | 1645d | **Cleanup** |
| **P2-F10 / F10b / C4** | `:6744`, `:12177`, `:1745` | `fbbbc10055e0` gh-127266 reland | 424d | **Data races.** Not error-return handling, not lock-release semantics |
| **P2-F15** | `:6369` | `f26eca7732ca` *"return a `_PyStackRef`"* | **137d** | A **refcount-convention** change; never audited the error-return protocol |
| **P2-F17** | `:9487` | `baae9cb159e2` *"Use an **atomic store** to set type flags"* | 515d | Audited **atomicity**, not publication **ordering** — which is the defect |
| **P2-C3** | `:6002` | `120b891e4dff` *"**Simplify** `PyType_GetBaseByToken`"* | 653d | **Simplification** that net-deleted 4 NULL checks + 4 asserts (§2) |
| **P2-C5** | `:6453` | `f268e328ed5d` *"Make `_abc` module **thread-safe**"* | 835d | Audited `_abc` thread-safety; gh-148450 later fixed only the *sibling* (preflight §4.3) |
| **P2-F13** | `:3320` | `6b91a5972107` *"**Clean up** the C3 MRO algorithm"* | 3139d | **Cleanup** |
| **P2-F6 / P2-F8** | dict `:1971`, type `:1206` | `20921922fa4d` take-2 | **19d** | The fourth iteration of a twice-failed path |
| **NEW-B** | dict `:1385` | `d5df25268b03` *"`_Py_dict_lookup` needs to **lock** shared keys"* | 821d | Audited **keys-sharing safety** — i.e. it *added* the lock that now deadlocks (§H-2) |
| P2-C2 / P2-F18 | `:1112`, `:1129` | `82ccbf69a842` *"allow setting a callback hook"* | 1373d | **Feature.** The asymmetric write was born with the API |
| P2-F3, P2-F7, P2-F11 | `:7609`, `:3776`, `:1952` | `f95a1b3c53bd` 2010 svnmerge | 5921d | Predate the concept |

**The answer to "how did code this heavily reviewed still hold these".** Two distinct mechanisms, and
they are separable:

1. **Adjacent-class blindness (8 sites).** A real audit visited the line — for error paths, for
   atomicity, for data races, for `_abc` thread-safety, for borrowed *return* values — and the defect
   is one class over. P2-F1b is the purest instance: the commit's whole purpose was fixing borrowed
   references, and it fixed the wrong end of the call.
2. **Perf/cleanup churn (6 sites).** The preflight established that *"the last 18 months of activity
   in R19, R21 and R37 is performance work, not correctness work"*. Blame confirms it at the line
   level, and adds the 2022 `3a4c15bb9815` cleanup as a third-generation source: **three separate
   pass-2 findings plus the new H-1 candidate all blame to one commit whose subject is "Cleanup".**

Note the youngest rows: `20921922fa4d` at **19 days** and `f26eca7732ca` at **137 days**. Neither is
old code that nobody looks at.

---

# 4. INCOMPLETE MIGRATIONS

## 4.1 `_PyCStackRef` pinning — the modern defence, applied to 14 of ~201 candidate sites

**Denominator, stated explicitly:** 201 target-field access expressions in `Objects/typeobject.c`
(31 `lookup_tp_dict`, 26 `lookup_tp_mro`, 15 `lookup_tp_bases`, 11 `lookup_tp_subclasses`, 47 plain
`->tp_{dict,mro,bases,subclasses}`, 53 plain `->tp_base`, 18 `su->…`). A mechanical
field-read-then-Python-reaching-call filter flagged 127 lines across 66 functions; **80 functions were
hand-triaged**, including 14 parameter-receiving callees the filter structurally cannot see (which is
where both known defects live).

**Structural pruning is what makes this bounded rather than a list of 201 maybes.** Which fields can
have their object *freed* while a C local points at it?

| Field | Freeable under a live type? | Mechanism |
|---|---|---|
| `tp_mro` | **YES** | `set_tp_mro:650`; the `undo:` rollback at `:1994` |
| `tp_bases` | **YES** | `set_tp_bases:598` |
| `tp_base` | **YES** | `type_set_bases_unlocked:1941` / `:2007` |
| `tp_subclasses` | **YES** | `remove_subclass:9790-9791` → `clear_tp_subclasses` → `Py_CLEAR` when empty |
| `tp_dict` | **NO** (in practice) | only `NULL`→new (`type_setattro:6820`); cleared only at type destruction |

**That single row disposes of 31 sites.** All `lookup_tp_dict` borrows are structurally safe while the
caller holds `type` — which **confirms and upgrades** `RUN_CONTEXT.md`'s prediction and the
preflight's §3.1 provisional triage from "most likely ACCEPTABLE" to a structural verdict. It is also
why `_PyType_GetSlotNames:7882` (borrowed dict across `PyObject_CallMethodOneArg`) and
`type_new_set_names:12278` (borrowed dict across `__set_name__`) are **not** defects.

**Sites that should pin and do not**, ranked:

| # | Site | Field | Survives | Conf. | Status |
|---|---|---|---|---|---|
| 1 | `recurse_down_subclasses:12369` | `tp_subclasses` | `PyDict_Contains:12386` | HIGH | **§H-1 — net new** |
| 2 | `_PyType_Modified_Unlocked:1195` | `tp_subclasses` | watcher cb `:1222` / `%R` `:1223` | HIGH | = P2-F9 |
| 3 | `compatible_for_assignment:7632` / `same_slots_added:7606` | `tp_base` chain, `ht_slots` | `PyObject_RichCompareBool:7609` | MED | **net new** — see below |
| 4 | `super_descr_get:12797` | `su->type`, `su->obj` | `supercheck` → `tp_getattro` | MED | **net new** — TOCTOU, not UAF |
| 5 | `mro_internal:3678` | `new_mro` (donated by `set_tp_mro`) | `type_mro_modified` → `has_custom_mro` | MED | **distinct provenance of P2-F5** |
| 6 | `_PyType_GetSubclasses:783` | `tp_subclasses` | GC finalizer via `PyList_Append:799` | LOW | comment covers direct calls only |
| 7 | `mro_hierarchy_for_complete_type:1808` | `new_mro` | `PyTuple_Pack:1813` alloc window | LOW | |
| 8 | `subtype_dealloc:2788-2792` | `tp_base` chain | `PyObject_CallFinalizerFromDealloc:2798` | LOW | re-fetches `type`, **not** `base` |
| 9 | `remove_subclass:9775` | `tp_subclasses` | `get_subclasses_key:9781` | LOW | OOM-only |

**#3 is the one worth flagging beyond H-1**, because it is the *same* `same_slots_added:7609` call
that P2-F3 and P2-F4 already turn on — but a **third** consequence. Beyond running Python inside the
stopped world (P2-F3) and staling `oldto` (P2-F4), the `RichCompareBool` also leaves
`PyTuple_GET_SIZE(slots_a)` (`:7611`), `a->tp_basicsize`/`b->tp_basicsize` (`:7613`),
`newbase->tp_base`/`oldbase->tp_base` (`:7682`) and `oldto->tp_name` (`:7693`) reading borrowed
pointers afterwards. **Practical consequence: the fix direction already agreed for P2-F3 — "hoist the
pure predicate `compatible_for_assignment` out of the stopped region" — does not on its own fix #3.**
Say so when filing.

**#5 matters for fix design:** `set_tp_mro` at `:3678` *donates* the reference, so both
`type_mro_modified(type, new_mro)` (`:3680`) and `type_mro_modified(type, lookup_tp_bases(type))`
(`:3683`) hand in borrowed pointers. **A fix applied only at the `bases` call site leaves `:3680`
broken.**

**Cleared, and worth recording** (bounded negative): `_PyType_GetSlotNames:7882`,
`type_new_set_names:12278` (takes a `PyDict_Copy` — the correct alternative defence),
`hackcheck_unlocked:10128`, `releasebuffer_maybe_call_super_unlocked:11368`, `update_one_slot:11977`,
`PyType_GetModuleByToken_DuringGC:5936`, `mro_implementation_unlocked:3440-3497`, `type_ready_mro:9262`,
`type_ready_inherit:9332`, `inherit_special:8771`, `add_operators:12459`, `add_subclass:9708`
(explicitly re-fetches after its GC-capable calls), `type_dealloc_common:6851`, all 31 `lookup_tp_dict`
borrows, and `type_set_bases_unlocked:1934/1936` — the last being a clean example of the
*ownership-transfer* defence: `set_tp_bases:1940` does not decref, so `old_bases`/`old_base` become
owned before any Python runs, balanced at `:1978-1979`/`:2015-2016`.

## 4.2 `lookup_tp_*` accessor migration (gh-94673) — **complete**

`f73abf8e03fd` (gh-94673, 2023-05-01) introduced the accessors so `PyTypeObject` fields could move to
`PyInterpreterState`. **The decisive history fact the preflight did not have:** `7be667dfafa`
(gh-105020, 2023-05-30, same author) **deliberately reverted** the `tp_bases`/`tp_mro` move —

> *"In gh-103912 we added `tp_bases` and `tp_mro` to each `PyInterpreterState.types.builtins` entry.
> However, doing so ignored the fact that both `PyTypeObject` fields are public API… We address that
> here by reverting back to shared objects, making them immortal in the process."*

So `lookup_tp_bases` / `lookup_tp_mro` are pass-throughs **by design**. Only `tp_dict` and
`tp_subclasses` carry live per-interpreter indirection.

**Counts.** 79 accessor-mediated reads in `Objects/typeobject.c` (30 `lookup_tp_dict`, 25
`lookup_tp_mro`, 14 `lookup_tp_bases`, 10 `lookup_tp_subclasses`). Raw grep for the four fields: 63
hits, 16 in comments, 47 in code — of which **34 are inside the accessor family itself** or the
static-builtin index helpers (`:228-258`, which overload the `tp_subclasses` slot to store a 1-based
index). That leaves **13 plain accesses outside the accessor family**, each classified:

- `:2931` `PyType_IsSubtype`, `:6037` `PyType_GetBaseByToken_DuringGC` — plain `tp_mro`, semantically
  identical because gh-105020 reverted that field's indirection. `:6037` self-documents (`// No
  lookup, following PyType_IsSubtype()`).
- `:4960`, `:6812`, `:6818`, `:6820` — plain `tp_dict`, all provably on non-static-builtin types
  (`type_new_impl`'s type is always a heap type; `type_setattro` is guarded by the
  `Py_TPFLAGS_IMMUTABLETYPE` rejection at `:6754`, and every static builtin is immutable, asserted at
  `:9269`). `:6820` is a **write** that bypasses the existing `set_tp_dict` — the one genuine style
  inconsistency.
- `:7020-7022` `type_dealloc`, `:7211-7214` `type_traverse`, `:7273` `type_clear` — heap-type-only,
  asserted.

**Category (b) — unconverted reads that would be wrong for a static builtin in a subinterpreter:
ZERO.** Completion **79/84 = 94.0 %** on reads excluding accessor internals; **79/85 = 92.9 %**
including the bypassed write. **The migration is functionally complete.** The only latent risk is that
the two plain `tp_mro` reads would silently break if anyone re-attempted gh-103912 — and one of them
explicitly invites the other to copy it.

**Rest of the tree:** 21 direct reads outside this file — `Objects/` (other than typeobject.c) and
`Python/` are **completely clean**; all 21 are in `Modules/` (8 deliberate `_testcapi` white-box pokes,
12 extension modules writing into a heap type's dict at module init, 1 generated table entry). None is
category (b).

## 4.3 Phase-5 CPython-specific analyses — **not applicable at this scope**, stated rather than padded

Argument Clinic migration completeness, `PyModule_AddObject` → `PyModule_AddObjectRef`, stable-ABI
progress and `module_families` are all `Modules/`-shaped. On a single-file `Objects/` scope they yield
zero. The one exception, checked: `Objects/typeobject.c` contains no `PyModule_AddObject` call sites
and its Clinic coverage is not a mixed-state migration. Effort redirected to §1 and §4.1, per the
agent's own gating rule.

---

# 5. CONSOLIDATED PRIOR-ART CHECK

All queries use `gh api -X GET search/issues -f q='repo:python/cpython …'` (`gh search issues`
silently returns nothing in this environment).

| Finding | Query terms | Result |
|---|---|---|
| **H-1** `recurse_down_subclasses` | `recurse_down_subclasses` | **3 hits.** **gh-151377 [open]** names the function and the `PyDict_Next` loop as a *cross-thread* race — **partial prior art, must be cited**. gh-30775 [closed **unmerged**] is the never-landed fix. GH-30795 is what landed instead. |
| **H-2** `_Py_dict_lookup` / `LOCK_KEYS` | 4 forms | **0 novel.** Only the gh-150490/151593/152200/152238 cycle, all closed |
| **H-3** tracemalloc `TABLES_LOCK` | 2 forms | 7 closed locking issues (gh-128679/139116/144763). **None covers `PyObject_Hash` re-entrancy** |
| **H-4** `HEAD_LOCK` deadlock | 2 forms | **gh-84139 [closed `not_planned`]** — same function, same lock. Closed 2022 *for lack of a reproducer* |
| **H-5** `PyObject_CallFinalizer` / `tp_name` | 2 forms | **0 hits** |
| §2 `PyType_IsSubtype` | 1 form | gh-117306 [closed `completed`] — **mis-closed**, cites the introducing commit. Code now safe by a different mechanism |
| §4.1 #3 `compatible_for_assignment` | covered by P2-F3/F4 searches | 0 hits (7 query forms across agents) |
| §4.1 #4 `super_descr_get` TOCTOU | `supercheck` forms | 0 hits |

**Umbrella-issue overlap — pass 1's conclusion re-verified, and it still holds.**

- **gh-151763** (OOM, open) — body inspected. Its only `Objects/typeobject.c` references are
  **OOM-0033** (`PyType_IsSubtype:2931`, marked SUPERSEDED as a dup of OOM-0036 → gh-151818),
  **OOM-0008** (`_PyType_LookupStackRefAndVersion:6343`, marked **FIXED** via gh-151905), and
  **OOM-0023** (`subtype_dealloc:2719`, unreported). **None is a pass-2 finding.** The one adjacency
  worth noting: OOM-0023 is in `subtype_dealloc`, which is P2-F4's *guarded twin* — adjacent function,
  different defect (exception clearing on the dealloc path).
- **gh-153852** (TSan, open) — no `Objects/typeobject.c` site appears in the body at all. **Zero
  overlap** with P2-C2 / P2-F18 (`tp_watched`) or P2-F17 (`type_ready` publication).

**Conclusion: no pass-2 finding is covered by either umbrella.** The only genuine prior-art collisions
in this whole run are gh-151377 (partial, H-1) and gh-84139 (closed-not-planned, H-4).

---

# 6. SWEEP COMPLETENESS — bounded negatives, including the shapes that came back clean

Stated per shape, including the clean ones, because a bounded negative converts a search into a verdict.

- **Pattern A, narrow (`Py_SET_TYPE`)**: exists at **11 call sites tree-wide** (7 production, 4 test
  scaffolding) plus the `Include/object.h` definition. **10 of 11** install a type onto an object that
  has no type yet — fresh allocation, static type at interpreter init, or module-init metatype fixup —
  and capture no prior type at all. **Only `Objects/typeobject.c:7790` (P2-F4)** shows
  capture-across-Python-then-release. **No new instances.**
- **Pattern A, generalized (capture `Py_TYPE(obj)`, run Python, use the capture)**: **124** declared-local
  captures in `Objects/` + `Python/`; 41 are generated duplicates of `Python/bytecodes.c`, leaving **83
  hand-written**. Exactly **4 release a reference** on the captured type — `typeobject.c:2774` and
  `:2883` carry the explicit re-fetch guard (*"Extract the type again; `tp_del` may have changed it"*),
  `object.c:2112` is balanced against its own `_Py_INCREF_TYPE`, and **only `typeobject.c:7834` does
  not**. A further **6 merely read** the capture after a Python-invoking call: **2 guarded**
  (`object.c:3308`, `:2037`), **4 not** — `object.c:578`, `:1662`, `:1764`, `:1896` (§H-5). The
  remaining 73 were read and ruled SAFE with a named mitigating factor.
- **Pattern B (borrowed `tp_subclasses` walked with a live `PyDict_Next` cursor)**: exists at **5 sites
  tree-wide**, all in `Objects/typeobject.c`. **3 carry the guard** — `:793` (comment + provably no
  Python-invoking call), `:6892` (`#ifndef NDEBUG`, finalization-only), `:9756` (no Python-invoking
  call). **2 do not**: `:1201` (P2-F9, confirmed) and **`:12377` (§H-1, net new)**.
- **Pattern C (arbitrary Python under a `_Py_LOCK_DONT_DETACH` mutex)**: **9 lock families tree-wide.
  5 provably cannot reach Python under the lock** — `MEMERRORS_LOCK` (freelist pointer surgery; the one
  `BaseException_clear` is deliberately hoisted to `exceptions.c:4171`), `uniqueid.c` `LOCK_POOL`
  (`PyMem_Realloc` + atomics that never drop a refcount to zero), `index_pool.c` `LOCK_POOL`
  (`PyErr_NoMemory` deliberately placed *after* `UNLOCK_POOL` at `:166-167`), the **weakref striped
  lock** (callback invocation deliberately deferred to `weakrefobject.c:1084-1090`, *after*
  `UNLOCK_WEAKREFS`; both callback decrefs outside — answering the specific question, **no weakref
  callback runs under the lock**), and the dict `watcher_mutex` (dispatch happens lock-free in
  `_PyDict_SendEvent`, which is the correct discipline). **1 more** (`Python/thread.c:120`) is a
  caller-owned primitive whose every in-tree blocking caller wraps it or holds only pure C. **Only 3
  can reach Python**: `LOCK_KEYS` (P2-F6 + **§H-2 new**), `TABLES_LOCK` (**§H-3 new**), `HEAD_LOCK`
  (**§H-4**). Lock ordering checked tree-wide: no site requests a stop-the-world while holding
  `HEAD_LOCK`; **the one order inversion found anywhere is §H-2's `LOCK_KEYS`-before-`types.mutex`.**
- **`_PyCStackRef` migration**: **201** target-field accesses, **80 functions** hand-triaged, **14**
  pinning sites present. `tp_dict`'s structural immunity clears **31** in one stroke. **9 candidates**
  remain, of which 2 are already-known and **4 are net new** (§4.1 #1, #3, #4, #5).
- **`lookup_tp_*` migration**: **79/84 reads converted (94.0 %)**; **0** sites in the wrong-for-a-
  static-builtin category. **Migration complete** — a clean negative with the denominator stated.
- **"Defence deleted" query**: **1,282** commits scanned, **174** with a net defence deletion, **97**
  since 2015, **8** hand-verified against HEAD. **2 still-standing deletions have live consequences**
  (P2-F4; and P2-C3's blame origin), **1 was relanded**, **1 was later re-covered by a different
  mechanism** (`PyType_IsSubtype`), **4 were compensated at the time**.

---

# 7. CHURN-RISK MATRIX

Density-first per the agent's own rule (raw churn is anti-correlated with defect density on
`Objects/`). Recency-weighted, then adjusted for what this pass actually found. Full per-function
density table is in the preflight §1b and is not repeated.

| # | File / Function | 3y crash-fix /KLOC | Risk factors found this pass | Risk | Action |
|--:|---|---:|---|---|---|
| 1 | `typeobject.c:_PyType_Modified_Unlocked`:1166-1240 | 93.3 (7/3y) | P2-F8 + P2-F9 + P2-F6; newly exported 19d ago; twice-failed path | **HIGHEST** | File; needs the H-1 fix too |
| 2 | `dictobject.c:_Py_dict_lookup` + `insert_split_key` | — (new) | P2-F6 **and §H-2**; the only lock-order inversion in the tree | **HIGHEST** | Immediate — 2 defects, 1 lock |
| 3 | `typeobject.c:recurse_down_subclasses`:12362 | — | **§H-1**; fix written 2022, closed unmerged; 2 lanes converged | **HIGHEST** | Comment on gh-151377 |
| 4 | `typeobject.c:same_slots_added`/`compatible_for_assignment`:7587-7695 | — | P2-F3 + P2-F4 + **§4.1 #3** — *three* defects on one `RichCompareBool` | **HIGH** | Fix all three together |
| 5 | `typeobject.c:_PyType_LookupStackRefAndVersion`:6306 | 86.4 (7/3y, 10 lifetime) | P2-F15; rewritten twice for perf in 2026 | **HIGH** | Schedule |
| 6 | `typeobject.c:object_set_class[_world_stopped]`:7700-7839 | 125.0 / 40.8 | P2-F3/F4; §2 defence deletion; STW made conditional 141d ago | **HIGH** | Fix with #4 |
| 7 | `Python/tracemalloc.c` `TABLES_LOCK` regions | — (new) | **§H-3**; hazard known and fixed once, not swept | **HIGH** | File separately |
| 8 | `typeobject.c:type_setattro`:6750 | 72.9 (7/3y, **2 in 12mo**) | P2-F10, P2-F2 upstream | **HIGH** | Schedule |
| 9 | `Python/pystate.c:_PyInterpreterState_DeleteExceptMain`:1088 | — | **§H-4**; gh-84139 closed for want of a reproducer | MODERATE | Write the reproducer |
| 10 | `typeobject.c:find_name_in_mro`:6145 | 117.6 (6/3y, 9 lifetime) | P2-F15 composite; return convention changed 137d ago | MODERATE | Monitor |

Below the cut but not cleared: `Objects/object.c` `PyObject_CallFinalizer` / the `tp_name` family
(§H-5) — low density, but four unguarded siblings of a debug-only twin.

---

# 8. HISTORY ANALYSIS SUMMARY

- **Fix commits analyzed**: 1,282 (full file history) + 4 named provenance commits read in full
- **Ledger sites blamed and classified**: 30
- **Incomplete fixes / propagation gaps found**: **5** (H-1 … H-5)
- **Net-new FIX candidates**: **3** (H-1, H-2, H-3) · **Net-new CONSIDER**: **2** (H-4, H-5) + **4**
  `_PyCStackRef` sites (§4.1 #3, #4, #5, and #1 = H-1)
- **Bug shapes swept to a bounded verdict**: **6** (Patterns A-narrow, A-general, B, C, `_PyCStackRef`,
  `lookup_tp_*`)
- **Defence-deleted commits found**: 174 · **verified against HEAD**: 8 · **with live consequences**: 2
- **Module-family gaps**: N/A at this scope (single `Objects/` file) — stated, not padded
- **Prior-art collisions**: **2** (gh-151377 partial; gh-84139 closed-not-planned). **0** overlap with
  gh-151763 / gh-153852 — pass 1's conclusion re-verified with specifics
- **Fix-to-feature ratio, pass-2 regions, 3y**: 69 crash-shaped fixes vs 32 total commits since
  2025-01-01 — **fix-dominated**
- **Churn concentration**: **Focused.** Three commits — `3a4c15bb9815` (2022 cleanup), `fbbbc10055e0`
  (gh-127266 reland), `20921922fa4d` (gh-150490 take-2) — account for 9 of the 30 blamed sites.

**The one-sentence answer to the run's motivating question.** Code this heavily reviewed still holds
these defects because the reviews were real but *adjacent*: eight sites were last touched by a genuine
audit of a neighbouring defect class (most sharply P2-F1b, where a commit whose entire purpose was
fixing borrowed references fixed the borrowed **return value** and left the borrowed **arguments**),
six by explicit performance or cleanup work naming no defect class at all — and in the single most
actionable case, **the correct fix was written, reviewed, and closed unmerged in 2022**.

---

## Artifacts

- `…/scratchpad/hist/defence_deleted.py` · `defence_deleted.json` — the §2 query, re-runnable
- `…/scratchpad/hist/blame_findings.py` — the §3 cross-reference

## Proposed toolkit feedback (for `TOOLKIT_DEFECTS.md`)

1. **`analyze_history.py` has no "defence deleted" mode.** §2 required a bespoke script and produced
   two live findings plus the P2-C3 blame origin. Proposal: add `--deleted-guards` implementing the
   net-guard-deletion diff scoring, with the reason-comment heuristic (deleted comments matching
   *"may have changed / can be replaced / re-fetch / underneath us / re-entrant / must not"*) weighted
   3×. This is a genuinely novel temporal query and it is cheap.
2. **`--introduced-by` answers "who wrote this line", not "who last audited this region for what".**
   The §3 table — blame + subject + defect-class classification of the subject + file count — is what
   actually produced the insight, and it is mechanical. Proposal: `--audit-context FILE:LINE`.
3. **Sweep-completeness reporting should be a first-class output.** The six bounded negatives in §6
   were assembled by hand from three subagent reports. The script knows the denominators.
