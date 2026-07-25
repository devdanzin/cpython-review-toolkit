# Findings — informed-explore, `Objects/` 14-file sample

**Target:** CPython main @ `4f3be1b5777`, 3.16.0a0 · **Scope:** 14 files / 13,250 lines of `Objects/`
**Verification builds:** in-tree `./python` (Clang 21, `-fsanitize=address`, debug) ·
`~/venvs/3.14_ft_venv` (free-threaded) · `3.14_tsan_debug_ft` (TSan) · released 3.12/3.14 for
regression differentials.

Every FIX below was **reproduced live**. Nothing here is a static-only hypothesis.
Two were reproduced by the orchestrator independently of the agent that found them (marked ★).

---

## FIX

### F1 ★ `Objects/iterobject.c:80` — `iter_iternext` borrowed-ref double-DECREF → heap UAF
The crown-jewel shape, in a PUBLIC-tier file reachable from `iter(seq)` on any sequence.

`seq = it->it_seq` is loaded **borrowed** at `:61`, then `PySequence_GetItem(seq, ...)` at `:70`
runs arbitrary Python. If that Python re-enters `next()` on the same iterator and the inner call
takes the `IndexError`/`StopIteration` branch, the inner frame executes `it->it_seq = NULL;
Py_DECREF(seq);` — dropping the only reference. The outer frame's `seq` local is now stale and runs
the *same* `Py_DECREF(seq)` again.

**Guarded twin — 165 lines below in the same file:** `calliter_iternext:243,249` uses
`Py_CLEAR(it->it_callable)`, which NULLs-and-decrefs from the struct atomically instead of dropping
a stale local. **The fix is to use `Py_CLEAR` here too — one line.**

Evidence: `repro/CPY-iterobject-uaf_repro.py` → ASan `heap-use-after-free`, free attributed to
`#4 iter_iternext Objects/iterobject.c:80` (`repro/CPY-iterobject-uaf_evidence.txt`).
Trigger is ~15 lines of pure Python.

### F2 ★ `Objects/bytearrayobject.c:2940` / `:280` — `__new__` bypass → SIGSEGV (**regression**)
`bytearray`'s `tp_new` is `PyType_GenericNew`, but the "buffer is always non-NULL" invariant is
established in `__init__` (`:923`). Bypassing `__init__` leaves it NULL, and
`_PyBytes_Resize(&obj->ob_bytes_object, …)` at `:280` dereferences it unguarded.

```python
bytearray.__new__(bytearray).append(1)              # SIGSEGV
class B(bytearray):
    def __init__(self, *a, **k): pass
B().append(1)                                        # SIGSEGV
```

Six entry points crash (`append`, `extend`, `+=`, `insert`, slice-assign, `resize`).
**Regression** introduced by gh-139871 (`732224e1139`): released 3.12.13 and 3.14.4 both return
`bytearray(b'')` cleanly. Orchestrator-verified: ASan `SEGV on unknown address 0x000000000008` on
main, exit 0 with clean output on 3.14.
**Guarded twins:** `_PyTuple_Resize` and `PyBytes_Concat` both NULL-check `*pv`; `_PyBytes_Resize`
is the only member of that family that does not. Also gh-144330, which fixed exactly this shape for
classmethod/staticmethod by initializing in `tp_new`.

### F3 `Objects/structseq.c:77` — `PyStructSequence_New` unguarded `n_fields` multiply → heap overflow
`size` comes from the type's own `n_fields` dict entry. Every type built by the public
`PyStructSequence_NewType` is a **mutable heap type** (`os.stat_result`, `time.struct_time`,
`pwd.struct_passwd`, `resource.struct_rusage`, …), so Python can write it. `PyObject_GC_NewVar` →
`_PyObject_VAR_SIZE` multiplies unchecked.

