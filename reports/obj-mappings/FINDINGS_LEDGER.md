# Findings ledger — obj-mappings slice

`Objects/dictobject.c` (8,597) + `Objects/setobject.c` (3,228) @ `4f3be1b5777`.
Running ledger; agent reports land in `agents/`, reproducers in `repro/`.

Classification: FIX / CONSIDER / POLICY / ACCEPTABLE.
Every reproduction claim carries **N/M runs and a build name**. A claim without one is a lead.

---

## Confirmed this run

### P3-F14 — `reversed(dict)` reads out of bounds with NO upper bound → SIGSEGV in RELEASED CPython  **[FIX — file this first]**

**Site:** `Objects/dictobject.c:6283-6284`, `dictreviter_iter_lock_held`; seed at
`dictiter_new:5636`. **Found by:** null-safety-scanner.
**Verified independently by the orchestrator: 9/9 across three interpreters.**

```c
Py_ssize_t i = di->di_pos;
PyDictKeysObject *k = d->ma_keys;
if (i < 0) { goto fail; }                                    /* :6272 — the ONLY bound */
PyDictUnicodeEntry *entry_ptr = &DK_UNICODE_ENTRIES(k)[i];   /* :6283 */
while (entry_ptr->me_value == NULL) { ... }                  /* :6284 — OOB READ */
```

`di_pos` is seeded from `load_keys_nentries(dict) - 1` — the `dk_nentries` of the keys object
that existed **when `reversed()` was called**. The only staleness check is
`di->di_used != d->ma_used` at `:6261`, and `ma_used` says nothing about `dk_nentries`.
There is **no upper bound at all**.

**Guarded twin — all three forward iterators have the bound this one lacks:**
`dictiter_iternextkey_lock_held:5747`, `dictiter_iternextvalue_lock_held:5870`,
`dictiter_iternextitem_lock_held:5994` each do `if (i >= n) goto fail;` against the *current*
`dk_nentries`. They can afford a weaker seed because their `di_pos` starts at 0 and only
grows; the reverse iterator starts at the far end, so it is precisely the one that needs the
check — and it is the one without it.

**Trigger is pure Python. No `_testcapi`, no threads, no C API.** Build a dict with many
entries, delete all but one (`dk_nentries` stays high, `ma_used` drops to 1), take
`reversed()`, then `clear()` and reinsert one key so the dict gets a fresh `PyDict_MINSIZE`
keys object. `di_used == ma_used` passes; `di_pos` is still N−1.

| interpreter | result |
|---|---|
| `release-gil-nojit` (3.16.0a0) | **SIGSEGV 3/3** (orchestrator), 24/24 (agent) |
| `debug-ft-nojit` (3.16.0a0) | **SIGSEGV 3/3** (orchestrator) |
| **`/usr/bin/python3` 3.14.4 — RELEASED** | **SIGSEGV 3/3** (orchestrator) |
| **3.12.13 — RELEASED** | SIGSEGV 5/5 (agent) |

ASan: `heap-buffer-overflow READ at :6284`. All five reverse entry points crash.

