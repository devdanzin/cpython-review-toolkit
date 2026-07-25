# Informed-exploration briefing — CPython C code review

You are running as part of an **informed** explore. Unlike a cold run, you have the catalog of recurring CPython C bug SHAPES (reusable templates — not file:line, since the scope differs every run), the cross-cutting triage rules, and the false-positive taxonomy. Use them: the goal is fix-propagation (find *every* instance of a known shape) and genuinely new territory, not re-discovering the catalog.

## Your three informed-mode rules

1. **Confirm, don't re-litigate.** If a candidate matches a previously-recorded finding (the catalog section below, if present), tally it in one line and move on — do not re-describe a known bug.
2. **Skip the known false-positive classes** (the taxonomy below). If you flag something that falls in one of those classes, you must say *why this instance is NOT that FP class*.
3. **Hunt siblings via the guarded twin.** For each bug SHAPE relevant to your scope, locate its **guarded twin** (the correctly-handled sibling in the same file/family = the fix), then search for the other sites that lack it — those un-found siblings are the point of an informed run.

## Cross-cutting triage rules (apply to every finding)

- The guarded twin is the strongest static-review signal: nearly every real CPython bug has a correctly-handled sibling in the same file — cite it as the fix and hunt for other unfixed siblings.
- Both-crash != acceptable: if the differential shows released CPython ALSO crashes on the input, that is not proof it is fine — search the CPython tracker (label:type-crash) and treat a pure-Python-reachable segfault as a bug in both.
- Reachability first: rank a candidate by whether its triggering value flows from Python-controlled input (a parsed arg, a user __index__/__hash__/__repr__, a user-supplied index/length) vs an internal invariant (assert / 'cannot fail').
- Confirm-don't-relitigate: a finding already recorded in the findings catalog is context, not a fresh discovery — confirm it still reproduces and move on to siblings.
- Class J (abort-vs-MemoryError) is out of scope; a wrong-size write or a swallowed error is in scope.

## Bug-shape catalog — sibling-hunt templates

6 recurring shapes. For each, the **guarded twin** is the correctly-handled sibling that both confirms the finding and *is* the fix.

### unguarded-recursion-in-slot — Recursion-prone slot without Py_EnterRecursiveCall
- **severity (default before triage):** FIX
- **pattern:** A tp_hash / tp_richcompare / tp_repr / tp_str slot (or a generic-alias parameter walk) that descends a user-controlled object graph — self-recursion, or a loop calling PyObject_Hash/Repr/RichCompare on items — with no Py_EnterRecursiveCall / Py_ReprEnter.
- **guarded twin (the fix):** A sibling slot in the same file that DOES bracket its descent with Py_EnterRecursiveCall()/Py_LeaveRecursiveCall() or Py_ReprEnter()/Py_ReprLeave().
- **hunt:** For every container/aggregate type, check tp_hash, tp_richcompare, tp_repr, tp_str for symmetry: if repr is guarded but hash is not, hash is suspect. Grep for copy-pasted hash algorithms across container types.
- **differential (how to confirm):** Build a deeply-nested or self-referential instance on a debug CPython and trigger the slot; a SIGSEGV (not RecursionError) confirms. A native stack overflow is a bug even if released CPython also crashes.
- **confirmed examples:** gh-154318 tuple_hash/frozendict_hash, gh-154275 _Py_make_parameters, gh-149146 tuple_dealloc
- **surfaced by:** `scan_recursion_guards.py`

### pyerr-clear-in-destructor — PyErr_Clear() clobbers an in-flight exception during teardown
- **severity (default before triage):** FIX
- **pattern:** A tp_dealloc / tp_clear / tp_finalize calls PyErr_Clear() (or drops a fallible result) with no surrounding save/restore of the exception state; the destructor can run while an exception is already pending.
- **guarded twin (the fix):** A destructor that captures with PyErr_GetRaisedException() at the top and restores with PyErr_SetRaisedException() at the bottom (or reports via PyErr_WriteUnraisable).
- **hunt:** Across a module family, check every dealloc/clear/finalize that calls a fallible API on a cleanup path; the ones that PyErr_Clear() without save/restore swallow MemoryError/KeyboardInterrupt.
- **differential (how to confirm):** Under set_nomemory OOM injection, raise MemoryError, then trigger the teardown; the swallowed error becomes a silent success/hang.
- **confirmed examples:** gh-152083 context_tp_dealloc, OOM-0039 deque_clear, gh-146102 PyErr_Clear-on-success sweep (upstream, in progress)
- **surfaced by:** `scan_pyerr_clear.py`

### dealloc-of-uninitialized-object — Half-constructed object freed on an error path
- **severity (default before triage):** FIX
- **pattern:** A constructor allocates via a non-zeroing allocator (PyObject_New/PyObject_GC_New/...), then on a fallible-step failure Py_DECREFs the object before its members are NULL-initialized; tp_dealloc reads garbage member pointers.
- **guarded twin (the fix):** A constructor that NULL-initializes all members (or memsets the object) immediately after allocation, before any fallible call.
- **hunt:** For each non-zeroing allocation with an error path that frees the object, read the tp_dealloc: if it Py_XDECREFs / switches-on-enum a member the constructor sets late, it crashes on that path.
- **differential (how to confirm):** set_nomemory to fail the exact allocation on the error path, then construct from Python; a crash in tp_dealloc confirms (OOM class O5).
- **confirmed examples:** gh-151815 template_iter, gh-152851 blake2 .copy()
- **surfaced by:** `scan_uninit_dealloc.py`

### borrowed-ref-across-call — Borrowed reference used across a call that can free it (crown jewel)
- **severity (default before triage):** FIX
- **pattern:** A borrowed pointer loaded from a slot field or container (PyList/Tuple/Dict_GET_ITEM) is used after an intervening call that can run arbitrary Python (PyObject_Call*, PyObject_Repr/Str, converter callbacks like PyUnicode_FSConverter, warnings, GC) with no intervening Py_INCREF.
- **guarded twin (the fix):** The same access pattern elsewhere that Py_INCREFs the borrowed ref before the call and Py_DECREFs after.
- **hunt:** Trace every borrowed load feeding a call that can execute Python; the free may come from a __del__, __fspath__, setcontext, or a re-entrant callback.
- **differential (how to confirm):** Supply an adversarial object whose callback drops the last strong ref (mutating __fspath__/__eq__/__index__), then observe UAF on ASan.
- **confirmed examples:** gh-148382 _decimal CURRENT_CONTEXT, gh-151403 _posixsubprocess __fspath__, gh-154527 defaultdict default_factory (FT)
- **surfaced by:** `scan_refcounts.py (widen the Python-reaching call set)`

### return-null-without-exception — Return NULL without setting an exception / stale-exception desync
- **severity (default before triage):** FIX
- **pattern:** A PyObject*-returning function returns NULL with no prior PyErr_Set*, or an entry point trusts res!=NULL while PyErr_Occurred() is set.
- **guarded twin (the fix):** Sibling error paths in the same function that set an exception before returning NULL.
- **hunt:** OOM paths are the usual offenders — a constructor whose allocation-failure branch returns NULL but forgets PyErr_NoMemory().
- **differential (how to confirm):** set_nomemory to force the failure; a SystemError('returned NULL without setting an exception') confirms.
- **confirmed examples:** gh-151968, gh-151126 (missing PyErr_NoMemory cluster)
- **surfaced by:** `scan_error_paths.py (return_null_no_exception)`

### integer-overflow-in-allocation — Allocation size from a Python-controlled multiply with no overflow guard
- **severity (default before triage):** CONSIDER
- **pattern:** PyMem_*/malloc(n * size) where n derives from a Py_ssize_t parsed from Python args, with no `n > MAX/size` guard and no safe-multiply helper.
- **guarded twin (the fix):** Sibling allocations that use PyMem_New / a checked multiply / an explicit bound.
- **hunt:** Every length/count argument that reaches an allocation multiply is a candidate; abort-vs-MemoryError is out of scope, wrong-size-then-write is the bug.
- **differential (how to confirm):** Pass a length near SIZE_MAX/elem; a segfault (vs a clean MemoryError) confirms the overflow.
- **confirmed examples:** gh-3493, gh-1779
- **surfaced by:** `memory-pattern-analyzer (promote to a real scanner in a later slice)`

## Previously-recorded findings (confirm, don't re-litigate; hunt siblings)

From the `cpython-review-findings` catalog (84 recorded). These are context, not fresh discoveries: confirm each still reproduces in one line, then spend your effort on **un-found siblings** of the same shape (via its guarded twin).