```python
import os
os.terminal_size.n_fields = 2**62
os.terminal_size((7, 9))        # allocation succeeds at 32 bytes; then OOB write
```
gdb at `structseq.c:84` confirms `size = 2**62` with a 32-byte allocation; ASan reports a
heap-buffer-overflow WRITE at `:235`.
★ **Orchestrator-verified:** the `2**62` case reproduces — ASan `heap-buffer-overflow WRITE of
size 8` at `structseq.c:235`.

> **Correction, twice — and the second overturns the first.** I initially could not reproduce the
> reported `:242` sibling (OOB `tp_members` walk, "no overflow needed at `n_fields = 64`") and
> marked it unconfirmed: `os.terminal_size.n_fields = 64; os.terminal_size((7,9))` exits 0.
> **That was my error — an incomplete invocation.** The `:242` path needs the optional *dict*
> argument to reach the `len < max_len` loop:
> ```python
> import os
> os.terminal_size.n_fields = 64
> os.terminal_size((7, 9), {'zzz': 1})     # ASan: SEGV on address 0x0
> ```
> Re-verified by me: **ASan `SEGV on unknown address 0x0`**, a NULL read in `strlen` ←
> `PyUnicode_FromString` ← `PyDict_GetItemStringRef`, caused by the out-of-bounds
> `tp_members[i].name` read at `:242`. So the sibling **is real, and needs no integer overflow at
> all** — a plain `n_fields = 64` suffices.
>
> Note this one is **not scanner-detectable**: it is an out-of-bounds array *read*, not an
> allocation, so it belongs in the agent prompt rather than in `scan_memory_patterns.py`.
> The `:308` and `:41` siblings remain agent-reported and not independently re-run.
**Guarded twin:** `tupleobject.c:52-57` `tuple_alloc` — the `n > MAX/elem` guard — in structseq's
own base type.

### F4 `Objects/genericaliasobject.c:542` — `_Py_subs_parameters` heap UAF via aliasing
`args = tuple_args = PySequence_Tuple(args)` at `:460` aliases the two names. The
"`__typing_subst__` returned a non-tuple" error branch does `Py_XDECREF(tuple_args)` at `:541`, then
reads `PyTuple_GET_ITEM(args, iarg)` at `:542` and feeds the recycled slot to `PyErr_Format("%T")`.
**Guarded twin:** all 14 other `Py_XDECREF(tuple_args)` sites `return` immediately.
Introduced by `1da989be74e` (gh-138479); the `8060aa5d7dd` refleak sweep touched `:460` and missed
this. Repro needs >20 elements — below that the tuple freelist masks it, which is why it survived.

### F5 `Objects/genericaliasobject.c:302` — `subs_tvars` `Py_DECREF` of a provably-NULL out-param
`8060aa5d7dd` (gh-145376) **added** `Py_DECREF(subargs)` to the `tuple_extend` failure branch — but
`tuple_extend` fails only when `_PyTuple_Resize` fails, and `_PyTuple_Resize` NULLs its out-param on
every failure path, so `subargs` is always NULL there. Then `634568d030f` (gh-148222, titled
*"Fix NULL dereference bugs in genericaliasobject.c"*) removed the **identical** line 60 lines above
in `_Py_make_parameters` and left this one. Still unfixed at HEAD.
Reproduced on ASan with `_testcapi.set_nomemory`; gdb shows `Py_DECREF (op=0x0)` ←
`subs_tvars:302` ← `_Py_subs_parameters:529` ← `ga_getitem:589`.
Trigger: `Ts = TypeVarTuple('Ts'); dict[str, tuple[*Ts]][int, str]`. **Fix: delete line 302.**
Tree-wide sweep says it is the only unfixed instance.

### F6 `Objects/genericaliasobject.c:583` — `ga_getitem` lazy init: gh-153298 is an incomplete fix
`68abf17fa92` (gh-153298), merged **7 days before HEAD**, wrapped `ga_parameters`' lazy init in
`Py_BEGIN_CRITICAL_SECTION` but left `ga_getitem`'s identical inline lazy init of the *same field*
alone — and a critical section held by only one of two accessors serializes nothing.
TSan reproduces three races: `ga_getitem`×`ga_getitem`, `ga_getitem:577`×`ga_parameters:842`, and
`_Py_subs_parameters:415` reading a tuple still being filled.
**Guarded twin — the right way to do it:** the 2025 fix `union_init_parameters`
(`unionobject.c:327`, gh-132713) introduced **one helper used by both callers**.