**Prior art: none.** Four `gh api` searches. The nearest hit, gh-82706 ("Strange reversed dict
behavior"), is **closed** — it is bpo-38555 (2019), which fixed the **lower** bound only and
left the upper bound absent. Nothing open covers this.

**Why this is the campaign's most reportable finding to date:** a live segfault in *released*
CPython, back to at least 3.12, from ordinary pure-Python code, with a one-line guarded twin
three functions away that shows the fix.

### P3-F15 — `insertdict` → `_PyDict_InsertSplitValue:1995` NULL-derefs `ma_values` on the DEFAULT GIL build  **[FIX]**

**Sites:** `Objects/dictobject.c:2030` → `:1995`; sibling
`dict_setdefault_ref_lock_held:4861`. **Found by:** null-safety-scanner.

`mp->ma_values` is dereferenced after `insert_split_key` ran arbitrary Python. **This is
CPY-0096's mechanism landing on the default GIL build** — the region is not inside
`#ifdef Py_GIL_DISABLED`. GIL **SIGSEGV 12/12**; both siblings.

**Why it was invisible until now:** the FT builds *hang* on CPY-0096's deadlock before
reaching this consequence, so a finding investigated only on FT builds cannot see it. That is
a general lesson for the campaign, not a detail of this bug.

### P3-F16 — three more stale-index-across-Python crashes  **[FIX]**

All from null-safety-scanner, all reproduced:

- **`dict_popitem_impl:5051/5075`** — `_PyDict_NotifyEvent` runs Python between the entry read
  and `lookdict_index`; `assert(j >= 0)` is the only guard. Debug: assertion failure 16/16.
  Release: **`popitem()` returns a tuple whose second element is a raw C NULL**, so `t[1]` →
  SIGSEGV 16/16. ASan: heap-use-after-free at `:5052`.
- **`delitem_common:2963/2990`** via `_PyDict_DelItem_KnownHash_LockHeld:3038` — debug
  assertion 16/16; release **`Py_DECREF(NULL)`** at `:2990`, gdb-confirmed, 12/12.
- **`delete_index_from_values:2943`** — a search loop bounded only by `assert(i < size)`.
  Debug SIGABRT 12/12; plain release does **not** crash (0/6) and instead **silently loses a
  dict entry**; `release-gil-nojit-asan` reports `heap-buffer-overflow READ`.

**CPY-0079 sibling hunt answered:** 4 new FIX-grade siblings plus 3 CONSIDER clusters
(9 sites) out of **110 deref-bearing asserts**.

### P3-F1 — `set.add()` reports success and silently drops the element  **[FIX]**

**Site:** `Objects/setobject.c:290-295`, `set_add_entry_takeref`.
**Found by:** git-history-context (structural), orchestrator (mechanism + reproduction).
Independently flagged by pep7-style-checker as an unbraced guard at `:294`.

The mutation-revalidation guard is ordered so it cannot run on the match path:

```c
cmp = PyObject_RichCompareBool(startkey, key, Py_EQ);   /* runs arbitrary Python */
Py_DECREF(startkey);
if (cmp > 0)  goto found_active;                                   /* :290 — escapes first */
if (cmp < 0)  goto comparison_error;                               /* :292 */
if (table != so->table || entry->key != startkey) goto restart;    /* :294 — only cmp == 0 */
```

A user `__eq__` that mutates the set and then claims equality reaches `found_active`, which
does `Py_DECREF(key); return 0;` — success — against a slot that no longer holds the element.

**Severity correction, stated explicitly:** this is **not** a use-after-free. `found_active`
never dereferences `entry`, so a stale `entry` after a mutation or resize is not read. The
consequence is a silent lost update: `add()` succeeds, the element is absent.

**Guarded twins — two, which is what makes this a defect rather than a design choice.**
Both order the identical three checks so revalidation gates the FOUND verdict:

- In-file: `set_compare_entry_lock_held:157-162` — `if (cmp < 0) return ERROR;` then
  `if (table != so->table || entry->key != startkey) return SET_LOOKKEY_CHANGED;` then
  `if (cmp > 0) return SET_LOOKKEY_FOUND;`.
- Cross-file: `dictobject.c compare_generic:1226-1235` — the revalidation gates the `return
  cmp` for **both** outcomes, so an "equal" verdict from a mutating `__eq__` still restarts.

Both twins address exactly this threat model (re-entrant mutation during the user compare),
so the lesson-3 check passes: they are twins for the hazard being claimed.

**Reproduction:** `repro/orch_set_add_lost_element.py`,
`repro/orch_dict_vs_set_differential.py`.

| build | result |
|---|---|
| `debug-gil-nojit` | lost 1/1 |
| `release-gil-nojit` | lost 1/1 |
| `debug-ft-nojit` | lost 1/1 |
| `release-ft-nojit` | lost 1/1 |
| `/usr/bin/python3` 3.14.4 (released) | lost 1/1 |

Deterministic — the first hostile compare loses the element, no iteration needed. Final state
is an **empty** set: the new element is dropped and the old one was removed by the callback.
`add()` returns `None` normally; nothing is raised.

**Differential:** identical hostile object through `dict.__setitem__` keeps both entries
(`len=1`, both present). Through `frozenset([a, b])` — `len=1`, no error.

**Prior art:** none found. `gh api -X GET search/issues` on three phrasings returns nothing
for the set case. Closest is **gh-148259** (`list.remove` is not atomic for non-trivial
`__eq__` comparisons), **open** — same family, different container, which is useful evidence
that CPython treats this class as a real problem rather than "all bets are off".

**History:** the guard was written into reader and writer in the same commit
(`2cc9b8486dd9`, 2020-05-10) with the orderings already different. It survived the 2025-12-13
rewrite that unified four read paths onto `set_do_lookup` while leaving the writer's
hand-copied probe loop behind. Six years unchanged. The trap for a reviewer is that grep says
the guard *is* there.

**Fix:** reorder to match the twins — move the `table != so->table || entry->key != startkey`
test above the `cmp > 0` test. Two lines.

**Status:** reproduced. Not yet recorded in the findings repo; not yet reported upstream.

---

### P3-F2 — the critical-section deadlock bypass misses runtime-wide stop-the-world  **[CONSIDER, cross-slice]**

**Sites:** `Python/critical_section.c:50` (the bypass), `Python/pystate.c:2531-2552`
(the two entry points). **Found by:** stw-safety-checker. **Verified independently by the
orchestrator.**

`Py_BEGIN_CRITICAL_SECTION`'s deadlock bypass reads one field:

```c
/* Python/critical_section.c:46-52 */
// If the world is stopped, we don't need to acquire the lock ...
// Without this check, acquiring a critical section while the world is
// stopped could lead to a deadlock.
if (tstate->interp->stoptheworld.world_stopped) { ...; return; }
```

But `stop_the_world(stw)` is generic and sets `world_stopped` on **whichever struct it was
handed**, and the two entry points hand it different structs:

```c
_PyEval_StopTheWorldAll(runtime) -> stop_the_world(&runtime->stoptheworld);   /* pystate.c:2534 */
_PyEval_StopTheWorld(interp)     -> stop_the_world(&interp->stoptheworld);    /* pystate.c:2550 */
```

So inside a **runtime-wide** stop-the-world, `interp->stoptheworld.world_stopped` is still 0,
the bypass does not fire, and the mutex is genuinely acquired — the exact deadlock the
comment says the check exists to prevent. Per-interpreter STW is unaffected.

Runtime-wide callers: `Objects/obmalloc.c:1942`, `Objects/object.c:3410`,
`Modules/posixmodule.c:697`, `Python/ceval_gil.c:1120`, `Python/pylifecycle.c:2355`,
`Python/tracemalloc.c`, and `pystate.c` (`sys._current_frames()`).

**Latent, not live:** the agent reports `PyDict_SetItem` running in exactly that state from
`sys._current_frames()`, but on a fresh unpublished local dict, so there is no contention.
**5/5 runs, no deadlock, `debug-ft-nojit`.**

**This does not invalidate the pass-2 retraction.** I checked: `Objects/typeobject.c` uses
`_PyEval_StopTheWorld(interp)` at all three sites (`:128`, `:1572`, `:1598`), so the bypass
*does* fire there and the retraction of the `object_set_class_world_stopped` lead stands.
P3-F2 is a genuinely different case, not a reinstatement.

**Slice ownership:** none. The campaign manifest covers `Objects/` + `Modules/` only, so a
finding anchored in `Python/` has no home slice — the same situation as CPY-0009. Record it
anyway.

### P3-F3 — `set_clear_internal` is registered raw as `tp_clear` with no critical section  **[CONSIDER]**

**Sites:** `Objects/setobject.c:2916`, `:3008`. **Found by:** stw-safety-checker.

All 12 other routes into `set_clear_internal` take a critical section; the two `tp_clear`
registrations do not. **Guarded twin:** `dict_tp_clear` → `PyDict_Clear` →
`Py_BEGIN_CRITICAL_SECTION`.

The only available justification is "the GC stops the world", and that is **false**:
`delete_garbage` dispatches `tp_clear` at `gc_free_threading.c:2176`, fifteen lines *after*
`StartTheWorld` at `:2161`.

**Lesson 3 applied, and it mattered.** `set_clear_internal` does carry a prominent comment
about being delicate because decrefs can mutate the set. That comment is correct — for
single-threaded `__del__` re-entrancy. It gives **zero** protection against a second thread.
Citing it as evidence of thread-safety would have been exactly the error lesson 3 warns about.

**8/8 clean, 0 TSan warnings.** Static-confirmed, not reproduced.

### P3-F4 — `set.add()` OOM leaves the set permanently unusable, then HANGS  **[FIX]**

**Site:** `Objects/setobject.c:319-326`, `set_add_entry_takeref`, `found_unused:`.
**Found by:** error-path-analyzer.

`found_unused:` commits the insertion — `so->fill++`, then the key write — **before** calling
`set_table_resize()`. A failed resize (OOM) returns −1 with the element already in and `fill`
already past the load-factor point, so the next add fails identically. **Four** such failures
drive `fill` to `mask+1`, which destroys the invariant the file states at `:517-520`
("set_lookkey needs at least one virgin slot to terminate failing searches") — and then the
`while (1)` probe loop in this same function never returns.

**HANG 6/6 `debug-gil-nojit`; 4/4 on each of `release-gil`, `debug-ft`, `release-ft`.** The
OOM window is bounded, so this is a genuine hang, not an allocation-famine livelock; a marker
trace pins it to the 9th `set.add()`.

**Guarded twin:** `dictobject.c:1910-1915` `insert_combined_dict` resizes *before* writing.
Lesson 3 applied honestly by the agent: that ordering is **structural** (dict has nowhere to
put the entry until the table exists), not a deliberate defence — so it is a twin in shape,
weaker evidence of intent than it first looks.

**Note:** this is the *second* independent defect in `set_add_entry_takeref` this run — P3-F1
is the other. Same function, unrelated mechanisms.

### P3-F5 — `set.remove()` / `set.discard()` swallow a user `__eq__` exception  **[FIX]**

**Sites:** `Objects/setobject.c:2656-2660` (`remove`) and `:2696-2700` (`discard`).
**Found by:** pyerr-clear-auditor **and** error-path-analyzer independently.
**Verified by the orchestrator on both GIL builds.**

The clear is narrowed with `PyErr_ExceptionMatches(PyExc_TypeError)` — but the call it guards,
`set_discard_key`, has **two** failure modes: `PyObject_Hash` *and* `PyObject_RichCompareBool`
against an existing element. The narrowing therefore discriminates nothing. A `TypeError`
raised by a colliding element's user `__eq__` is cleared.

- `s.discard(x)` → **silent success**, exception gone, no `__context__`, no unraisable report.
- `s.remove(x)` → a `KeyError` that misreports the cause.

Reproduced 5/5 (pyerr-clear) and 3/3 (error-path) on `debug-gil-nojit` and `release-gil-nojit`;
orchestrator confirmed on both. Damage is genuinely bounded to `TypeError` — a
`KeyboardInterrupt` from `__hash__` propagates correctly (verified).

**Guarded twins, both in the same file:** `_PySet_Contains:2559` and
`frozenset___contains___impl:2619` call `PyObject_Hash` **themselves**, so no comparison can
enter their clear window. Lesson 3 satisfied precisely: it is the *hoisting of the hash*, not
the narrowing, that makes those two sound — the narrowing is identical in all four.

### P3-F6 — `assert(new_dict == NULL)` asserts a relationship the code never establishes  **[FIX]**

**Sites:** `Objects/dictobject.c:7932` and `:7848`. **Found by:** error-path-analyzer.

On the `_PyDict_DetachFromObject` failure path. That callee fails only on `copy_values` OOM,
and `obj.__dict__ = {...}` reaches the assert with `new_dict != NULL`.
**SIGABRT 10/10 `debug-gil-nojit`**; the FT arm carries the same assert and also aborts;
release is correct.

**Guarded twin:** `PyObject_ClearManagedDict:8010-8024` handles the identical failure through
`PyErr_FormatUnraisable` plus a manual teardown.

### P3-F7 — `dictiter_iternext_threadsafe` leaks the key and value it just increfed  **[FIX, FT-only]**

**Site:** `Objects/dictobject.c:6145`. **Found by:** gil-discipline-checker (redirected remit).

On the `"dictionary keys changed during iteration"` path. The two build arms do the same two
operations in **opposite order**: the FT arm calls `acquire_key_value` (INCREF) and *then*
tests `di->len == 0`; the GIL twin `dictiter_iternextitem_lock_held:6012` tests `di->len == 0`
**thirteen lines before** its `Py_NewRef` pair.

Reproduced single-threaded: **10/10 leaked on `debug-ft-nojit` and `release-ft-nojit`,
0/10 on both GIL builds.**

**Guarded twin:** `:6012` — and its guard is *statement ordering*, not a lock. Per lesson 3
that is exactly the threat model at issue here, so the twin is a true twin.

### P3-F9 — a DOC-CONFORMING dict watcher makes CPython re-enter Python in 10 stale-state windows  **[FIX]** ← headline of the slice

**Site:** `Objects/dictobject.c:8314` `_PyDict_SendEvent`, plus 10 of the 14
`_PyDict_NotifyEvent` call sites. **Found by:** refcount-auditor.
**Crux verified verbatim by the orchestrator.**

The contract in `Doc/c-api/dict.rst` says two things, four lines apart:

> The callback may inspect but must not modify *dict* … **Do not trigger Python code
> execution in the callback**, as it could modify the dict as a side effect.
> …
> **If the callback sets an exception, it must return `-1`**

And `_PyDict_SendEvent` responds to that mandated `-1` with:

```c
if (cb && (cb(event, (PyObject*)mp, key, value) < 0)) {
    PyErr_FormatUnraisable("Exception ignored in %s watcher callback for <dict at %p>", ...);
}
```

`PyErr_FormatUnraisable` invokes `sys.unraisablehook` — **arbitrary Python, settable from
pure Python**. So a callback that follows the documented contract *exactly* — sets an
exception, returns −1, itself runs no Python — causes **CPython** to run Python inside the
notify window. The contract places the burden on the callback and then CPython discharges it
wrongly on the callback's behalf.

Ten of the fourteen notify sites capture `ix`, a borrowed `old_value`, an `ep0` pointer, or a
`dk_usable` precondition before the notify and use it after. Three reproduced **on the default
release GIL build**, not merely under ASan:

| site | evidence |
|---|---|
| `insert_combined_dict:1917` | ASan **heap-buffer-overflow WRITE** 5/5, `0 bytes after` the keys block at `:1927`; debug `assert(dk_usable >= 0)` at `:1938`; release **silent corruption**, exit 0 |
| `dict_popitem_impl:5051` | ASan heap-use-after-free R+W 5/5; release **SIGSEGV 3/3** |
| `_PyDict_DelItem_KnownHash_LockHeld:3038` | debug `assert(hashpos >= 0)` at `:2963` 5/5; release **SIGSEGV 3/3** |

**Guarded twin, lesson 3 applied and it changed the conclusion:** `dict_dealloc:3650-3658` is
the only safe site — but its `_PyObject_ResurrectStart/End` bracket defends against
**resurrection**, and the comment at `_PyDict_SendEvent:8309-8312` names that threat and only
that threat. Its mutation-safety is *incidental*, coming from reading `ma_keys` / `ma_values`
**after** the notify. **Take the ordering as the fix, not the bracket.**

This also reaches CPY-0096's inherited `dk_usable` consequence on **combined** dicts by a
shorter route, and as a real out-of-bounds write.

**Supersedes the CONSIDER classification below.** The pyerr-clear agent rated the same window
CONSIDER because triggering it needed a watcher that violates `Doc/c-api/dict.rst:583`. That
premise is now falsified: no violation is required. P3-F8 is folded in here as FIX.

### P3-F8 — `PyDict_EVENT_ADDED` notify runs Python between a state decision and its write  **[folded into P3-F9 — FIX]**

**Sites:** `Objects/dictobject.c:2103` and `:1917`. **Found by:** pyerr-clear-auditor.

Sibling entry points of the inherited CPY-0096 shape, not a re-litigation of it. At `:2103` a
re-entrant insert desyncs `ma_used` from `dk_nentries`; at `:1917` it invalidates the
`dk_usable` capacity check.

Reproduced: debug **5/5 SIGSEGV** and **5/5 SIGABRT** (`assert dk_usable >= 0`); release hands
`<class 'str'>` back as a dict key; ASan gives `Py_INCREF(0xbebebebebebebebe)` and a
**heap-buffer-overflow WRITE of size 8** at `dictobject.c:1926`, *located 0 bytes after* the
keys allocation.

**CONSIDER rather than FIX** because it requires a watcher callback that violates
`Doc/c-api/dict.rst:583` — **which CPython's own `_testcapi` watcher does** (`%S` →
`PyObject_Str`). That tension is the finding's real content and should be stated to
maintainers rather than resolved unilaterally.