| id | category | site | title | status |
|----|----------|------|-------|--------|
| CPY-0001 | recursion | `Objects/tupleobject.c:385 (tuple_hash); Objects/unionobject.c:67 (union_hash)` | tuple_hash has no recursion guard: a deeply-nested / cyclic tuple overflows the native C stack (SIGSEGV) instead of raising RecursionError | reproduced |
| CPY-0002 | recursion | `Objects/genericaliasobject.c:231 (_Py_make_parameters); Objects/unionobject.c:332 (union_init_parameters); Objects/unionobject.c:349 (union_getitem)` | _Py_make_parameters walks nested type arguments self-recursively with no Py_EnterRecursiveCall: a deeply-nested arg overflows the C stack (SIGSEGV) -- reachable through typing.Union, not just list[...] | fixed |
| CPY-0003 | refcount | `Objects/iterobject.c:80 (iter_iternext); Objects/iterobject.c:61 (iter_iternext)` | iter_iternext holds a borrowed it_seq across PySequence_GetItem: a re-entrant next() double-DECREFs it -> heap use-after-free | reproduced |
| CPY-0004 | refcount | `Objects/genericaliasobject.c:542 (_Py_subs_parameters); Objects/genericaliasobject.c:460 (_Py_subs_parameters)` | _Py_subs_parameters reads PyTuple_GET_ITEM(args, iarg) one line after Py_XDECREF(tuple_args) frees the tuple that args aliases -> heap use-after-free | reproduced |
| CPY-0005 | null-deref | `Objects/genericaliasobject.c:302 (subs_tvars)` | subs_tvars does Py_DECREF(subargs) on a provably-NULL out-parameter after tuple_extend fails -> SIGSEGV; gh-148222 removed the identical line 60 lines above and left this one | reproduced |
| CPY-0006 | pyerr-clear | `Objects/unionobject.c:172 (unionbuilder_add_single_unchecked)` | unionbuilder_add_single_unchecked calls PyErr_Clear() unconditionally after PyObject_Hash, swallowing any user __hash__ exception including KeyboardInterrupt and MemoryError | reproduced |
| CPY-0007 | null-deref | `Objects/typeobject.c:12763 (supercheck); Objects/typeobject.c:12797 (super_descr_get); Objects/typeobject.c:12786 (super_descr_get)` | super.__new__(super).__get__(1) -> SIGSEGV: super_descr_get passes an uninitialized su->type to supercheck, which dereferences it as type->tp_name | reproduced |
| CPY-0008 | null-deref | `Modules/_io/winconsoleio.c:957 (_io__WindowsConsoleIO_readall_impl)` | _io._WindowsConsoleIO.readall dereferences an unchecked PyBytes_FromStringAndSize on the very next line (Windows-only; guarded twin 65 lines below) | static-confirmed |
| CPY-0009 | pyerr-clear | `Python/pystate.c:836 (interpreter_clear)` | interpreter_clear() unconditionally _PyErr_Clear()s an audit-hook exception: a hook vetoing cpython.PyInterpreterState_Clear is silently discarded, with no unraisable report and exit code 0 | reproduced |
| CPY-0011 | uninit-dealloc | `Objects/odictobject.c:1952 (odictiter_new); Objects/odictobject.c:1718 (odictiter_dealloc)` | odictiter_new: Py_DECREF of a half-built, never-GC-tracked odict_iterator on the _PyTuple_FromPairSteal failure path -> SIGABRT (_PyObject_GC_UNTRACK assert), garbage Py_XDECREFs with NDEBUG | reproduced |
| CPY-0012 | memory-pattern | `Objects/structseq.c:77 (PyStructSequence_New); Objects/structseq.c:235 (structseq_new_impl); Objects/structseq.c:84 (PyStructSequence_New)` | PyStructSequence_New multiplies the Python-writable n_fields into the allocation size with no overflow guard -> heap-buffer-overflow WRITE from three lines of stdlib Python | reproduced |
| CPY-0013 | memory-pattern | `Objects/structseq.c:242 (structseq_new_impl); Objects/structseq.c:243 (structseq_new_impl); Objects/structseq.c:240 (structseq_new_impl)` | structseq_new_impl walks tp_members past its end using the Python-writable n_fields as the loop bound -> SEGV in strlen; needs no integer overflow, only the optional dict argument | reproduced |
| CPY-0014 | uninit-dealloc | `Objects/listobject.c:262 (PyList_New); Objects/listobject.c:569 (list_dealloc); Python/marshal.c:1448 (r_object)` | PyList_New (Py_GIL_DISABLED branch): Py_DECREF of a list whose ob_item and ob_size are both uninitialized when list_allocate_array fails -> PyMem_Free of a garbage pointer (SIGSEGV); free-threaded builds only | reproduced |
| CPY-0015 | uninit-dealloc | `Objects/dictobject.c:5646 (dictiter_new); Objects/dictobject.c:5662 (dictiter_dealloc)` | dictiter_new: Py_DECREF of a never-GC-tracked dict item-iterator on the _PyTuple_FromPairSteal failure path -> SIGABRT (_PyObject_GC_UNTRACK assert); GC-list corruption with NDEBUG | reproduced |
| CPY-0016 | uninit-dealloc | `Modules/_elementtree.c:2377 (create_elementiter); Modules/_elementtree.c:2183 (elementiter_dealloc)` | create_elementiter: Py_DECREF on the PyMem_New failure path leaves parent_stack_used uninitialized, and elementiter_dealloc uses it as a loop bound over a NULL parent_stack -> SIGSEGV | reproduced |
| CPY-0017 | uninit-dealloc | `Objects/templateobject.c:238 (template_iter); Objects/templateobject.c:232 (template_iter); Objects/templateobject.c:53 (templateiter_clear)` | template_iter: both PyObject_GetIter error branches Py_DECREF the t-string iterator before any member is written, so templateiter_clear Py_CLEARs uninitialized pointers -> SIGSEGV | reproduced |
| CPY-0018 | memory-pattern | `Objects/structseq.c:700 (PyStructSequence_InitType2); Objects/structseq.c:667 (_PyStructSequence_InitBuiltinWithFlags); Objects/structseq.c:709 (PyStructSequence_InitType)` | PyStructSequence_InitType2 / _PyStructSequence_InitBuiltinWithFlags free the tp_members array on the error path after PyType_Ready installed it and built member descriptors holding d_member pointers into it -> dangling type | static-confirmed |
| CPY-0019 | recursion | `Objects/dictobject.c:8427 (frozendict_pair_hash); Objects/dictobject.c:8462 (frozendict_hash)` | frozendict_pair_hash is a guardless copy of tuple_hash: a frozendict nested through its VALUES overflows the native C stack (SIGSEGV) instead of raising RecursionError | reproduced |
| CPY-0020 | recursion | `Objects/genericaliasobject.c:615 (ga_hash); Objects/genericaliasobject.c:619 (ga_hash)` | ga_hash (types.GenericAlias.__hash__) hashes both origin and __args__ with no recursion guard: hash(list[list[...]]) overflows the native C stack (SIGSEGV) | reproduced |
| CPY-0021 | recursion | `Objects/weakrefobject.c:199 (weakref_hash_lock_held); Objects/weakrefobject.c:206 (weakref_hash)` | weakref_hash_lock_held hashes its referent with no recursion guard -- inside a critical section: a weakref.ref SUBCLASS chain overflows the native C stack (SIGSEGV) | reproduced |
| CPY-0022 | recursion | `Objects/unionobject.c:170 (unionbuilder_add_single_unchecked)` | unionbuilder_add_single_unchecked hashes a caller-supplied member: `int \| x` is an unguarded ENTRY POINT into the hash graph, so union construction itself SIGSEGVs | reproduced |
| CPY-0023 | recursion | `Objects/genericaliasobject.c:482 (_Py_subs_parameters); Objects/genericaliasobject.c:427 (_Py_subs_parameters)` | _Py_subs_parameters is a SECOND unguarded self-recursion in genericaliasobject.c that the gh-154275 fix did not cover: subscripting an alias over a deeply-nested list arg SIGSEGVs | reproduced |
| CPY-0025 | tsan | `Objects/genericaliasobject.c:583 (ga_getitem); Objects/genericaliasobject.c:419 (_Py_subs_parameters)` | gh-153298 is an incomplete fix: ga_getitem still lazily initialises alias->parameters inline with no critical section, racing the guarded ga_parameters_lock_held accessor | reproduced |
| CPY-0026 | null-deref | `Objects/genericaliasobject.c:952 (ga_iternext)` | ga_iternext's NULL-check / Py_SETREF pair is not atomic: two threads calling next() on one GenericAlias iterator both pass the guard, and the loser executes Py_DECREF(NULL) -> SIGSEGV at ob_ref_local (0x0c) | reproduced |
| CPY-0027 | tsan | `Objects/descrobject.c:624 (descr_get_qualname); Objects/descrobject.c:625 (descr_get_qualname)` | descr_get_qualname lazily caches d_qualname with no synchronization anywhere in descrobject.c: read/write and write/write data races on a type-shared descriptor, plus a leaked str per lost race | reproduced |
| CPY-0028 | tsan | `Objects/odictobject.c:1886 (odictiter_reduce); Objects/odictobject.c:1891 (odictiter_reduce)` | odictiter_reduce copies the iterator struct -- including ob_mutex -- while the iterator's own critical section is held, so PySequence_List(&tmp) parks forever on a locked mutex copy nobody can unlock (free-threaded-build-only permanent hang) | reproduced |
| CPY-0029 | tsan | `Objects/funcobject.c:534 (func_get_annotation_dict); Objects/funcobject.c:552 (func_get_annotation_dict); Objects/funcobject.c:581 (PyFunction_GetAnnotations)` | gh-128714 is an incomplete fix: PyFunction_GetAnnotations reaches func_get_annotation_dict with no critical section, racing the clinic-guarded __annotations__ getter -- reproduced as a data race AND a dict driven to refcount -1 (SIGABRT) | reproduced |
| CPY-0030 | null-deref | `Objects/odictobject.c:1098 (_odict_popkey_hash)` | _odict_popkey_hash:1098 does Py_NewRef(failobj) with no NULL check while its guarded twin five lines below checks it: OrderedDict.pop(key) with an inconsistent __eq__ dereferences NULL -> SIGSEGV | reproduced |
| CPY-0031 | null-deref | `Objects/lazyimportobject.c:95 (lazy_import_name); Objects/lazyimportobject.c:89 (lazy_import_name); Objects/lazyimportobject.c:92 (lazy_import_name); Objects/lazyimportobject.c:70 (lazy_import_clear); (+1 more)` | lazy_import_name reads m->lz_from with no NULL check although tp_clear Py_CLEARs it: a lazy import that survives delete_garbage is reachable via gc.get_objects(), and repr() executes Py_NewRef(NULL) -> SIGSEGV | reproduced |
| CPY-0032 | memory-pattern | `Modules/_datetimemodule.c:1713 (format_ctime); Modules/_datetimemodule.c:3233 (date_new); Modules/_datetimemodule.c:5485 (datetime_new); Modules/_datetimemodule.c:474 (days_before_year)` | date_new/datetime_new's pickle-state path validates only the month byte, so year=0 reaches format_ctime's unguarded DayNames[wday] with a negative index -> global-buffer-overflow READ, .rodata disclosure and SIGSEGV | reproduced |
| CPY-0033 | error-path | `Modules/_zoneinfo.c:2314 (get_local_timestamp); Modules/_zoneinfo.c:2324 (get_local_timestamp); Modules/_zoneinfo.c:2334 (get_local_timestamp); Modules/_zoneinfo.c:2304 (get_local_timestamp)` | get_local_timestamp tests `hour/minute/second == -1` without `&& PyErr_Occurred()`, so the legitimate VALUE -1 returns the error sentinel with no exception set -> SystemError on release, Fatal Python error (SIGABRT) on a debug build | reproduced |
| CPY-0034 | refcount | `Modules/itertoolsmodule.c:3633 (count_nextlong); Modules/itertoolsmodule.c:3635 (count_nextlong); Modules/itertoolsmodule.c:3641 (count_nextlong)` | count_nextlong returns the borrowed lz->long_cnt without INCREF across a user-controlled PyNumber_Add: a re-entrant next() from __radd__ hands the same reference to two owners -> heap use-after-free | reproduced |
| CPY-0035 | refcount | `Modules/itertoolsmodule.c:3988 (zip_longest_next_lock_held); Modules/itertoolsmodule.c:4018 (zip_longest_next_lock_held)` | zip_longest_next_lock_held open-codes a clear-and-drop on a stale borrowed `it`: a re-entrant next() makes the outer frame Py_DECREF a reference it never owned -> use-after-free (both branches, :3988 and :4018) | reproduced |
| CPY-0036 | refcount | `Modules/itertoolsmodule.c:210 (batched_next); Modules/itertoolsmodule.c:196 (batched_next); Modules/itertoolsmodule.c:224 (batched_next)` | batched_next calls tp_iternext THROUGH a freed iterator after a re-entrant next() runs the GIL-build-only Py_CLEAR(bo->it) -- GIL build only; the #ifdef Py_GIL_DISABLED arm is already the fix | reproduced |
| CPY-0037 | refcount | `Modules/itertoolsmodule.c:1711 (islice_next); Modules/itertoolsmodule.c:1719 (islice_next); Modules/itertoolsmodule.c:1701 (islice_next); Modules/itertoolsmodule.c:1732 (islice_next)` | islice_next calls tp_iternext THROUGH a freed iterator after a re-entrant next() runs the unconditional Py_CLEAR(lz->it) -- unlike batched, the clear is NOT #ifndef-guarded, so BOTH the GIL and free-threaded builds crash | reproduced |
| CPY-0038 | tsan | `Modules/itertoolsmodule.c:361 (pairwise_next); Modules/itertoolsmodule.c:373 (pairwise_next); Modules/itertoolsmodule.c:366 (pairwise_next); Modules/itertoolsmodule.c:354 (pairwise_next)` | pairwise_next has no critical section: two threads both reach the exhaustion drop and double-Py_CLEAR po->it / po->old -> refcount -1, validate_refcounts abort (free-threaded build only) | reproduced |
| CPY-0039 | tsan | `Modules/itertoolsmodule.c:1732 (islice_next); Modules/itertoolsmodule.c:1705 (islice_next); Modules/itertoolsmodule.c:1710 (islice_next)` | islice_next has no critical section: two threads both reach `empty:` and double-Py_CLEAR lz->it -> refcount -1, validate_refcounts abort (free-threaded build only) | reproduced |
| CPY-0040 | tsan | `Modules/itertoolsmodule.c:691 (_grouper_next); Modules/itertoolsmodule.c:693 (_grouper_next); Modules/itertoolsmodule.c:676 (_grouper_next); Modules/itertoolsmodule.c:682 (_grouper_next)` | _grouper_next mutates its PARENT groupby's state with no critical section on either object: two threads both steal gbo->currvalue and race Py_CLEAR(gbo->currkey) against the assert/compare at :676-:682 -> SIGABRT on debug, NULL deref SIGSEGV with NDEBUG | reproduced |
| CPY-0041 | tsan | `Modules/itertoolsmodule.c:3678 (count_repr); Modules/itertoolsmodule.c:3691 (count_repr); Modules/itertoolsmodule.c:3696 (count_repr); Modules/itertoolsmodule.c:3682 (count_repr); (+1 more)` | gh-153908 (TSAN-0006) is an INCOMPLETE FIX: it made count_repr's lz->cnt read atomic and left all three plain reads of lz->long_cnt / lz->long_step, and the residual half is the dangerous one -- count_repr hands the borrowed long_cnt to PyObject_Repr while count_nextlong frees it -> TSan race + hard SEGV | reproduced |
| CPY-0044 | memory-pattern | `Modules/_struct.c:2274 (unpackiter_iternext); Modules/_struct.c:2275 (unpackiter_iternext); Modules/_struct.c:2334 (Struct_iter_unpack_impl); Modules/_struct.c:1990 (Struct___init___impl)` | Struct.iter_unpack's iterator captures buf.len at construction but re-reads s_size every step: re-initializing the live Struct returns uninitialized heap to Python (single-threaded, default GIL build) | reproduced |
| CPY-0045 | memory-pattern | `Modules/_struct.c:2249 (unpackiter_len); Modules/_struct.c:2321 (Struct_iter_unpack_impl)` | unpackiter_len divides by so->s_size, which Struct_iter_unpack_impl guarantees non-zero only at construction: re-init to an empty format makes list(it) die with SIGFPE on debug AND release | reproduced |
| CPY-0046 | memory-pattern | `Modules/_struct.c:2278 (unpackiter_iternext); Modules/_struct.c:2267 (unpackiter_iternext); Modules/_struct.c:2274 (unpackiter_iternext)` | unpackiter_iternext advances by so->s_size, so re-initializing the Struct to an empty format gives a zero stride: the iterator never exhausts and any accumulating consumer grows without bound | reproduced |
| CPY-0047 | tsan | `Modules/_struct.c:2270 (unpackiter_iternext); Modules/_struct.c:2271 (unpackiter_iternext); Modules/_struct.c:2278 (unpackiter_iternext); Modules/_struct.c:2263 (unpackiter_iternext)` | unpackiter_iternext re-reads self->so at :2278 after the exhaustion branch may have Py_CLEAR'd it: two threads sharing one iter_unpack iterator SEGV at NULL+0x20 on the free-threaded build (gh-154013, closed with a test only -- still live) | reproduced |
| CPY-0048 | refcount | `Modules/_struct.c:2371 (s_pack_internal); Modules/_struct.c:2429 (s_pack_internal); Modules/_struct.c:2436 (s_pack_internal); Modules/_struct.c:2377 (s_pack_internal); (+1 more)` | s_pack_internal walks a raw formatcode* cursor into s_codes across e->pack(), which runs a user __index__: a re-entrant Struct.__init__ PyMem_Frees the array under the live cursor (heap-use-after-free) | reproduced |
| CPY-0049 | memory-pattern | `Modules/_struct.c:2473 (Struct_pack_impl); Modules/_struct.c:2485 (Struct_pack_impl); Modules/_struct.c:2466 (Struct_pack_impl)` | Struct_pack_impl allocates the output writer with s_size at :2473 and re-reads self->s_size at :2485 after s_pack_internal ran user code: pack() returns a bytes far larger than what was written, filled with heap, and SIGSEGVs outright when the delta is large | reproduced |
| CPY-0050 | refcount | `Modules/_struct.c:2062 (s_unpack_internal); Modules/_struct.c:2084 (s_unpack_internal); Modules/_struct.c:2102 (s_unpack_internal); Modules/_struct.c:2068 (s_unpack_internal); (+1 more)` | s_unpack_internal walks a raw formatcode* cursor across PyErr_WarnEx for the deprecated 'F'/'D' codes, which runs warnings.showwarning: a re-entrant Struct.__init__ PyMem_Frees the array under the cursor (heap-use-after-free) | reproduced |
| CPY-0051 | pyerr-clear | `Modules/_struct.c:2667 (cache_struct_converter)` | cache_struct_converter's unnarrowed PyErr_Clear() after PyDict_SetItem swallows whatever the format key's __hash__ raised: struct.pack(StrSubclass(fmt), 1) returns successfully with a KeyboardInterrupt discarded | reproduced |
| CPY-0052 | memory-pattern | `Modules/_struct.c:2058 (s_unpack_internal); Modules/_struct.c:2101 (s_unpack_internal); Modules/_struct.c:2065 (s_unpack_internal)` | s_unpack_internal sizes its result tuple once with soself->s_len at :2058 and then fills it with a per-element counter bounded by the formatcodes' repeats, so a re-init that raises s_len overruns PyTuple_SET_ITEM at :2101 | static-confirmed |
| CPY-0053 | refcount | `Modules/_pickle.c:7414 (_pickle_Unpickler_find_class_impl); Modules/_pickle.c:7356 (_pickle_Unpickler_find_class_impl); Modules/_pickle.c:7346 (_pickle_Unpickler_find_class_impl); Modules/_pickle.c:3839 (fix_imports)` | _pickle_Unpickler_find_class_impl borrows global_name out of _compat_pickle.NAME_MAPPING and uses it at :7414 after PyImport_Import ran arbitrary Python -> heap-use-after-free | reproduced |
| CPY-0054 | refcount | `Modules/_pickle.c:6586 (load_extension); Modules/_pickle.c:6559 (load_extension); Modules/_pickle.c:6575 (load_extension); Modules/_pickle.c:7328 (_pickle_Unpickler_find_class_impl); (+1 more)` | load_extension borrows module_name/class_name out of copyreg._inverted_registry's value tuple and passes them into find_class(), whose FIRST statement (PySys_Audit) can free them -> heap-use-after-free reachable from sys.addaudithook alone | reproduced |
| CPY-0055 | null-deref | `Modules/_pickle.c:3502 (batch_dict_exact_impl); Modules/_pickle.c:3501 (batch_dict_exact_impl); Modules/_pickle.c:3495 (batch_dict_exact_impl); Modules/_pickle.c:3521 (batch_dict_exact_impl); (+1 more)` | REVERT REGRESSION: b770b23 restored two fixed defects in batch_dict_exact_impl (a failable assert at :3495 and an unchecked PyDict_Next -> Py_INCREF(NULL) at :3501-3502) AND deleted the NEWS entry, so the changelog has no record they are live; reproduced single-threaded on the default GIL build | reproduced |
| CPY-0056 | refcount | `Modules/_pickle.c:4689 (save); Modules/_pickle.c:5358 (Pickler_members); Modules/_pickle.c:4796 (dump)` | save() reads self->dispatch_table across PyMapping_GetOptionalItem, whose key-hash step runs the metaclass __hash__; `del p.dispatch_table` there frees the pickler's only reference and the lookup lands in a recycled dict - an attacker-chosen reduce function selected out of freed memory | reproduced |
| CPY-0057 | null-deref | `Modules/_csv.c:953 (Reader_iternext_lock_held); Modules/_csv.c:945 (Reader_iternext_lock_held); Modules/_csv.c:688 (parse_save_field); Modules/_csv.c:968 (Reader_iternext_lock_held)` | INCOMPLETE FIX of gh-145105: the `self->fields == NULL` guard was added only to the lineobj != NULL arm of Reader_iternext_lock_held; the mutually exclusive EOF arm still calls parse_save_field -> PyList_Append(NULL) -> SIGSEGV on the zero page | reproduced |
| CPY-0058 | error-path | `Modules/_zoneinfo.c:1073 (load_data); Modules/_zoneinfo.c:1063 (load_data); Modules/_zoneinfo.c:1103 (load_data); Modules/_zoneinfo.c:462 (zoneinfo_ZoneInfo_from_file_impl)` | load_data tests `PyLong_AsSsize_t(num) == -1` without `&& PyErr_Occurred()`, so a transition index of -1 takes the failure path with no exception set -> `Assertion 'PyErr_Occurred()' failed` at _zoneinfo.c:462 (SIGABRT), SystemError on release | reproduced |
| CPY-0059 | refcount | `Modules/_zoneinfo.c:2436 (find_in_strong_cache); Modules/_zoneinfo.c:2444 (find_in_strong_cache); Modules/_zoneinfo.c:2412 (remove_from_strong_cache); Modules/_zoneinfo.c:2466 (eject_from_strong_cache); (+1 more)` | find_in_strong_cache compares with PyObject_RichCompareBool(key, node->key) at :2436 while walking a bare PyMem_Malloc StrongCacheNode; a user __eq__ that calls ZoneInfo.clear_cache() frees the node under the comparison -> heap-use-after-free (this is the already-reported, still-OPEN gh-142782) | reproduced |
| CPY-0060 | refcount | `Modules/_zoneinfo.c:2583 (clear_strong_cache); Modules/_zoneinfo.c:2374 (strong_cache_node_free); Modules/_zoneinfo.c:2436 (find_in_strong_cache); Modules/_zoneinfo.c:2466 (eject_from_strong_cache); (+1 more)` | clear_strong_cache frees every node at :2583 and only THEN unpublishes the root at :2584; strong_cache_node_free's Py_XDECREF runs a key __del__ that re-enters ZoneInfo(...) and walks the already-freed chain -> heap-use-after-free | reproduced |
| CPY-0061 | tsan | `Modules/_collectionsmodule.c:1986 (dequeiter_next_lock_held); Modules/_collectionsmodule.c:2049 (dequeiter_len); Modules/_collectionsmodule.c:2137 (dequereviter_next_lock_held)` | dequeiter_next_lock_held writes it->counter plainly at :1986 while dequeiter_len reads it with FT_ATOMIC_LOAD_SSIZE at :2049 outside any critical section - an atomic read racing a non-atomic write, reproduced under TSan | reproduced |
| CPY-0062 | tsan | `Modules/_elementtree.c:2259 (elementiter_next); Modules/_elementtree.c:2260 (elementiter_next); Modules/_elementtree.c:2253 (elementiter_next)` | elementiter_next steals it->root_element at :2259 and NULLs it at :2260 with no critical section, so two concurrent next() calls both take the single owning reference -> _Py_NegativeRefcount / <object is freed>, SIGABRT on the free-threaded build | reproduced |
| CPY-0065 | null-deref | `Modules/_asynciomodule.c:2788 (_asyncio_Task_get_context_impl)` | _asyncio_Task_get_context_impl does Py_NewRef(self->task_context) with no NULL guard: Task.__new__(Task).get_context() -> SIGSEGV, while its two neighbouring methods guard the identical field family | reproduced |
| CPY-0066 | recursion | `Modules/_sqlite/row.c:239 (pysqlite_row_hash); Modules/_sqlite/row.c:235 (pysqlite_row_hash)` | pysqlite_row_hash descends both sqlite3.Row fields through PyObject_Hash with no recursion guard: Row(cur, (Row(cur, (...)),)) alternates with tuple_hash and overflows the native C stack (SIGSEGV) | reproduced |
| CPY-0067 | tsan | `Modules/arraymodule.c:3247 (arrayiter_next); Modules/arraymodule.c:3248 (arrayiter_next)` | arrayiter_next drops its owning array reference with a plain store + plain DECREF and no critical section: a shared array iterator double-DECREFs the array under free-threading (refcount -1, SIGABRT) | reproduced |
| CPY-0068 | refcount | `Objects/typeobject.c:9332 (type_ready_inherit); Objects/typeobject.c:9336 (type_ready_inherit); Objects/typeobject.c:8814 (overrides_hash); Objects/typeobject.c:4768 (type_new_set_classcell)` | type_ready_inherit holds a borrowed tp_mro across overrides_hash(), which dispatches a user __eq__ that can free it | reproduced |
| CPY-0069 | refcount | `Objects/typeobject.c:12369 (recurse_down_subclasses); Objects/typeobject.c:12377 (recurse_down_subclasses); Objects/typeobject.c:12386 (recurse_down_subclasses); Objects/typeobject.c:9790 (remove_subclass)` | recurse_down_subclasses holds a borrowed tp_subclasses across PyDict_Contains, whose user __eq__ can free the dict mid-iteration | reproduced |
| CPY-0070 | null-deref | `Objects/typeobject.c:1966 (type_set_bases_unlocked); Objects/typeobject.c:1965 (type_set_bases_unlocked); Objects/typeobject.c:11938 (update_one_slot)` | type_set_bases_unlocked never branches on add_all_subclasses's result: the rollback is skipped and __bases__ is committed while MemoryError is raised | reproduced |
| CPY-0071 | recursion | `Objects/typeobject.c:7117 (merge_class_dict); Objects/typeobject.c:8526 (object___dir___impl); Objects/typeobject.c:8478 (type___dir___impl)` | merge_class_dict recurses over __bases__ with no recursion guard: dir(obj) on a cyclic __class__.__bases__ is an uncatchable SIGSEGV | reproduced |
| CPY-0072 | tsan | `Objects/typeobject.c:12136 (fixup_slot_dispatchers); Objects/typeobject.c:12056 (update_one_slot); Objects/typeobject.c:9581 (type_ready_set_bases); Objects/typeobject.c:4958 (type_new_impl)` | fixup_slot_dispatchers rewrites the slot table with plain stores AFTER PyType_Ready published the type into every base's tp_subclasses | reproduced |
| CPY-0073 | null-deref | `Objects/typeobject.c:6494 (set_flags_recursive); Objects/typeobject.c:6522 (_PyType_SetFlagsRecursive); Objects/typeobject.c:778 (_PyType_GetSubclasses); Objects/typeobject.c:799 (_PyType_GetSubclasses)` | _PyType_SetFlagsRecursive allocates with the world stopped and discards the failure from a void function, leaving MemoryError pending and subclasses unflagged | reproduced |
| CPY-0074 | pyerr-clear | `Objects/typeobject.c:6183 (find_name_in_mro); Objects/typeobject.c:11942 (update_one_slot)` | find_name_in_mro's bare PyErr_Clear feeds update_one_slot a NULL slot_value, silently clearing tp_init: C(1,2,3) is accepted after del C.__init__ | reproduced |
| CPY-0075 | pyerr-clear | `Objects/typeobject.c:11090 (has_dunder_getitem); Objects/typeobject.c:11108 (slot_tp_iter)` | has_dunder_getitem discards lookup_maybe_method's -1 and slot_tp_iter then overwrites the live exception with TypeError, __context__ = None | reproduced |
| CPY-0076 | pyerr-clear | `Objects/typeobject.c:2405 (type_repr); Objects/typeobject.c:7490 (object_repr)` | type_repr and object_repr bare-clear whatever a user __eq__ raised during type_module()'s dict lookup | reproduced |
| CPY-0077 | pyerr-clear | `Objects/typeobject.c:6149 (find_name_in_mro); Objects/typeobject.c:6158 (find_name_in_mro)` | find_name_in_mro's bare PyErr_Clear turns a user __eq__ exception into a wrong AttributeError | reproduced |
| CPY-0078 | pyerr-clear | `Objects/typeobject.c:7609 (same_slots_added)` | same_slots_added collapses PyObject_RichCompareBool's tri-state, replacing a user exception with TypeError and __context__ = None | reproduced |
| CPY-0079 | null-deref | `Objects/dictobject.c:4494 (copy_lock_held_untracked); Objects/dictobject.c:4489 (copy_lock_held_untracked); Objects/dictobject.c:4492 (copy_lock_held_untracked); Objects/dictobject.c:5362 (anydict_new_untracked)` | An assert() dereferences an unchecked allocation result in dictobject.c, and the UB lets the optimizer delete the inlined NULL check | static-confirmed |
| CPY-0080 | null-deref | `Objects/typeobject.c:12793 (super_descr_get)` | super_descr_get passes a NULL su->type into PyObject_CallFunctionObjArgs, silently truncating a 2-argument call to 0 arguments | reproduced |
| CPY-0081 | null-deref | `Objects/typeobject.c:12839 (super_init_without_args); Objects/typeobject.c:12840 (super_init_without_args)` | super_init_without_args casts localsplus[0] to PyCellObject* on co_localspluskinds alone; the PyCell_Check is a debug-only assert | reproduced |
| CPY-0082 | tsan | `Objects/typeobject.c:1745 (type_set_abstractmethods); Objects/typeobject.c:12523 (PyType_Freeze)` | Two callers enter types_stop_world() holding TYPE_LOCK without type_lock_prevent_release(), so the detach silently drops the lock | static-confirmed |
| CPY-0083 | refcount | `Objects/typeobject.c:783 (_PyType_GetSubclasses); Objects/typeobject.c:788 (_PyType_GetSubclasses); Objects/typeobject.c:9790 (remove_subclass)` | _PyType_GetSubclasses holds a borrowed tp_subclasses across PyDict_Next, guarded only by a stale GIL-era comment | static-confirmed |
| CPY-0084 | refcount | `Objects/typeobject.c:1195 (_PyType_Modified_Unlocked); Objects/typeobject.c:1223 (_PyType_Modified_Unlocked)` | _PyType_Modified_Unlocked holds a borrowed tp_subclasses across a type-watcher callback and PyErr_FormatUnraisable("%R") | static-confirmed |
| CPY-0085 | pyerr-clear | `Objects/typeobject.c:11227 (slot_tp_finalize); Objects/typeobject.c:11243 (slot_tp_finalize)` | slot_tp_finalize restores a saved exception over a live one raised by __del__'s descriptor lookup, with zero unraisable reports | reproduced |
| CPY-0086 | uninit-dealloc | `Objects/typeobject.c:5623 (type_from_slots_or_spec); Objects/typeobject.c:5562 (type_from_slots_or_spec); Objects/typeobject.c:7034 (type_dealloc)` | type_from_slots_or_spec rejects a custom metaclass tp_new but dispatches through that metaclass's unvalidated tp_alloc, leaving ht_slots uninitialized | static-confirmed |
| CPY-0087 | recursion | `Objects/typeobject.c:12359 (update_subclasses); Objects/typeobject.c:1206 (_PyType_Modified_Unlocked); Objects/typeobject.c:1431 (assign_version_tag); Objects/typeobject.c:1854 (mro_hierarchy_for_complete_type); (+1 more)` | Five class-hierarchy descents in typeobject.c recurse over a Python-mutable graph with no recursion guard; the file has zero guard macros | reproduced |
| CPY-0088 | memory-pattern | `Objects/typeobject.c:5290 (type_from_slots_or_spec)` | type_from_slots_or_spec negates spec->basicsize without a range check: INT_MIN is signed-overflow UB and lands a negative tp_basicsize in PyType_Ready | static-confirmed |
| CPY-0089 | null-deref | `Objects/typeobject.c:592 (lookup_tp_bases); Objects/typeobject.c:5946 (type_from_slots_or_spec); Objects/typeobject.c:709 (managed_static_type_state_get); Objects/typeobject.c:4032 (_PyObject_SetDict)` | Four latent NULL/assert-only guards in typeobject.c: unchecked tp_bases INCREF, assert-only tp_mro, unasserted managed-static state, and Py_NewRef on a deletable __dict__ | static-confirmed |
| CPY-0090 | tsan | `Objects/typeobject.c:1572 (type_set_name); Objects/typeobject.c:1598 (type_set_qualname)` | type.__name__ / __qualname__ assignment collapses 4,141x under free-threading; object_set_class got the uniquely-referenced fast path and these did not | reproduced |