### F7 `Objects/unionobject.c:172` — unconditional `PyErr_Clear()` swallows user exceptions
`unionbuilder_add_single_unchecked` calls `PyObject_Hash(arg)` and, on `-1`, calls `PyErr_Clear()`
unconditionally — discarding *any* exception a user `__hash__` raised, not just `TypeError`.
Reproduced live: `int | C` and `typing.Union[int, D]` silently discard `KeyboardInterrupt` and
`MemoryError` and return a union.
**Guarded twin:** 7 of the 8 `PyErr_Clear()` sites in the sample first narrow with
`PyErr_ExceptionMatches` (`iterobject.c:75,247,403,427`, `genericaliasobject.c:631`,
`funcobject.c:1361`, `descrobject.c:1907`). This is the lone outlier.
Introduced `dc6d66f44c0` (gh-105499, 2025-03).

### F8 `Objects/odictobject.c:1945` — `odictiter_new` dealloc of a half-built object
`_PyTuple_FromPairSteal` failure → `Py_DECREF(di)` at `:1952` with `kind` / `di_odict` /
`di_current` still garbage and `di` untracked. `odictiter_dealloc:1718` uses the **unchecked macro**
`_PyObject_GC_UNTRACK`, so it faults before even reaching the wild `Py_XDECREF`s.
Targeted OOM sweep: **K=1 → SIGABRT**, `Assertion "_PyObject_GC_IS_TRACKED" failed … object type
name: odict_iterator`. Reachable via `iter(OrderedDict(...).items())`.
**Guarded twin:** `dictobject.c:5617 dictiter_new` — same fallible call, placed *last*.

### F9 `Objects/listobject.c:250` — `PyList_New` free-threaded branch (scope escape)
In the `Py_GIL_DISABLED` branch, a `list_allocate_array` failure `Py_DECREF(op)`s at `:262` with
both `op->ob_item` and `Py_SIZE(op)` uninitialized; `list_dealloc:560` gates on `ob_item != NULL`,
giving a garbage loop and `PyMem_Free(garbage)`.
Python-reachable via `marshal.loads` (`Python/marshal.c:1448`, `n` bounded only by `SIZE32_MAX`).
**Guarded twin:** `listobject.c:281 list_new_prealloc` asserts `op->ob_item == NULL` first.
Outside the 14-file sample — found by hand while hunting the shape.

### F10 `Objects/genericaliasobject.c:952` — `ga_iternext` `Py_DECREF(NULL)` → ASan SIGSEGV
Same iterator family as F1. Reproduced; missed by the scanner.

### F11 Unguarded-hash recursion cluster — native C-stack SIGSEGV, not `RecursionError`
**The organizing fact:** of the four dispatchers, only `PyObject_Hash` (`object.c:1158`) lacks a
recursion guard — `PyObject_Repr` (`:759`), `PyObject_Str` (`:800`) and `PyObject_RichCompare`
(`:1099`) all wrap `_Py_EnterRecursiveCallTstate`. That asymmetry explains the whole class.

- **CPY-0001 / gh-154318** `tuple_hash` (`tupleobject.c:385`) — confirmed still unguarded,
  reproduced (ASan stack overflow, 1M-deep tuple).
- **CPY-0002 / gh-154275** `_Py_make_parameters` (`genericaliasobject.c:231`) — confirmed, and
  **its recorded reachability needs widening**: `(int | list[deep_tuple]).__parameters__` SIGSEGVs
  via `unionobject.c:332`, and `u[str]` via `:349` → a *second* unguarded self-recursion at
  `genericaliasobject.c:482`. Reachable from `typing.Union`, not only `list[...]`.
