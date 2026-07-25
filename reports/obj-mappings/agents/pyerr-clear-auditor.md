# pyerr-clear-auditor — obj-mappings slice

Target: `/home/danzin/projects/cpython` @ `4f3be1b5777`
Scope: `Objects/dictobject.c` (8,597 lines) + `Objects/setobject.c` (3,228 lines) — exactly these two.
Builds: `~/projects/python_build_matrix/builds/*` @ `a1d580430c8`.
**Both cited files are byte-identical between the build commit and the target ref**
(`git diff --stat a1d580430c8 4f3be1b5777 -- Objects/dictobject.c Objects/setobject.c` → empty).

---

## 1. Denominator

| quantity | value | source |
|---|---|---|
| `PyErr_Clear`-family call sites in slice | **6** (dict 2, set 4) | `total_pyerr_clear_calls` |
| …raw grep reconciliation | **6** (`grep -c PyErr_Clear`: 2 + 4) | matches — **no AST-attribution gap** |
| …in the destructor family | **0** | `total_pyerr_clear_calls_in_destructors` |
| destructor-family functions | **12** (dict 7, set 5) | `destructor_functions` |
| functions analyzed | 404 | `functions_analyzed` |
| **scanner findings** | **0** | `scan_pyerr_clear.sample.json` |
| findings after hand reading | **4** (1 FIX, 3 CONSIDER) | this report |

The `grep` pattern `PyErr_Clear` is a superset of `_PyErr_Clear`; there are **zero** `_PyErr_Clear(tstate)`
call sites in either file, so nothing is hiding behind the private spelling.

I enumerated the 12 destructors by hand and they match the scanner's count exactly:
dict — `dict_dealloc:3648`, `dict_traverse:5089`, `dict_tp_clear:5121`, `dictiter_dealloc:5658`,
`dictiter_traverse:5669`, `dictview_dealloc:6433`, `dictview_traverse:6443`;
set — `set_clear_internal:646`, `set_dealloc:734`, `set_traverse:937`, `setiter_dealloc:1040`,
`setiter_traverse:1050`.

### Is the zero structural or evidential?

**Evidential for the destructor rule, structural for the other two.**

- **Destructor rule** (`pyerr_clear_in_dealloc`): denominator 12 destructors, 0 of which contain a clear.
  This zero is *earned*. I read all 12; every one of them is `Py_DECREF`/`Py_VISIT`-only. Confirmed clean.
- **Success-path rule** (`pyerr_clear_on_success_path`): **structurally suppressed**. Both dict clears
  sit inside an `if (<something> == NULL/-1)` branch, which the rule's dominance gate reads as "an
  error test", so the rule cannot fire. See §4 recall gap A — one of the two is a real instance.
- **Unfiltered-after-Python-call rule**: **structurally suppressed**. All four set clears are preceded
  by `PyErr_ExceptionMatches(PyExc_TypeError)`, so the narrowing gate suppresses them. See §4 recall
  gap B — two of the four narrowings are unsound, and that is the FIX in this report.

### Why `scan_pyerr_clear` = 0 next to `scan_error_paths` = 2 on the same lines

`scan_error_paths`'s `unconditional_pyerr_clear` fired on `dictobject.c:1336` and `:7269` — exactly the
two clears my own rule could not see. The gates differ: `scan_error_paths` asks "is this clear
unconditional *within its basic block*", `scan_pyerr_clear` asks "does an error test *dominate* it".
An `if (p == NULL)` after `PyMem_Malloc` satisfies the second and not the first. That divergence is the
recall signal, and it is correct-in-favour-of-`scan_error_paths` here: `:7269` is a real vestigial clear
and `:1336` is a dead branch. Deduped by `(file,line)`, both sites appear once below.

---

## 2. Findings

### [FIX] `set.remove()` / `set.discard()` clear a user `__eq__` exception, not just the hash error — `Objects/setobject.c:2660` (`set_remove_impl`), `Objects/setobject.c:2700` (`set_discard_impl`)