## Known false-positive classes — DO NOT re-report (justify if you flag one)

The full taxonomy lives in `data/cpython_non_bugs.md`; it is reproduced here so every agent sees it inline.

# CPython false-positive taxonomy (cpython-review-toolkit)

The precision-decay guard for the informed-explore loop. Each entry is a pattern
the scanners *can* surface but that is usually **not** a bug in CPython's own
code. During triage, skip these classes — or explicitly justify why this instance
is different. Add to this file whenever a review confirms a new FP class.

---

## PyErr_Clear / exception state

- **`PyErr_Clear()` after a sentinel-returning lookup is idiomatic.** After
  `PyObject_GetAttr` / `PyDict_GetItemWithError` / `PyMapping_GetOptionalItem`
  where a missing key is expected, clearing an `AttributeError`/`KeyError` is
  correct — *outside* the destructor family. The `pyerr-clear-auditor` is scoped
  to dealloc/clear/finalize precisely to avoid this; a hit there is real.
- **A destructor that already saves/restores** (`PyErr_GetRaisedException` /
  `PyErr_Fetch` / `PyErr_WriteUnraisable`) is fine — but verify the save/restore
  actually brackets the flagged clear in a large function (whole-function
  suppression can hide a second, unguarded clear).

## Recursion guards

- **Guarded by the dispatcher — but NOT for hash.** A leaf slot reached only
  through `PyObject_Repr` (`Objects/object.c:759`), `PyObject_Str` (`:800`) or
  `PyObject_RichCompare` (`:1099`) is safe *if it is never reached directly*,
  because those three do wrap `_Py_EnterRecursiveCallTstate`. Confirm the call
  graph before dismissing.

  **`PyObject_Hash` (`Objects/object.c:1158`) has NO recursion guard.** Verified
  against main @ 3.16.0a0. A `tp_hash` slot that descends into element hashes is
  therefore unguarded at *every* level and overflows the native C stack (SIGSEGV,
  not a catchable `RecursionError`). Never dismiss a hash-descent finding as
  "dispatcher-guarded": that asymmetry between the four dispatchers is exactly
  what makes `tuple_hash` (gh-154318 / CPY-0001), `union_hash`, `ga_hash` and
  `frozendict_pair_hash` real bugs. This entry previously listed `PyObject_Hash`
  among the guarded dispatchers, which was factually wrong and would have
  suppressed the entire true-positive class.