### P3-F10 — `dictreviter_iternext` double-DECREF of the dict  **[FIX, FT-only]**

**Site:** `Objects/dictobject.c:6337-6340`, latch at `:6347`. **Found by:** ft-race-scanner.

The **reverse** iterator has **no `#ifdef Py_GIL_DISABLED` arm at all**, and `d = di->di_dict`
is latched at `:6347` *before* the critical section — which is keyed on the **dict**, not the
iterator. Two threads advancing one shared reverse iterator drop the same owning reference.

**6/6 `_Py_NegativeRefcount` on the dict (refcount −1 / −2) on `debug-ft-nojit`; 0/6 GIL.**
No upstream prior art.

### P3-F11 — `get_index_from_order:676` NULL-deref → SIGSEGV  **[FIX, FT-only]**

**Site:** `Objects/dictobject.c:676`, reached from `:6100`. **Found by:** ft-race-scanner.

The lock-free iterator snapshots `ma_values` atomically at `:6086` and uses that snapshot at
`:6101` — but the helper it calls at `:6100` **re-reads the field**, and a concurrent
`dictresize` does `set_values(mp, NULL)`.

**6/6 SIGSEGV on all three FT builds, 0/6 on both GIL builds**, gdb frame exactly as
predicted. **Trigger is pure Python** — no C API needed.