- **New siblings, all SIGSEGV-reproduced, none scanner-surfaced:**
  `genericaliasobject.c:615,619` `ga_hash` · `weakrefobject.c:199` `weakref_hash_lock_held`
  (which also recurses *inside* a `Py_BEGIN_CRITICAL_SECTION`) · `unionobject.c:170`.
- **Scope escape — `dictobject.c:8427` `frozendict_pair_hash`.** `244300162d2` (2026-05-20) *added*
  the comment "update also frozendict_pair_hash() which copied this code" above `tuple_hash`,
  formalizing the copy-paste **without adding the guard to either**. Must be fixed in the same patch
  as CPY-0001. It was never surfaced because the chassis defect (TK-8) makes `dictobject.c` lose
  ~39% of its lines.

### F12 `Objects/templateobject.c:225` — gh-151815 confirmed still present at HEAD
`Py_DECREF(iter)` at `:232`/`:238` still precedes the member writes at `:243-245`, byte-identical at
`4f3be1b5777`. **Correcting a v0.7 conclusion:** the "60/60 clean `MemoryError`" sweep was *not*
evidence of a fix — this shape only crashes on a *dirty* recycled block, and `templateiter_clear`
NULLs both members before `tp_free`, so same-type blocks always come back clean. Reproduced via the
NULL-safety path (`op=0xcdcdcdcd` in `templateiter_clear`). Latent, still FIX.

### F13 ★ `Modules/_io/winconsoleio.c:957` — unchecked allocation dereferenced on the next line
**Found by the *repaired* `scan_null_checks.py`, not by the review run** — the first bug the cleanup
paid for, and evidence the rebuilt rule works on code nobody had looked at.

```c
bytes = PyBytes_FromStringAndSize(NULL, bytes_size);   /* :957 — no NULL check */
rn = _copyfrombuf(self, PyBytes_AS_STRING(bytes), bytes_size);   /* :958 — dereferences it */
```
`PyBytes_AS_STRING` casts and dereferences unconditionally, so an allocation failure is an immediate
NULL dereference in `_io._WindowsConsoleIO.readall`.

**Guarded twin — same file, 65 lines below at `:1022`:**
```c
bytes = PyBytes_FromStringAndSize(NULL, size);
if (bytes == NULL)
    return NULL;
bytes_size = readinto(state, self, PyBytes_AS_STRING(bytes), …);
```
Orchestrator-verified by reading both sites. Windows-only code path, so not reproducible on this
Linux host — reported as a static finding with its twin, not as a reproduced crash.

### F14 ★ `Objects/typeobject.c:12747` — `super.__new__(super).__get__(1)` → SIGSEGV
**Found by the repaired `scan_init_bypass.py`.** `super`'s `tp_init` establishes `su->obj_type`,
which `supercheck` dereferences unguarded when reached via `super_descr_get:12781`.

```python
super.__new__(super).__get__(1)      # SIGSEGV
```
Orchestrator-verified: ASan `SEGV on unknown address 0x18` on main **and** a plain segfault on
released `/usr/bin/python3` (3.14) — so unlike F2 this is **longstanding, not a regression**.
The subclass route does *not* crash (`Py_IS_TYPE(su, &PySuper_Type)` diverts it) — verified, exit 0.

Note this **corrects the v1 review**, which classified `super` as ACCEPTABLE on the grounds that
`super_getattro`/`super_repr` guard `su->obj_type`. That probe missed `__get__`, and the field
actually guarded elsewhere is `obj_type` — the same field, but not on this path.

### F15 `Objects/dictobject.c:5662` — `dictiter_new` GC-track defect (sibling of F8)
**Found by the new `gc-untrack-macro-form` rule.** `dictiter_new` does `Py_DECREF(di)` on the
`_PyTuple_FromPairSteal` failure path **before** `_PyObject_GC_TRACK`, while its `tp_dealloc` untracks
with the unchecked macro — structurally identical to F8 (`odictiter_new`). Only 2 sites fire across
`Objects/` + `Modules/` + `Python/` out of ~80 macro uses, and both are true positives.
This upgrades the earlier CONSIDER note on `dictobject.c:5646` to a confirmed sibling.