**Mechanism.** Both call `set_discard_key(so, key)` (`:2656`, `:2696`) and then, on `-1`, narrow with
`if (!PySet_Check(key) || !PyErr_ExceptionMatches(PyExc_TypeError)) return NULL;` before
`PyErr_Clear()`. But `set_discard_key` (`:623-631`) bundles **two** fallible steps into one `-1`:

```c
static int
set_discard_key(PySetObject *so, PyObject *key)
{
    Py_hash_t hash = PyObject_Hash(key);        /* producer 1: user __hash__ */
    if (hash == -1) { set_unhashable_type(key); return -1; }
    return set_discard_entry(so, key, hash);    /* producer 2: user __eq__ via
                                                   PyObject_RichCompareBool */
}
```

The narrowing separates *nothing*: a `TypeError` raised by an element's `__eq__` during the probe is
indistinguishable from the `TypeError` `set_unhashable_type` manufactures for an unhashable set key.
A **set subclass that defines `__hash__`** satisfies `PySet_Check`, hashes successfully, reaches the
comparison, and lands on the clear with the user's live exception. The clear then discards it, the code
re-probes with `frozenset_hash_impl(key)` — a *different* bucket — finds nothing, and
`set.discard()` returns `None` while `set.remove()` raises `KeyError`. The user's exception is gone and
`__context__` is `None`.

**Guarded twin — and what its guard actually defends.** `_PySet_Contains` (`:2554-2574`) is the same
five lines with the hash step hoisted out:

```c
    Py_hash_t hash = PyObject_Hash(key);
    if (hash == -1) {
        if (!PySet_Check(key) || !PyErr_ExceptionMatches(PyExc_TypeError)) { ... }
        PyErr_Clear();                       /* provably clears only the HASH error */
        ...
    }
    return set_contains_entry(so, key, hash);
```