### P3-F12 — `dictiter_len:5682` torn read  **[CONSIDER, FT-only]**

TSan confirms the exact pair (`dictiter_len:5682` vs `dictiter_iternext_threadsafe:6158`).
**Not** crash-reproduced in 3,000 trials — QSBR delays the free. Reported at the strength
actually measured: a confirmed TSan race, not a demonstrated crash.

### P3-F13 — two plain `ma_values` stores to live dicts bypass `set_values()`  **[CONSIDER, static]**

`Objects/dictobject.c:7976` and `:8019`. `:8019` sits one line below
`set_keys(dict, Py_EMPTY_KEYS)`. Static only — no reproduction attempted, and said so.

### TSAN-0053 has a second, un-catalogued site

`dictiter_iternextitem_lock_held:6027-6030`, reachable on FT via `try_locked:6162`.
**Any gh-154130 fix must cover it.** Worth carrying into the upstream conversation.

---

## Bounded negatives from the concurrency pass — the fix surface is now sized

- **The CPY-0096 sibling hunt comes up EMPTY.** All 7 `LOCK_KEYS` regions enumerated; 2 are
  the recorded violations, 5 clean with explicit reasons (`dictresize:2230` does `Py_NewRef`
  only — no DECREF, no Python). This **bounds the fix surface for that class at exactly two
  call sites**, which is worth as much to a maintainer as a new finding.
  `setobject.c` is a structural zero — no keys object.