- **Non-nestable receiver.** A `*_hash`/`*_repr` on a type whose elements can
  never be the same or another container (e.g. a code object's fixed fields) can
  descend without a guard because the depth is bounded. ACCEPTABLE — but state
  the bound.
- **Iterative deallocation bounds a `tp_dealloc` — but do NOT look for
  `Py_TRASHCAN_BEGIN` to decide that.** On main (verified @ 3.16.0a0) the old
  macros are **empty backwards-compat shims** — `Include/cpython/object.h:446-447`,
  literally `#define Py_TRASHCAN_BEGIN(op, dealloc)` with an empty body — and
  **zero call sites remain in `Objects/` or `Modules/`**. The live mechanism is
  automatic inside `_Py_Dealloc`, via `_PyTrash_thread_deposit_object()` /
  `_PyTrash_thread_destroy_chain()`.

  So the correct test is *not* "is this dealloc trashcan-protected?" (nothing is,
  by that marker — an agent applying the old test finds no marker anywhere and
  wrongly promotes every dealloc finding). The test is **what the descent goes
  through**: a `tp_dealloc` that recurses only by `Py_DECREF`-ing contained
  objects is bounded by the automatic chain and is a FP; a dealloc that recurses
  some *other* way is not covered and can still overflow the C stack — see
  `gh-149146 tuple_dealloc` (recursion during MemoryError unwind), which is a
  real catalogued bug.

## Uninitialized dealloc

- **Zeroing allocator.** `PyType_GenericAlloc` / `_PyType_AllocNoTrack` /
  `*_GC_Calloc` zero the object; a following early free is safe. The scanner
  excludes these, but a wrapper macro may hide one.
- **`type->tp_alloc(type, n)` is NOT unconditionally zeroing** — *amended
  2026-07-25*. An earlier draft of this entry listed `tp_alloc` alongside
  `PyType_GenericAlloc` as if the slot always zeroed. It does not: a type may
  install its own `allocfunc`. `Modules/_datetimemodule.c` installs two —
  `time_alloc` (`:879`, wired positionally at `:5382`) and `datetime_alloc`
  (`:891`, at `:7349`) — both `PyObject_Malloc` + `_PyObject_Init` with no
  `memset`, and the file's own comment (`:861-862`) says so: *"All data members
  remain uninitialized trash."* `time_dealloc`/`datetime_dealloc` then `switch`
  on the scalar `hastzinfo` to decide whether to `Py_XDECREF(self->tzinfo)` —
  the blake2 `impl` shape (gh-152851) exactly. **There is no live bug there
  today**: all nine `tp_alloc` call sites in that file set `hastzinfo` in the
  statement immediately after the allocation. But *resolve the slot* before
  dismissing on this ground; do not dismiss on the spelling. Tree-wide at
  3.16.0a0 these are the only two non-zeroing `tp_alloc`s (`bytes_alloc`,
  `_PyType_AllocNoTrack` and `PyType_GenericAlloc` all zero), and
  `scan_uninit_dealloc.py` now detects them mechanically
  (`_nonzeroing_tp_allocs`).
- **`tp_dealloc` guards each member with `Py_XDECREF`** *and* the members were
  NULL-initialized before the failing step — Py_XDECREF(NULL) is a no-op, so no
  crash. Only a member left as *garbage* (not NULL) at the free point is a bug.

## NULL checks / error paths

- **Infallible-by-construction returns.** `Py_None`/`Py_True`/`Py_False`, interned
  singletons, and `_Py_ID(...)` never return NULL; a missing check is not a bug.
- **Checked via a macro the scanner doesn't model.** `Py_SETREF` / `Py_XSETREF`
  and `Py_CLEAR` handle NULL internally; assignments through them are safe.
  `Py_XDECREF(NULL)` is likewise a documented no-op: a `Py_XDECREF` on a path
  where the pointer is provably NULL is dead code, not a crash.
- **Result is returned directly — NULL propagation *is* the error handling.**
  The dominant FP class in `Objects/`: 9 of 21 candidates in the sample run,
  ~43%. `res = PyUnicode_FromFormat(...); return res;` is the canonical `tp_repr`
  body and is correct — the caller sees NULL and an exception is already set.
  Only report an unchecked value that is *dereferenced*. Exemplars:
  `Objects/cellobject.c:124` `cell_repr`, `Objects/descrobject.c:615`
  `calculate_qualname`, `Objects/weakrefobject.c:226`/`:231` `weakref_repr`,
  `Objects/weakrefobject.c:785` `proxy_iternext` (`PyIter_Next` → NULL is the
  StopIteration protocol, not an error).
- **The NULL check is the loop condition.** `while ((pair = PyIter_Next(it)) != NULL)`
  and `for (key = PyIter_Next(i); key; key = PyIter_Next(i))` test the value in
  the controlling expression, on the same line as the assignment. Exemplars:
  `Objects/odictobject.c:2234`, `:2314`, `Objects/dictobject.c:4346` `dict_merge`.
- **The check is on the struct-field lvalue.** `ub->args = PyList_New(0)` is
  checked as `if (ub->args == NULL)`; a scanner that captured only the trailing
  identifier (`args`) looks for the wrong name. Exemplars:
  `Objects/unionobject.c:145`, `:174`.
- **The check is on an aliased lvalue.** `args = tuple_args = PySequence_Tuple(args);`
  is checked two lines later as `if (args == NULL)` — the *outer* target, not the
  innermost one. Exemplars: `Objects/genericaliasobject.c:192`, `:460`.
- **Correct by construction / interprocedural.** The callee is NULL-tolerant
  (`Objects/genericaliasobject.c:647` passes an unchecked `obj` to `set_orig_class`,
  whose first statement is `if (obj != NULL)`), or the check *is* the return
  expression (`Objects/tupleobject.c:1068`: `*pv = PyTuple_New(newsize);
  return *pv == NULL ? -1 : 0;`). Out of reach for single-function analysis;
  establish the callee's contract before promoting.
- **Out-parameter fills are checked through the pointer, or by the caller.**
  `*result = PyObject_GetItem(obj, key); if (*result) return 1;`
  (`Objects/abstract.c:215` `PyMapping_GetOptionalItem`) is checked; so is
  `*myerrno`/`if (!*myerrno)` (`Objects/exceptions.c:2099`). Where there is no
  local check at all (`Objects/unicode_format.c:716`), the obligation belongs to
  the caller — verify there before reporting.
- **`sizeof *x` inside the allocation's own argument list is not a use of the
  result.** `struct unpacker *x = PyMem_Malloc(sizeof *x);` is the standard
  CPython idiom. Ten of the 37 `Modules/` candidates in the calibration run were
  this. The same applies to `x = PyMem_Malloc(n * sizeof(x[0]))`.

## Refcounts

- **Borrowed ref under a known-live owner.** A borrowed item is safe across a call
  if a strong reference is provably held elsewhere for the duration (e.g. the
  container is a local the callee cannot reach). Establish the owner before
  dismissing a borrowed-across-call finding.
- **Stolen-ref APIs used correctly.** `PyList_SET_ITEM`/`PyTuple_SET_ITEM` on a
  freshly-created, not-yet-published container are the normal fast path.

## Free-threading (when the FT detectors land)

- **Immortal objects** (`_Py_IMMORTAL_REFCNT`) are not raced by refcount ops.
- **Access under `Py_BEGIN_CRITICAL_SECTION`** for the relevant object is
  protected; a plain read is only a race if a *concurrent* writer exists without
  the same critical section.

## Error paths — `unchecked_return` FP classes (scan_error_paths.py, 2026-07)

Measured on a 14-file `Objects/` sample: **28 of 28** `unchecked_return`
candidates were false positives, in five mechanical classes. The scanner now
suppresses all of them; they are recorded here because an agent reading code by
hand meets the same shapes.

- **Value returned directly (46% of that sample's noise).**
  `res = PyUnicode_FromFormat(...); ...; return res;` needs no NULL check — the
  callee's exception propagates untouched. This also covers the wrapped form,
  `return set_orig_class(obj, self);` (`Objects/genericaliasobject.c:647`).
- **Positive-form and loop-condition checks.** `if (v)`, `if (*v)`,
  `if (v != NULL)`, `while ((v = PyIter_Next(it)) != NULL)`,
  `for (k = PyIter_Next(it); k; k = PyIter_Next(it))` and
  `return v == NULL ? -1 : 0` are all checks. Only `== NULL`, `!v` and `== 0`
  used to count as one.
- **Aliased assignment.** `a = b = API(...)` — the check may be written against
  either name (`args = tuple_args = PySequence_Tuple(args); if (args == NULL)`).
- **Struct-member destination.** `ub->args = PyList_New(0)` is checked as
  `if (ub->args == NULL)`; a bare-identifier LHS capture misses it.
- **Out-parameter store.** `*result = PyObject_GetItem(obj, key);` and
  `*method = PyObject_GetAttr(obj, name);` hand the NULL check to the caller by
  contract (`Objects/abstract.c:215`, `Objects/object.c:1670`).
- **NULL-tolerant consumer.** `PyModule_Add` / `PyModule_AddObject` /
  `PyModule_AddObjectRef` reject NULL explicitly and propagate the pending
  exception (`Python/modsupport.c:602`); `Py_XDECREF`, `Py_CLEAR` and
  `Py_XSETREF` are NULL-safe. Passing an unchecked result to one of these is the
  house idiom, not a bug.

## Allocators — who owes the `MemoryError`

- **Allocators that raise for you.** `PyObject_New` / `PyObject_NewVar` /
  `PyObject_GC_New` / `PyObject_GC_NewVar` / `PyType_GenericAlloc` / `tp_alloc`
  (which raises even in the hand-written non-zeroing form — `time_alloc`
  returns `PyErr_NoMemory()`; *raising* and *zeroing* are separate questions,
  see the Uninitialized-dealloc section),
  and the object constructors (`PyList_New`, `PyTuple_New`, `PyDict_New`, ...)
  set `MemoryError` themselves. A failure branch that just returns the sentinel
  after one of these is correct.
- **Allocators that do not.** Only the raw family —
  `PyMem_Malloc`/`Calloc`/`Realloc`, `PyMem_RawMalloc`/`RawCalloc`/`RawRealloc`,
  `PyObject_Malloc`/`Calloc`/`Realloc`, and plain `malloc`/`calloc`/`realloc` —
  needs an explicit `PyErr_NoMemory()`. **`PyMem_New` and `PyMem_Resize` belong
  to this group**: they are plain macros over `PyMem_Malloc` / `PyMem_Realloc`
  (`Include/pymem.h:63,73`) and do *not* raise. An earlier draft of this
  taxonomy listed `PyMem_New` as exempt; that was wrong.
- **Obligation deferred to the caller.** A thin static allocation helper
  (`list_allocate_array`, `new_values` in `Objects/`) may return NULL and let
  every caller raise. Check the call sites before reporting the helper.
- **The raw memory layer cannot raise.** `Objects/obmalloc.c` (`_PyMem_Strdup`,
  `arena_map_get`, `new_arena`, `_PyMem_init_obmalloc`, ...) runs where no
  thread state need exist; returning NULL without an exception is the contract
  there, not a bug. Six of the ten `alloc_null_no_memerror` candidates in
  `Objects/` are this class.

## PyErr_Clear — widened-scanner FP classes

Added after `scan_pyerr_clear` was widened past the destructor family; measured
on main @ 3.16.0a0 over `Objects/` + `Modules/` + `Python/`.

The "sentinel-returning lookup" entry above is correct and load-bearing — it
predicted the split almost exactly: **47 of the 86 attributed clears in
`Objects/` are `PyErr_ExceptionMatches`-narrowed** and must stay suppressed. Two
refinements and five new classes came out of measuring the widened rules.

- **Refinement: an *unfiltered* clear after a call that runs arbitrary Python is
  NOT in the idiomatic class.** `PyObject_Hash`, `PyObject_GetBuffer`,
  `PyNumber_AsSsize_t`, `PyObject_Call*`, an import hook, or a slot dispatched
  off a runtime object (`pb->bf_getbuffer`) can raise *anything*. Clearing
  without a narrowing test discards `MemoryError` / `KeyboardInterrupt` /
  `RecursionError` along with the expected `TypeError`. Require an
  `ExceptionMatches` narrowing before calling one of these acceptable.
  (`Objects/unionobject.c:172` is the archetype; `set_orig_class` in
  `Objects/genericaliasobject.c` is the guarded twin.)
- **Refinement: whole-function save/restore suppression is unsound**, as the
  entry above warns. Measured: it also hides a *second* clear even when the pair
  brackets the first — `xibufferview_dealloc` (`Modules/_interpretersmodule.c`)
  has clears at `:175` and `:183` and only one was ever reported, because the
  shared `deduplicate_findings` collapses same-file same-type findings by a
  *normalized* detail string that erases function names and line numbers.
  Distinct sites in one file must not be deduplicated that way.

New FP classes, each one measured as a real false positive of a widened rule:

- **Early-return guard clause.** CPython narrows by guard clause far more often
  than by nesting: `if (!PyErr_ExceptionMatches(PyExc_KeyError)) return -1;` then
  `PyErr_Clear();`. The clear has *no enclosing conditional* but is fully
  dominated by the negation of the guard. Any success-path rule must treat
  preceding terminating `if`s as dominating conditions, in both polarities —
  `if (key != NULL) return key;` before a clear equally proves the call failed.
  (`Objects/abstract.c:223`, `Objects/typeobject.c:9748`,
  `Objects/moduleobject.c:1354/1390`, `Objects/memoryobject.c:3006`.)
- **Macro-hidden return.** `Py_RETURN_TRUE` / `Py_RETURN_NONE` /
  `Py_RETURN_NOTIMPLEMENTED` / `Py_UNREACHABLE()` / a module-local `FAIL(...)`
  parse as ordinary expression statements, so a guard clause ending in one does
  not *look* terminating to tree-sitter. Treat them as terminators.
  (`Modules/_interpretersmodule.c:1346`, `Modules/_testcapimodule.c:766`.)
- **Non-identifier lvalue.** The tested value is often a struct member or a
  dereferenced out-parameter, not a local: `interp->dict = PyDict_New();
  if (interp->dict == NULL)`, `*pmod = parse(...); if (*pmod == NULL)`. Matching
  assignment targets by bare identifier makes these read as success paths.
  (`Python/pystate.c:1280`, `:2125`, `Python/pythonrun.c:302`.)
- **File-local status helper.** `if (random_seed_urandom(self) < 0)` is an error
  test even though the callee is not `Py`-prefixed. Recognize *any* call whose
  result is compared against `NULL`/`0`/`-1` as an error test — but do **not**
  extend that to a bare predicate call with no comparison
  (`if (!equiv_shape(vv, ww))` sets nothing and is a true positive).
  (`Modules/_randommodule.c:305`, `Modules/_remote_debugging/frame_cache.c:211`.)
- **Wrong-polarity branch attribution.** A clear inside `if (module) { ... }` is
  in the branch taken when the import *succeeded*; it is not reacting to that
  failure. Attributing it to the enclosing call's failure is an FP. Only the
  innermost enclosing branch counts, and only on its failure side.
  (`Modules/_testcapimodule.c:815`.)
- **Sibling-branch exception consumer.** `if (tracebacks_enabled)
  PyErr_FormatUnraisable(...); else PyErr_Clear();` — the function exists to
  consume the pending exception one way or the other. A report/chain API in the
  clear's own `if`/`else` means the clear is deliberate.
  (`Modules/_sqlite/connection.c:929`.)
- **Statically-known type slot.** `PyUnicode_Type.tp_hash(key)` is a fixed C
  function; no user code runs. Only a slot read off a *runtime* object
  (`pb->bf_getbuffer`, `Py_TYPE(x)->tp_descr_get`) is arbitrary Python.
  (`Objects/dictobject.c:1336`.)
- **The API's own implementation.** `Python/errors.c:545 PyErr_Clear()` is the
  public wrapper around `_PyErr_Clear(tstate)`. Scanners that match the private
  alias must not flag the definition.
- **Diagnostic and test-support code is dense with deliberate swallows.**
  `Modules/_testcapimodule.c`, `Modules/_xxtestfuzz/`, `Python/traceback.c`,
  `Python/pythonrun.c`'s interactive-prompt helpers and `Python/errors.c`'s
  unraisable writers account for most of the tree-wide hits of the widened
  rules. They are genuine instances of the pattern and almost always POLICY or
  ACCEPTABLE. `Modules/_testcapimodule.c return_null_without_error` clears
  *precisely so* `_Py_CheckFunctionResult` can detect the resulting bug.

## Uninitialized dealloc (v0.8 additions)

- **Wrapper constructor that NULL-inits every slot.** An allocation routed
  through a project-local helper that zeroes all members —
  `PyStructSequence_New` (`Objects/structseq.c:65`, all `n_fields` slots NULLed
  before anything fallible), `PyTuple_New`, `PyList_New(0)` — is safe, and the
  scanner never sees the raw allocator at the call site. `Objects/structseq.c`
  is a **silent correct negative** for this shape, not an unexamined file. Do
  not re-hunt it.
- **Shared `fail:` label.** A `Py_XDECREF(var)` on an error label reachable only
  *before* the allocation (var still NULL) or *after* every member is written is
  not a finding — there is no member write after the free.
  `Objects/typeobject.c:11343 slot_bf_getbuffer` is the exemplar.
- **Non-NULL sentinel initializer.** `new->ob_exports = 0;`
  (`Objects/bytearrayobject.c:164`) initializes the member just as effectively
  as `= NULL`; a gate that only matches `= NULL` mis-reads it as uninitialized.
- **Scalar member left unset.** A `Py_ssize_t` / `int` / enum member written
  after the free is only a bug if the destructor *acts on it* — decrefs it,
  switches on it, or uses it as a bound over an array it decrefs. Members the
  destructor never reads (`_sre.c:2955` `pos`/`endpos`/`lastindex`,
  `_decimal.c:1443` `tstate`/`modstate`, `_ssl.c:942` `socket_type`) are FPs.
  The converse is *not* an FP: blake2's `impl` enum (gh-152851) and
  `elementiter_dealloc`'s `parent_stack_used` loop bound are scalars that do
  drive teardown.
- **Plain `#ifdef` block with no `#else`.** Only one arm of a preprocessor
  conditional is ever compiled, so `co->_co_unique_id = …` inside
  `#ifdef Py_GIL_DISABLED … #endif` *does* dominate a later `Py_DECREF(co)`
  outside the block (`Objects/codeobject.c:736`). Dominance is broken only by a
  *different arm* of the same group.
- **A clean OOM sweep is not an exoneration.** gh-151815 (`template_iter`)
  survives 60/60 clean `MemoryError` runs and is still live at 3.16.0a0: the
  shape crashes only on a *dirty* recycled block, and `templateiter_clear` NULLs
  both members before `tp_free`, so a same-type block always returns clean.
  Record such results as "unstable trigger", never as "fixed".

## Memory patterns (v0.8 additions)

- **`bounded-by-an-existing-allocation`.** `Py_SIZE(x)`, `PyTuple_GET_SIZE(x)`,
  `PyList_GET_SIZE`, `PyBytes_GET_SIZE`, `PyByteArray_GET_SIZE`,
  `PyUnicode_GET_LENGTH` and the other concrete-type accessors return the length
  of an object that is *already in memory*, so `n * sizeof(ptr)` cannot overflow
  `Py_ssize_t` — the container itself would have had to exceed
  `PY_SSIZE_T_MAX / elemsize` bytes. `Objects/call.c:491` and
  `Objects/listobject.c:2985` are the exemplars; this was 100% of
  `alloc_size_overflow`'s noise on `Objects/`. **Not** in this class:
  `PyLong_As*` results, `PyNumber_AsSsize_t`, `PyArg_Parse*` outputs, the
  protocol-dispatched `PyObject_Length` / `PySequence_Size` /
  `PyObject_LengthHint` (a Python `__len__` may return any `Py_ssize_t` with no
  memory behind it), and anything read out of a mutable type dictionary.
- **Narrow-typed `nitems`.** On LP64 an `int` / `short` / `char` count cannot
  make `nitems * tp_itemsize` wrap a 64-bit `size_t`, so
  `PyObject_GC_NewVar(..., slots)` with `int slots` is safe
  (`Objects/frameobject.c:2119`, `genobject.c:1100`, `genobject.c:1170`,
  `memoryobject.c:649`).
- **A `< 0` sign check is not an overflow guard.** `PyStructSequence_New` has
  `if (size < 0) return NULL;` and still hands `2**62` to `_PyObject_VAR_SIZE`.
  Only a `PY_SSIZE_T_MAX / elemsize` division check or
  `__builtin_mul_overflow` counts.
- **`PyObject_GC_UnTrack` (the function) is untracked-tolerant, not NULL-safe.**
  It re-checks `_PyObject_GC_IS_TRACKED`, which dereferences its argument
  unconditionally. A `gc_untrack_without_track` candidate whose *own type's*
  `tp_dealloc` uses the function form is an ACCEPTABLE true negative
  (`templateobject.c:232`, `interpolationobject.c:218`, `codeobject.c:751`,
  `listobject.c:262`, `context.c:895`) — but do not restate the reason as
  "NULL-safe".
- **A sibling type's macro is not this type's macro.** A file-level
  `_PyObject_GC_UNTRACK` test lets a safe constructor through whenever any other
  type in the same file uses the macro; `PyList_New` was reported only because
  `listiter_dealloc` uses it while `list_dealloc` does not. Resolve the type.

## Refcounts — borrowed-ref-across-call (added from the `scan_refcounts` rebuild)

Every entry below was a *measured* false positive of the borrowed-ref rules on
CPython main @ 3.16.0a0, and each one is now gated in the scanner. They are
listed here because the same shapes will fool a human reading code.

- **Mutually exclusive preprocessor branches.** A `Py_XDECREF(v)` before an
  `#else` and a use of `v` after it never run in the same build.
  `Objects/dictobject.c`'s `Py_GIL_DISABLED` lookups are the canonical case:
  the free-threaded branch DECREFs the out-parameter, the default branch is the
  one that assigns it. Never reason across a `#if`/`#else`/`#endif` boundary.
- **Out-parameter re-binding.** `Py_XDECREF(file); PySys_GetOptionalAttr(&_Py_ID(stderr), &file)`
  releases the *previous* value; `&file` overwrites it. Same for `_PyErr_Fetch(tstate, &exc_type, ...)`.
  A `&var` handed to a call is an assignment.
- **Shadowed re-declaration.** `Py_XDECREF(loader); if (!has_loader) { PyObject *loader = ...; }`
  — the inner `loader` is a different variable (`Python/pylifecycle.c` `add_main_module`).
- **Macro-hidden assignment.** `Py_CLEAR(obj); ... ASSIGN_PTR(obj, PyObject_CallMethod(...))`
  re-binds `obj` inside a SCREAMING_CASE macro (`Modules/_decimal`).
- **`Py_CLEAR` NULLs its own operand.** A later read of that same variable is a
  NULL read, not a dangling one. Only an *alias* is still exposed.
- **A struct member is not a local.** `self->last` must not read as a use of a
  local named `last` (`Modules/_elementtree.c` `treebuilder_handle_end`).
- **The reference was published before it was dropped.** `PyModule_AddType(m, T); Py_DECREF(T);`
  then `T->tp_dict` — the module holds it. Generalises the existing
  "borrowed under a known-live owner" entry to registration APIs.
- **The INCREF is written against the source, not the destination.**
  `Py_INCREF(lz->lz_attr); fromlist = lz->lz_attr;` makes `fromlist` an owner
  even though no `Py_INCREF(fromlist)` appears (`Python/import.c`).
- **Owner swap, not stale drop.** `old = self->f; self->f = new; Py_XDECREF(old);`
  is correct: once the slot is overwritten the local is the sole owner of the
  old value (`defaultdict.__init__`). The *dangerous* variant is the slot
  cleared to `NULL` and the stale local dropped — that is a re-entrancy
  double-DECREF, and `Py_CLEAR` is the fix.

## Refcounts — new-reference balance

- **Ownership transfer is not a leak.** `*p_result = result` (out-parameter),
  `ctx->slots = new_slots` (context struct), `listrepr = tmp` (plain alias) and
  `value->_m_dict = (struct cached_m_dict){ .copied=copied }` (compound
  literal) all move the reference somewhere the function no longer owns it.
- **`Py_BuildValue`'s `N` code consumes its argument** even though the call
  itself returns a new reference: `Py_BuildValue("N(N)", iter, list)`.
- **`Py_SETREF(dst, src)` consumes `src`,** not just `dst`.
- **Module-lifetime statics.** A file-scope `static PyObject *Struct = NULL;`
  assigned in a module-exec function is process-lifetime by design.
- **`PySet_Discard` does not steal a reference.** It removes an element; a
  following `Py_DECREF` of the same variable is not a double-free. (This was a
  factual error in the toolkit's own `STEAL_REF_APIS` table.)
- **`PyModule_AddObject` steals only on success,** so `Py_DECREF(x)` inside
  `if (PyModule_AddObject(m, "x", x) < 0) { ... }` is required, not a
  double-free. Judge steal-then-drop by brace depth: a drop *nested deeper*
  than the steal is the steal's own failure branch.
- **A `goto` inside the variable's own NULL check is not a live error path.**
  `PyObject *c = PyCapsule_New(...); if (c == NULL) { goto error; }` — `c` is
  NULL at the label and cannot leak there.
- **A variable declared in a nested block is out of scope at a function-level
  cleanup label,** so it cannot leak there (`complex_richcompare`'s `sub_res`).
- **A cleanup label that `return`s the variable** is transferring ownership,
  not leaking (`math_fsum`'s `_fsum_error: ... return sum;`). Note CPython
  indents such labels, so a column-0 label regex misses them entirely.

## Reading CPython source with a scanner — two standing traps

- **Some markers only exist in comments.** The positional static `PyTypeObject`
  form names its slots in trailing comments (`(newfunc)list_new, /* tp_new */`)
  — 42 occurrences in `Objects/` versus 2 designated. Any check for slot
  registration must run on the *raw* source.
- **Some markers only exist in string literals.** `_ctypes`' re-init guard is
  the message `"StgInfo of '%s' is already initialized."`. A guard check run
  against comment-and-string-stripped source cannot see it.

## Refcounts — the escape / deref hazards (v0.9, `slot_transfer_across_call` and `stale_slot_use`)

- **Type-constrained operand makes a protocol call non-Python-reaching.**
  `PyNumber_Add` / `PyObject_RichCompare` / `PyObject_Hash` are all in
  `PYTHON_REACHING_APIS`, but if *every* operand is provably of a concrete
  builtin type for the lifetime of the field, the dispatch resolves to a C slot
  and no user code runs. `Objects/enumobject.c:196`
  `increment_longindex_lock_held` is the exemplar: `en->one` is
  `_PyLong_GetOne()` and `en_longindex` is only ever a `PyLong` (it arrives via
  `start = PyNumber_Index(start)`), so borrowing across `PyNumber_Add` is safe.
  This is the numeric-protocol analogue of the existing "statically-known type
  slot" entry.
  **Do not generalise it.** The sibling `Modules/itertoolsmodule.c`
  `count_nextlong` is textually identical — same transfer idiom, same comment
  wording — and *is* a reproduced heap-use-after-free, because `lz->long_step`
  comes from a constructor parameter that is only `PyLong_Check`-ed on the
  `fast_mode` path while `count_nextlong` is the *slow* path. The discriminator
  the scanner encodes: a parameter counts as type-pinned only when the function
  coerces it through an int-producing conversion **of itself**
  (`start = PyNumber_Index(start)`); receiving a default
  (`long_step = _PyLong_GetOne()`) proves nothing about what the caller passed.

- **A completed ownership transfer is not a stale borrow.** `func = v->func;
  v->func = NULL; PyObject_CallNoArgs(func);` (`Modules/_tkinter.c`
  `TimerHandler`) and `elem = it->root_element; /* steals a reference */
  it->root_element = NULL;` (`Modules/_elementtree.c` `elementiter_next`) clear
  the slot *before* anything can run Python, so the local is the legitimate
  sole owner. Only a clear a re-entrant call could reach — one at or after the
  first Python-reaching call — is dangerous.

- **A re-read of the slot after the call is the guarded twin.**
  `Modules/itertoolsmodule.c` `pairwise_next:364` calls
  `(*Py_TYPE(it)->tp_iternext)(it)`, then re-reads `it = po->it` and returns on
  NULL. A local that is re-loaded from its slot cannot be stale.

- **`#ifdef Py_GIL_DISABLED` asymmetry is a promotion signal, not a
  suppression.** The "never reason across a `#if`/`#else`" rule is about
  reasoning *across* arms. In `batched_next` the `Py_CLEAR(bo->it)` calls and
  the dangerous `iternext(it)` compile into the *same* (default, GIL) build;
  the `#ifndef Py_GIL_DISABLED` only tells you the bug is GIL-build-only — and
  the free-threaded arm is the fix. Report per-configuration, and treat "one
  arm guards, the other does not" as evidence *for* the finding.

- **A raw `PyMem_Malloc` buffer hanging off a live object is NOT protected by
  its owner** — a carve-out from the "borrowed ref under a known-live owner"
  entry above. That rule says "a *strong reference* is provably held for the
  duration"; a strong reference to the *container* says nothing about a
  refcount-less block hanging off it, which an ordinary method on the live
  receiver is free to `PyMem_Free`. Three reproduced instances:
  `Modules/_struct.c` `s_codes` (freed by `Struct.__init__` → `prepare_s`
  under an in-flight `pack`/`unpack`), `Modules/_zoneinfo.c`'s
  `StrongCacheNode` chain (freed by `ZoneInfo.clear_cache()` from inside a
  user `__eq__`), and `Modules/_elementtree.c`'s `extra`. None of the three is
  reachable by the scanner's rules — they walk the buffer with pointer
  arithmetic rather than caching it into one local — so this entry exists to
  stop a reader dismissing them.

## Free-threading — field synchronisation asymmetry (v0.9, T1 retarget)

Every entry was a measured false positive of the per-site T1 rule on CPython
main @ 3.16.0a0 and is now gated in the scanner.

- **The lock is in the Argument Clinic wrapper.** `@critical_section` in a
  `/*[clinic input]*/` block emits `Py_BEGIN_CRITICAL_SECTION` into
  `<dir>/clinic/<file>.c.h`, so the `_impl` body looks completely
  unsynchronised while every access in it runs under a per-object lock. This
  was the single largest FP class of the retargeted rule (`Modules/_io/`,
  `Modules/_collectionsmodule.c`).
- **The caller holds the section, transitively.** `count_nextlong`
  (`Modules/itertoolsmodule.c`) is not named `*_lock_held` and takes no lock,
  but its only free-threaded caller wraps it in `Py_BEGIN_CRITICAL_SECTION(lz)`.
  Chains are longer than one hop: `Modules/_io/textio.c` reaches
  `_textiowrapper_writeflush` from `textiowrapper_read_chunk`, itself reached
  only from clinic-guarded impls.
- **The lock is a macro.** `LOCK_WEAKREFS(obj)` / `UNLOCK_WEAKREFS_FOR_WR(self)`
  (`Include/internal/pycore_weakref.h`) expand to a critical section, and the
  `#define` is not in the `.c` file being scanned.
- **A by-value aggregate is never shared.** `WFILE wf;` in `Python/marshal.c`,
  `struct worklist` and `gc_mark_args_t` in `Python/gc_free_threading.c`: the
  caller owns it on its stack and passes `&wf`, so `p->buf` cannot race.
- **Teardown, fork-child and assert-only paths.** `_Py_qsbr_after_fork` runs in
  the single-threaded child; `_PyObject_ManagedDictValidityCheck` and the
  `*CheckConsistency` family compile out or run from a debugger;
  `*_dealloc`/`*_traverse`/`*_clear`/`*Finalize*`/`*destroy*` run when nothing
  else can observe the object.
- **`PyType_Ready` construction.** `inherit_slots` / `type_ready_*` populate
  slots before the type object is reachable, so a plain read of `tp_free`
  elsewhere is not racing them.
- **A guarded write into a freshly allocated object is not evidence.**
  `deque_copy_impl` stores `new_deque->maxlen` while holding the *source*
  deque's lock, and `deque_iter` fills a brand-new iterator. Neither makes a
  plain read of that field elsewhere a race — such writes are excluded from the
  guarded-twin set, not just from the finding set.
- **A field with many unguarded accessors is an un-hardened module, not a
  missed guard.** `Modules/_pickle.c` has three critical sections in 8,298
  lines and no per-object locking on `Pickler`/`Unpickler` state at all;
  reporting each site as a separate FIX misstates the problem. The scanner caps
  T1 at 4 unsynchronised sites across 2 functions per field for exactly this
  reason — report the wholesale case as one POLICY finding instead.

## Error paths — `int_status_never_tested` (issue #28 rule 4)

- **Symmetric cleanup that must run regardless of the status.**
  `Modules/_pickle.c` `save_frozenset:3796` is the shape:

  ```c
  if (self->fast && !fast_save_enter(self, obj)) { return -1; }
  int status = save_frozenset_impl(state, self, obj);
  if (self->fast && !fast_save_leave(self, obj)) { return -1; }
  return status;
  ```

  The scanner's "the region between the assignment and the read is fallible"
  gate fires on that intervening `return -1;`, but the intervening call is the
  *leave* half of an enter/leave pair and is **required** to run on the failure
  path too. Nothing is committed and nothing is skipped, and `status` reaches
  the caller unchanged. Tell this apart from the real shape by asking what the
  intervening code *does*: cleanup that must happen either way is correct,
  whereas `type_set_bases_unlocked:1966` runs `update_all_slots()` with a live
  exception and skips its own rollback. **1 of the 2 candidates tree-wide is
  this class** — check it first.
- **Accumulate-then-return.** `res = f(); Py_DECREF(x); return res;` with only
  cleanup in between is correct and is already suppressed by the same gate
  (160 raw assignments across `Objects/` + `Modules/` + `Python/` reduce to 2).

## NULL checks — the widened fallible-source set (issue #28)

`scan_null_checks` used to resolve its assignment sources from a closed enum of
45 API names, which reached **49 of 760** assignment-from-call sites (6.4%).
The producer of the value is now also discovered from the file: any
pointer-returning function whose body can `return NULL`, transitively through
thin forwarders. That is what made `Objects/dictobject.c:4494` visible at all.
Two FP classes come with it:

- **A public API that is both a checked function and an unchecked macro in the
  same translation unit.** `Objects/unicodeobject.c:15388` defines
  `void* PyUnicode_DATA(PyObject *op)` with a real `return NULL` type-check
  path — so discovery is correct to call it fallible — but every *internal*
  call site expands the same-named **macro** instead, which cannot fail. The
  finding at `:12922` is real about the name and wrong about the call. Check
  whether the header defines a macro of the same name before triaging.
- **The argument is provably the right type at the call site.** Most of these
  helpers only return NULL on a type check the caller has already done. That is
  a legitimate dismissal, but say *which* prior check establishes it.

Before dismissing a whole file, read the three denominators now in the
envelope: `assignment_sites`, `fallible_sources_resolved`, and
`local_nullable_helpers`. Tree-wide they are 78,109 / 5,386 / 5,341.
`summary.decref_of_nulled_outparam_call_sites` exists for the same reason: that
rule's denominator on CPython is **effectively zero**, so its zero is
structural and must never be reported as a clean result.