---

## CONSIDER

- `Objects/descrobject.c:624` — **TSAN-0043 confirmed live** (TSan `data race descrobject.c:624:16
  / :625:27`); file has zero critical sections. Its guarded twin is now
  `genericaliasobject.c:844 ga_parameters_lock_held` (gh-153298, 7 days before HEAD). Also leaks
  +2 refs/descriptor via a raw store with no `Py_XSETREF`.
- `Objects/funcobject.c:534` `func_get_annotation_dict` — lazy init; guarded twin is the clinic
  `@critical_section` getter (`clinic/funcobject.c.h:94`); `PyFunction_GetAnnotations:581` reaches
  it unguarded. Only `_testcapi` calls it in-tree.
- `Objects/odictobject.c:1098` — `Py_NewRef(failobj)` where `failobj` is nullable; the guarded twin
  is five lines below at `:1103`, and `popitem` passes `NULL` at `:1172`. Latent, one-line fix.
- `Objects/lazyimportobject.c:87-95` — `lazy_import_name` reads `lz_from` post-`tp_clear`.
  Guarded twin `cellobject.c:121`.
- `Objects/structseq.c:700` — `PyMem_Free(members)` after `PyType_Ready` installed `tp_members` and
  created descriptors holding `d_member` pointers into it → dangling. Twin
  `_PyStructSequence_NewType:781` is safe because `PyType_FromSpec` memcpys.
- `Objects/dictobject.c:5646` — un-found sibling with the identical GC-track defect to F8.
- Lazy-init raw stores leaking on reentrancy: `ga_parameters_lock_held:848`, `ga_getitem:584`,
  `union_init_parameters:332` (measured `refcnt(t)` 4→5). Scope escapes: `bytearrayobject.c:924`,
  `enumobject.c:181`, `typeobject.c:9479`.
- `Python/pystate.c:836 interpreter_clear` — unguarded `_PyErr_Clear(tstate)`, no save/restore.
  Found only because the `_PyErr_Clear` spelling was checked by hand (TK-10).

## Classes bounded (clean negatives)

Stated because a cold run never produces them:

- **`weakrefobject.c` refcounts are clean** — uses strong `_PyWeakref_GET_REF` throughout; the
  callback loop reads a private local tuple. The briefing's flagship hypothesis was **wrong**.
- **odict reentrancy→UAF is swept** — `odict_repr` no longer walks the linked list (`PyDict_Copy` +
  `%R`), so the 2015-shape lead is a clean negative.
- **`structseq.c` is clean for uninit-dealloc** — `PyStructSequence_New:65` NULLs all `n_fields`
  slots before anything fallible; it *is* the guarded twin. (Its bug is F3, a different shape.)
- **`cellobject.c` and `funcobject.c` versioning are properly hardened** against FT races;
  `PyCell_GetRef()` is the model implementation for the init-bypass family.
- **`tuple_richcompare` dismissed as an FP** — 1M-deep `a == b` raises a clean `RecursionError`
  (dispatcher-guarded, unlike hash).
- **The `*_lock_held` convention holds at all 16 sample sites.**
- **1,664 init-bypass probe runs** (32 types × 2 constructions × 26 ops) on debug+ASan: zero crashes
  besides `bytearray`.
- Missing `PyErr_NoMemory()` is clean in the sample (2/2 raw allocators correct);
  `lazyimportobject.c` error paths clean (all 157 lines read).

## Upstream reporting status

**Nothing has been reported upstream.** F1, F2, F3, F5 and the F11 siblings look novel and
individually reportable; F2 is a regression with a clean bisect target (gh-139871). CPY-0001/0002
are already tracked as gh-154318 / gh-154275. Per `WORKING_WITH_MAINTAINERS.md` these need
deduplication against the tracker before filing — awaiting your go-ahead.