- **The `#ifdef` divergence class (the CPY-0099 shape) is clean, 24/24.**
- **The history agent's unpropagated-`memcpy` hypothesis is DISPROVEN for `dictobject.c`,
  13/13** — every dict `memcpy` targets freshly allocated, unpublished memory. A hypothesis
  I relayed to two agents, killed by measurement.
- `so->fill` is a lock-only field, 21/21. `set_pop_impl` is clinic-guarded.
- Scanner finding #19 is a branch-unaware false positive.

Scanner precision on this class: **2/20 promoted (10%)**; 3 of the 5 FIX findings came from
reading rather than from the scanner.

---

## Inherited from obj-typeobject pass 2 — now DISCHARGED

All three GIL-build consequences of CPY-0096 are anchored, by gil-discipline-checker at
`insert_split_key:1962`. The mechanism: **`LOCK_KEYS` expands to nothing in the `#else` arm**,
so on the default build the stale `dk_usable > 0` read at `:1964` and the write at `:1976` are
separated by `_PyType_Modified_Unlocked` with no barrier at all.

| consequence | evidence |
|---|---|
| `dk_usable` invariant break | assertion **SIGABRT 5/5** on `debug-gil` |
| unbounded recursion → SIGSEGV | **SIGSEGV 5/5** on `release-gil` *and* `debug-gil` (nested variant) |
| ASan heap-buffer-overflow | at `store_instance_attr_lock_held:7497` on `release-gil-nojit-asan` |
| (control) | `release-ft-nojit` **hangs 3/3** on the identical input |