Its `PyErr_Clear()` is dominated by `hash == -1`, i.e. by the failure of *one* named call whose only
possible error is the one being cleared. That is what makes it sound — **not** the
`PyErr_ExceptionMatches` narrowing, which is identical in all four sites. `frozenset.__contains__`
(`:2619-2629`) has the same hoisted shape. So the file already contains the fix twice; `remove`/`discard`
are the two sites that did not get it. All four were introduced together in `2408a8a22bd`
(gh-121795 / #121796) — this is a same-commit asymmetry, not an incomplete follow-up.

**Prescribed fix.** Hoist the hash out of `set_discard_key` in both impls, exactly as `_PySet_Contains`
does, and call `set_discard_entry` directly:

```c
    Py_hash_t hash = PyObject_Hash(key);
    if (hash == -1) {
        if (!PySet_Check(key) || !PyErr_ExceptionMatches(PyExc_TypeError)) return NULL;
        PyErr_Clear();
        Py_BEGIN_CRITICAL_SECTION(key);
        hash = frozenset_hash_impl(key);
        Py_END_CRITICAL_SECTION();
    }
    rv = set_discard_entry(so, key, hash);
    if (rv < 0) return NULL;                 /* comparison errors now propagate */
```

**Reproduction.** `repro/set_discard_swallows_eq_typeerror.py`

- `release-gil-nojit`: **5/5** — `set.discard` returns `None`, `set.remove` raises `KeyError`, both
  swallowing `TypeError("boom from __eq__")`.
- `debug-gil-nojit`: **5/5** — identical.
- Baseline in the same run: the guarded twin (`x in s`) propagates the `TypeError` correctly.

Not a data-loss or memory-safety bug — a wrong-but-valid result with a destroyed user exception. It is
the `Objects/unionobject.c:172` / CPY-0006 shape with a *partial* narrowing rather than none.

No prior art: `gh api -X GET search/issues -f q='repo:python/cpython set.discard PyErr_Clear TypeError __eq__'`
→ `total_count: 0`; nothing in the findings repo mentions `set_discard_impl` / `set_remove_impl`.

---

### [CONSIDER] `insert_to_emptydict` runs Python between allocating the keys and publishing them → `ma_used`/`dk_nentries` desync → OOB read handed to Python as a dict key — `Objects/dictobject.c:2103`

**Mechanism.** `insert_to_emptydict` (`:2089-2131`) allocates `newkeys` at `:2097`, fires
`_PyDict_NotifyEvent(PyDict_EVENT_ADDED, ...)` at `:2103`, and publishes `mp->ma_keys = newkeys`
only at `:2129`. The comment at `:2124-2128` explains the late publish:

> *"We store the keys last so no one can see them in a partially inconsistent state…"*

That reasoning is about **another thread** (it goes on to discuss the non-owner-thread case). It does
not cover **re-entrancy on this thread**: because `ma_keys` is still `Py_EMPTY_KEYS` while `:2103` runs
Python, a re-entrant insert takes the *same* `insert_to_emptydict` path, builds and publishes its own
keys object, and sets `ma_used = 1`. The outer frame then does `STORE_USED(mp, ma_used + 1)` → 2, and
overwrites `ma_keys` with its own **one**-entry table at `:2129`. The inner keys object and its strong
references leak, and the dict now reports `ma_used == 2` over a table with `dk_nentries == 1`.
Every consumer that trusts `ma_used` — iteration, `dictresize`, `dict_dealloc` — reads one slot past
the end of the entries array and treats whatever is there as a `PyObject *`.

**Guarded twin.** `insert_combined_dict` re-reads `mp->ma_keys` from the object on every access rather
than caching it in a local, which is why the *resize*-during-callback case there does not produce a
dangling keys pointer. `insert_to_emptydict` caches `newkeys` in a local across the notify and cannot
do the same, because its keys object is not yet reachable from `mp`. The minimal fix is to move the
notify to after `:2129`, or to re-check `mp->ma_keys == Py_EMPTY_KEYS` when it returns.

**Why CONSIDER, not FIX.** The notify only runs Python if a registered `PyDict_WatchCallback` does,
and `Doc/c-api/dict.rst:582-584` explicitly forbids that:
*"The callback may inspect but must not modify dict… **Do not trigger Python code execution in the
callback**, as it could modify the dict as a side effect."* The two in-tree non-test watchers
(`Python/pylifecycle.c:1570 builtins_dict_watcher`, the JIT globals watcher in
`Python/optimizer_analysis.c:137`) do not run Python. So the crash is gated on a C extension violating
a documented contract — **except that CPython's own `_testcapi` watcher violates it** (see §5), which
is what my reproducer uses. The guard here is documentation only; the failure mode it produces is
memory-unsafe, not the "unpredictable effects, including infinite recursion" the doc warns about.

**Reproduction.** `repro/dict_insert_to_emptydict_notify_reentry.py`

- `debug-gil-nojit`: **5/5 SIGSEGV** (rc=139), after printing `len(d) = 2`.
- `release-gil-nojit`: silent corruption — `len(d) == 2`, and
  `list(d.keys())` returns `['outer', <class 'str'>]`: the second "key" is a pointer read out of bounds.
  `d == dict(d)` is `False`.
- `release-gil-nojit-asan`: `AddressSanitizer: SEGV … caused by a READ memory access`, top frame
  `Py_INCREF Include/refcount.h:286`, `rdi = 0xbebebebebebebebe` — i.e. the dict handed Python a
  pointer read out of **uninitialized** heap.

---

### [CONSIDER] `insert_combined_dict` fires the ADDED notify between the capacity check and the entry write → heap-buffer-overflow WRITE — `Objects/dictobject.c:1917`

**Mechanism.** Same window, different site and different consequence:

```c
    if (mp->ma_keys->dk_usable <= 0) {                            /* :1910 capacity secured */
        if (insertion_resize(mp, 1) < 0) return -1;
    }
    _PyDict_NotifyEvent(PyDict_EVENT_ADDED, mp, key, value);      /* :1917 runs Python */
    Py_ssize_t hashpos = find_empty_slot(mp->ma_keys, hash);      /* :1920 */
    ep = &DK_UNICODE_ENTRIES(mp->ma_keys)[mp->ma_keys->dk_nentries];  /* :1925 */
    STORE_KEY(ep, key); STORE_VALUE(ep, value);
    STORE_KEYS_USABLE(mp->ma_keys, mp->ma_keys->dk_usable - 1);    /* :1936 */
    assert(mp->ma_keys->dk_usable >= 0);                          /* :1938 */
```

A re-entrant insert from the notify that consumes the **last** usable slot (without itself triggering a
resize) leaves `dk_usable == 0` and `dk_nentries == capacity`. The outer frame then writes at
`DK_UNICODE_ENTRIES[capacity]` — one entry past the array — and drives `dk_usable` to `-1`.
The capacity check at `:1910` is not re-validated after `:1917`.

**Guarded twin.** `insert_combined_dict` itself, one branch up: the gh-140551 comment at `:1902-1908`
already acknowledges that *"dict was cleared in `_Py_dict_lookup`"* and **re-resizes** to repair the
invariant. That is the same class of re-entrancy, recognised and repaired for the key-kind invariant,
and not for the `dk_usable` invariant seven lines below. The fix is the same shape: re-test
`dk_usable <= 0` after the notify (or move the notify after the write).

**Why CONSIDER.** Same documented-contract gate as the previous finding.

**Reproduction.** `repro/dict_insert_combined_notify_reentry.py` (sweeps prefill 0..12 in subprocesses;
crashes at prefill 0 — that is the `insert_to_emptydict` finding above — and at prefill **4** and **9**,
the two last-usable-slot boundaries).

- `debug-gil-nojit`, prefill=4: **5/5 SIGABRT** (rc=134) —
  `Objects/dictobject.c:1938: int insert_combined_dict(...): Assertion 'mp->ma_keys->dk_usable >= 0' failed`
- `release-gil-nojit`, prefill=4: **SIGSEGV** (rc=139).
- `release-gil-nojit-asan`, prefill=4: `AddressSanitizer: heap-buffer-overflow`, **WRITE of size 8**,
  the address `located 0 bytes after` the keys allocation — i.e. exactly one entry past the end:

  ```
  #0 insert_combined_dict Objects/dictobject.c:1926:9      <- STORE_KEY(ep, key)
  #1 insertdict           Objects/dictobject.c:2051:13
  0x73f05e21b7b8 is located 0 bytes after 120-byte region [0x73f05e21b740,0x73f05e21b7b8)
  allocated by thread T0 here:
  #1 new_keys_object      Objects/dictobject.c:860:14
  #2 insert_to_emptydict  Objects/dictobject.c:2097:33
  ```

**Relation to the inherited CPY-0096 work.** The RUN_CONTEXT says CPY-0096's three further consequences
("an ASan heap-buffer-overflow, a `dk_usable` invariant break 5/5 from a single re-entry, and unbounded
recursion → SIGSEGV") reproduce on the default GIL build and belong to this slice. These two findings
are that shape at **two sibling entry points** CPY-0096 does not name — `insert_combined_dict:1917` and
`insert_to_emptydict:2103` rather than `insert_split_key:1971` — reached through the **dict** watcher
rather than the **type** watcher. CPY-0096's own entry point (`_PyType_Modified_Unlocked` →
`sys.unraisablehook`) needs no C extension at all, which is why that one is FIX and these are CONSIDER.
I did not re-litigate CPY-0096 itself.

---

### [CONSIDER] `_PyDict_NewKeysForClass`'s `PyErr_Clear()` is vestigial — it can only discard a caller's exception — `Objects/dictobject.c:7269`

**Mechanism.** This is the gh-146102 / `mutablemapping_add_pairs` shape, created by an allocator swap:

```c
/* Returns NULL if cannot allocate a new PyDictKeysObject,
   but does not set an error */                                   /* :7256-7257 */
...
    struct _instancekeysobject *shared_keys = PyMem_Malloc(...);  /* :7264 */
    if (shared_keys == NULL) {
        PyErr_Clear();                                            /* :7269 */
        return NULL;
    }
```

`PyMem_Malloc` **sets no Python exception**. So no path reaching `:7269` can have an exception this
function raised; the only exception the clear can destroy is the caller's. Git archaeology makes the
regression explicit — before `efb2fffae1f` (gh-150490) the allocation was
`new_keys_object(NEXT_LOG2_SHARED_KEYS_MAX_SIZE, 1)`, whose failure path *does* call `PyErr_NoMemory()`
(`Objects/dictobject.c`, `new_keys_object`), and the `PyErr_Clear()` was exactly right there — it
discharged the "does not set an error" contract in the header comment. The commit swapped in the raw
`PyMem_Malloc` and kept the clear, which no longer has anything of its own to clear.

**Guarded twin / prescribed fix.** The upstream sweep's mechanical remedy: **delete the clear, add
`assert(!PyErr_Occurred());`**. That idiom already appears in this very file at `:3620`
(`dict_fromkeys` success path) and `:3625`, so its maintainers accept it. The header comment at
`:7256-7257` stays true without the clear.

**Why CONSIDER rather than FIX.** The sole caller is `Objects/typeobject.c:9480`
(`type_ready_managed_dict`), which immediately does `PyErr_NoMemory(); return -1;` — so the function's
own contract is unaffected either way, and I could not construct an ordinary-Python path that reaches
`PyType_Ready` with a live exception *and* fails this allocation. The finding stands on the static
argument (a clear that provably cannot be clearing its own error) rather than on a reproduction.

**Reproduction.** None attempted — reaching it needs OOM injection targeted at one `PyMem_Malloc` during
type readying. Stated as static-only, not claimed as reproduced.

---

### [POLICY] `dict_getitem` suppresses lookup errors, correctly bracketed — `Objects/dictobject.c:2425-2467`

The 10-line comment at `:2415-2424` documents the historical contract. The implementation is the
textbook idiom: `_PyErr_GetRaisedException` at `:2450`, the lookup, a non-`KeyError` check plus
`PyErr_FormatUnraisable` at `:2459-2462`, `_PyErr_SetRaisedException` at `:2463`. There is no early
return between the fetch and the restore, so the restore happens on every path out. `PyDict_GetItem`
(`:2470`) and `PyDict_GetItemString` (`:5546`) both route through it and both name the modern
replacement in the unraisable message. Correct as designed.

### [POLICY] `dict_unhashable_type` / `set_unhashable_type` — `Objects/dictobject.c:2477-2496`, `Objects/setobject.c:345-359`

Both fetch, test `Py_IS_TYPE(exc, PyExc_TypeError)` (exact type — a `TypeError` *subclass* is restored
untouched, which is the conservative direction), and on the exact-match path reformat with the original
embedded via `%S` before `Py_DECREF(exc)`. Neither leaks the fetched exception nor leaves the state
unset. The original traceback is lost, which is the CPY-0078 flavour, but the message is preserved and
the narrowing is exact. Deliberate UX behaviour, correctly implemented.

### [POLICY] `_PyDict_SendEvent`'s unraisable report is a clear site reached from `dict_dealloc` and `dict_tp_clear` — `Objects/dictobject.c:8309-8317`

```c
    if (cb && (cb(event, (PyObject*)mp, key, value) < 0)) {
        PyErr_FormatUnraisable("Exception ignored in %s watcher callback for <dict at %p>", ...);
    }
```

`PyErr_FormatUnraisable` is **not** exception-neutral: `format_unraisable_v`
(`Python/errors.c:1662-1769`) fetches at `:1671` and ends with an unconditional
`_PyErr_Clear(tstate); /* Just in case */` at `:1768`. Reached from `dict_dealloc:3652`
(`PyDict_EVENT_DEALLOCATED`) and from `dict_tp_clear:5121` → `PyDict_Clear` → `:3142`
(`PyDict_EVENT_CLEARED`), both of which run at arbitrary points including mid-unwind, so a callback
that returns `-1` there destroys the in-flight exception.

**This is POLICY because the obligation is documented on the callback side**, `Doc/c-api/dict.rst:600-605`:
*"There may already be a pending exception set on entry to the callback… the callback may not call any
other API that can set an exception unless it saves and clears the exception state first, and restores
it before returning."* A conforming callback returns `0` and never reaches the report. Note what that
guard actually defends against, though: it binds *third-party* callback authors, not CPython, and
CPython's own `_testcapi` watcher does not honour it (§5).

### [ACCEPTABLE] `_PyDictKeys_StringLookupSplit`'s clear is in a dead branch — `Objects/dictobject.c:1336`

```c
    Py_hash_t hash = unicode_get_hash(key);
    if (hash == -1) {
        hash = PyUnicode_Type.tp_hash(key);
        if (hash == -1) { PyErr_Clear(); return DKIX_ERROR; }
    }
```

`PyUnicode_Type.tp_hash` is a **statically known** slot, `unicode_hash`
(`Objects/unicodeobject.c:12047-12063`): it returns the cached hash or computes `Py_HashBuffer`, and
has no failure path — it cannot return `-1` and cannot run user code. The inner branch is unreachable,
so the clear can never execute. `assert(PyUnicode_CheckExact(key))` at `:1331` closes the type question.
Introduced defensively in `1b15c89a17c`. Harmless; the *only* reason it is worth a line is that the
`hash == -1` dominance is what suppressed both scanner rules (§4).

### [ACCEPTABLE] `_PySet_Contains:2565` and `frozenset___contains___impl:2625`

The two sound siblings of the FIX above. Both hoist `PyObject_Hash` out (`:2559`, `:2619`) so the
`hash == -1` branch that dominates the clear has exactly one possible producer. Correct.

---

## 3. Classes bounded

| class | denominator | result |
|---|---|---|
| clears in `tp_dealloc`/`tp_clear`/`tp_finalize`/`tp_traverse` | 12 destructors, all read | **clean** — 0 clears, 0 fallible calls other than `Py_DECREF`/`Py_VISIT`/`PyDict_Clear` |
| `PyErr_Fetch`/`GetRaisedException` pairs — restore on every path out | 3 sites (`dictobject.c:2450`, `:2480`, `setobject.c:348`) | **clean** — each is a single linear path; the two `*_unhashable_type` helpers consume the fetched exception on the reformat path and restore it on the other |
| clears on a provable success path | 6 clears examined | **1 hit** (`dictobject.c:7269`) |
| unfiltered clears after a call into user Python | 6 clears examined; 4 carry a `PyErr_ExceptionMatches` narrowing | **2 hits** (`setobject.c:2660`, `:2700`) — the narrowing is present but does not discriminate |
| "lookup failure converted to *not found* by clearing" | all 24 `DKIX_ERROR` sites in `dictobject.c`; both set lookup families (`set_lookkey`, `set_lookkey_threadsafe`, `set_do_lookup`) | **clean in dict** — every `ix == DKIX_ERROR` propagates; the `PyErr_Occurred()`-after-`PyIter_Next` sites (`:3819`, `:4118`, `:4384`, `:4708`, `:6520`, `:6759`, `:6816`, `:6976`) are the standard iterator-exhaustion idiom, not suppression. **2 hits in set** — the FIX above is exactly this shape (`rv < 0` → clear → re-probe → `NOTFOUND`) |
| `_PyErr_Clear(tstate)` private spelling | 0 sites | structural zero, stated as such |

**State-consistency under a raising user `__eq__` (the adjacent shape I was asked to read).**

- `set_add_entry_takeref` (`:252-335`) is the **guarded twin** for the whole file: it caches
  `table = so->table` before `PyObject_RichCompareBool` (`:286-288`), `Py_INCREF`s `startkey` across
  it, and restarts the probe if `table != so->table || entry->key != startkey` (`:294-295`).
  `set_compare_entry_lock_held` (`:137-165`) does the same and returns `SET_LOOKKEY_CHANGED`, which
  `set_lookkey` loops on (`:422-427`). `so->used` and `so->fill` are only bumped after the last
  fallible step, at `:314-323` — no premature accounting found.
- `set_compare_frozenset` (`:171-193`) deliberately drops both the `Py_INCREF` and the restart check,
  with the comment *"we don't need to check if the set has changed"*. **What that reasoning actually
  defends:** frozensets are immutable *from Python*, and `PySet_Add` gates its frozenset arm on
  `_PyObject_IsUniquelyReferenced` (`:3118-3122`) so an already-exposed frozenset cannot be mutated
  through the public C API. It does **not** cover `_PySet_ClearInternal` (`:3069-3073`), which has no
  such gate. No in-slice caller violates this; flagged only so the assumption is written down.
- Dict: `insertdict` (`:2018-2085`) does `STORE_USED(mp, ma_used + 1)` only *after*
  `insert_combined_dict` returns 0 (`:2051-2054`) — correct ordering. `insert_to_emptydict` does not
  (`:2121` precedes the publish at `:2129`), which is the CONSIDER above.
  `_PyDict_InsertSplitValue` (`:1995-2008`) takes the reference with `Py_NewRef` *before* the
  `STORE_USED`, and its comment at `:2005-2006` documents having already been bitten by the ordering.

---

## 4. Toolkit feedback

### Recall gap A — the dominance gate treats a raw-allocator NULL test as an "error test" (highest value)

`pyerr_clear_on_success_path` is suppressed by *any* dominating condition that tests an error signal.
But `if (p == NULL)` after `PyMem_Malloc` / `PyMem_Calloc` / `PyMem_Realloc` / `PyMem_RawMalloc` /
`malloc` / `calloc` / `realloc` tests an error in the **value** while setting **no Python exception**.
A clear inside such a branch is still a pure success-path clear in the exception-state sense — which is
precisely how `dictobject.c:7269` regressed when `efb2fffae1f` swapped `new_keys_object` (which calls
`PyErr_NoMemory`) for a bare `PyMem_Malloc` and left the clear behind.

**Proposal.** Add a closed list of non-exception-setting allocators. When the *only* dominating
condition is a NULL test on a value assigned from one of them, do not credit it as an error test —
emit `pyerr_clear_on_success_path` at `medium`. The allocator list is small and closed, so the added
population tree-wide should be tiny. This recovers `:7269` mechanically and is the exact inverse of
the `alloc_null_no_memerror` rule `scan_error_paths` already ships (that rule *knows* these allocators
set nothing — the two scanners disagree about the same fact).

### Recall gap B — the narrowing gate assumes narrowing implies discrimination (this is what found the FIX)

The narrowing gate is described as "roughly half of all `Objects/` clears are correctly narrowed with
`PyErr_ExceptionMatches` and are suppressed. If you are tempted to widen the rules, that is the class
you will re-import." That is the right default, but it hides a real sub-class: narrowing is only sound
when the narrowed predicate distinguishes *this function's* expected error from a user error. In
`set_discard_impl` the tested value (`set_discard_key`'s `-1`) has **two** producers, `PyObject_Hash`
and `PyObject_RichCompareBool`, and both can raise the narrowed type.

**Proposal — a new low-noise finding type, not a widening.** Emit
`narrowed_clear_over_multi_producer_failure` at `low` confidence when all of:
1. a `PyErr_ExceptionMatches(E)` narrowing is present (so the existing gate would suppress), **and**
2. the failure being tested comes from a call to a **file-local helper**, **and**
3. that helper's body contains ≥ 2 distinct Python-reaching calls (`PyObject_Hash`,
   `PyObject_RichCompare*`, `PyObject_Call*`, …) that can plausibly set `E`.

Condition 3 is a one-level interprocedural step the toolkit already performs: `scan_error_paths` on
this slice reports `int_status_callees_resolved: 112`. Reusing that resolution keeps the cost near
zero. In this slice the rule would fire on exactly 2 of the 4 narrowed clears and suppress the 2
correct ones (`_PySet_Contains`, `frozenset___contains___impl`) — because in those the tested value is
`PyObject_Hash`'s own return, not a helper's.

### Recall gap C — `PyErr_FormatUnraisable` / `PyErr_WriteUnraisable` are clear sites, and the scanner credits them as guards

The agent instructions say *"`PyErr_WriteUnraisable` / save-restore near a flagged clear ⇒ likely
intentional"*, and the scanner treats a report API in the clear's own branch as a guard. That is
correct for a clear the function itself caused, but `format_unraisable_v`
(`Python/errors.c:1662-1769`) *fetches* at `:1671` and ends with an unconditional
`_PyErr_Clear(tstate)` at `:1768`. So inside the **destructor family** a bare
`PyErr_FormatUnraisable` is itself the thing the destructor rule exists to find: it consumes whatever
exception is live, whether or not this destructor caused it.

**Proposal.** Inside `tp_dealloc` / `tp_clear` / `tp_finalize` (and any function transitively reached
from one), treat `PyErr_WriteUnraisable` / `PyErr_FormatUnraisable` as a **clear site** rather than a
guard, unless a `PyErr_GetRaisedException` / `PyErr_Fetch` positionally brackets it. Slice population:
5 unraisable calls in `dictobject.c` (`:2435`, `:2461`, `:5552`, `:8013`, `:8314`), of which **2 are
destructor-reachable** — `:8314` (via `dict_dealloc:3652` and `dict_tp_clear` → `PyDict_Clear:3142`)
and `:8013` (`PyObject_ClearManagedDict`, called from `subtype_dealloc`/`subtype_clear`). Both are
POLICY here because the contract is documented on the callback side, but the scanner should *surface*
them for that judgement rather than silently credit them.

### Precision and envelope notes

- **Precision on this slice is undefined** (0 findings ⇒ no false positives and no true positives).
  Recall for the class the rule exists to find is **0 of 1** (`:7269`) at the FIX/CONSIDER level, and
  0 of 3 counting the setobject pair.
- The `denominators` block omits the two numbers that actually make this rule's zero auditable.
  **Move `total_pyerr_clear_calls` and `total_pyerr_clear_calls_in_destructors` into `denominators`**
  so the canary is uniform with the other scanners. As shipped, an agent that only reads
  `denominators` sees `findings: 0, destructor_functions: 12` and cannot tell whether 0 or 200 clears
  were examined.
- Dedup with `scan_error_paths` worked as documented: `(file,line)` collision on `:1336` and `:7269`,
  reported once each here.

---

## 5. Noticed outside slice (one line each)

- `Modules/_testcapi/watchers.c:49` — `dict_watch_callback` formats with
  `PyUnicode_FromFormat("new:%S:%S", key, new_value)`; `%S` calls `PyObject_Str`, so CPython's own
  example watcher **triggers Python code execution inside a dict watcher callback**, which
  `Doc/c-api/dict.rst:583-584` explicitly forbids — this is what makes both dict CONSIDERs reachable
  from a Python script.
- `Modules/_testcapi/watchers.c:32-70` — the same callback calls `PyUnicode_FromFormat` and
  `PyList_Append` (both can set exceptions) with no save/restore, violating the *other* half of the
  documented contract at `Doc/c-api/dict.rst:600-605` ("there may already be a pending exception set
  on entry").
- `Python/errors.c:1768` — `format_unraisable_v` ends with an unconditional `_PyErr_Clear(tstate)`,
  so **every** `PyErr_WriteUnraisable` / `PyErr_FormatUnraisable` call site tree-wide is a clear site;
  worth a catalog entry of its own, since the FP taxonomy currently lists these APIs only as guards.
- `Objects/typeobject.c:1223` — `_PyType_Modified_Unlocked`'s `PyErr_FormatUnraisable` is the
  exception-state half of CPY-0096/CPY-0084 and reaches `insert_split_key` (`dictobject.c:1971`)
  through the same window as the two dict CONSIDERs above.
- `Objects/odictobject.c` (other slice) — I checked for the `set_discard_key`-style ambiguous-`-1`
  shape in the odict pop/discard family while confirming the guarded twin; nothing to report, but the
  slice owner should apply recall gap B's test there.

---

## Reproducers

| file | finding | build | result |
|---|---|---|---|
| `repro/set_discard_swallows_eq_typeerror.py` | FIX, `setobject.c:2660`/`:2700` | `debug-gil-nojit`, `release-gil-nojit` | 5/5 each — exception swallowed |
| `repro/dict_insert_to_emptydict_notify_reentry.py` | CONSIDER, `dictobject.c:2103` | `debug-gil-nojit` | 5/5 SIGSEGV |
| " | " | `release-gil-nojit` | OOB pointer returned as a dict key |
| " | " | `release-gil-nojit-asan` | SEGV on READ, `Py_INCREF(0xbebebebebebebebe)` |
| `repro/dict_insert_combined_notify_reentry.py` (arg `4`) | CONSIDER, `dictobject.c:1917` | `debug-gil-nojit` | 5/5 SIGABRT, `assert dk_usable >= 0` |
| " | " | `release-gil-nojit` | SIGSEGV |
| " | " | `release-gil-nojit-asan` | heap-buffer-overflow **WRITE** of size 8 |