Also explained: why a *single* re-entry is silent on release — `_PyDict_NewKeysForClass:7262`
allocates 42 entries but sets `dk_usable = 30`.

**CPY-0079 re-run: discharged, and the verdict flipped.** Two agents converged independently.
Error-path achieved **48** allocation-failure points on `debug-gil-nojit` (40 on debug-ft,
46 on release-gil); uninit-dealloc achieved **40**. Both above the 20-point floor, both
`thin_evidence: false`. Verdict is **REPRODUCED, not clean** — SIGSEGV 10/10 and 20/20
respectively. gdb puts the fault at `dictobject.c:5368` *inside the inlined callee, past its
own `:5362` NULL check*, `rax=0`, `si_addr=0x10`. That is the record's central claim —
the caller's UB-bearing assert let the optimizer delete the guard — now observed.

---

## Inherited from obj-typeobject pass 2, owed to this slice

| item | status |
|---|---|
| CPY-0096 consequence: ASan heap-buffer-overflow on the **GIL** build | assigned to memory-pattern-analyzer + tsan-stress-generator |
| CPY-0096 consequence: `dk_usable` invariant break 5/5 from a single re-entry | same |
| CPY-0096 consequence: unbounded recursion → SIGSEGV | same |
| CPY-0079 OOM sweep re-run (old denominator was 2, below the certify threshold) | assigned to error-path-analyzer |

---

## Confirmed-not-relitigated

| id | site | one line |
|---|---|---|
| CPY-0015 | `dictiter_new:5646` | untracked-DECREF on the error path |
| CPY-0019 | `frozendict_pair_hash:8427` | guardless copy of `tuple_hash` (gh-154318) |
| CPY-0079 | `copy_lock_held_untracked:4494` | `assert()` derefs an unchecked allocation |
| CPY-0096 | `insert_split_key:1971` | arbitrary Python under the non-reentrant keys mutex |
| CPY-0107 | `_Py_dict_lookup:1385` | `RichCompareBool` under `LOCK_KEYS` |
| CPY-0115 | `delitem_common:2987` | live `PyDict_Next` cursor vs mutation |

---

## Bounded negatives (denominator stated)

### Init bypass — **structurally clean, proven not assumed**

0 findings out of **0 nullable fields** across 404 functions. Proven structural with a
counterfactual rather than asserted: patching `tp_new` → `0` in both type tables makes the
scanner's pairing filter pass, the heuristic then runs and succeeds, and the result is still
0 — because `dict_init` assigns no struct field and `set_init` assigns only a scalar.

The fact that bounds it: `type_ready_set_new` (`typeobject.c:9428-9437`) adds
`Py_TPFLAGS_DISALLOW_INSTANTIATION` **implicitly** to a static type with `tp_new == NULL` and
`tp_base == object`. The token appears nowhere in either slice file, yet all 10 view and
iterator types carry the flag (`type(iter({})).__flags__ & (1 << 7)` → `True`).

Dynamic confirmation: **2,048 runs, 0 crashes** (512 probes × 4 builds — 4 construction
routes × 14 types × every callable and 12 operators, each forked), plus a 15-route
dict/frozendict layout-confusion probe, 30/30 clean.

`di_dict` / `si_set` are genuinely nullable via *exhaustion* rather than init bypass — 11 read
sites, **all** correctly guarded. Zero deletable members or getsets in scope.

### `LOCK_KEYS` regions — population exhausted

Seven regions in `dictobject.c`. Two carry recorded findings (`:1385` CPY-0107, `:1962`/`:1971`
CPY-0096); the include-graph-mapper read and classified the other five clean (`:738`, `:1283`,
`:1317`, `:2230`, `:7317`). A *new* finding of that exact shape is not available here — the
lock-discipline agent owns the final verdict and two open discrepancies (six regions vs seven,
and `:1962` vs the record's `:1971`).

---

## POLICY

### Dead `dv_dict == NULL` guards — nine guarded reads, three unguarded

`dictobject.c`: nine `dv->dv_dict == NULL` guards against three unguarded reads
(`dictview_mapping:6489`, `dictviews_to_set:6657`, `dictitems_xor:6878`). Reads as a
guarded-twin asymmetry; **is not one**. `git log --all -S "dv_dict = NULL"` returns nothing —
the field has never been NULL in 19 years. The guards are from 2007 and defend an abandoned
design whose TODO still sits at `dictobject.c:6499`. Recorded as D-26 so a future informed run
does not report the three reads as siblings needing a fix.

### PEP 7 — 185 brace-less bodies, 3 keyword-space violations

185 `missing-braces` (dict 87, set 98) — all true positives, none a present-tense bug.
Three escalate to CONSIDER as drift risk next to load-bearing code:
`dictobject.c:1635`/`:1666` (`_Py_dict_lookup_threadsafe`, unbraced `goto read_failed;` where
the identical predicate is braced at `:1645`, and the surrounding idiom is
`Py_DECREF(value); goto read_failed;`), and `setobject.c:294`/`:159` (both halves of the
re-entrancy defense — `:294` is P3-F1's guard).
154 `line-too-long` are POLICY: CPython's `.editorconfig` sets no `max_line_length`.
