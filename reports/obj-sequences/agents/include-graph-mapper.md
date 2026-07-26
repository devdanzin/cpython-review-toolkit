# include-graph-mapper — slice `obj-sequences` (Phase 1)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`

> **Build-matrix validity, measured first.**
> `git diff --stat a1d580430c8 4f3be1b5777 --` over `Objects/{listobject,bytesobject,bytearrayobject,bytes_methods}.c`,
> `Objects/clinic/{listobject,bytesobject,bytearrayobject}.c.h`, `Include/cpython/{listobject,bytearrayobject}.h`
> and `Include/internal/pycore_bytes_methods.h` is **empty**. Every file this slice reviews is **byte-identical**
> between the build-matrix commit and the review target. All matrix builds are valid evidence for this slice.

---

# §b — THE RE-ENTRANCY SURFACE (read this first)

Every row is a site in the four files where C code calls out to something that can execute arbitrary
user Python. "Carried" = state read **before** the call and still used **after** it.

**Verdict column:** `PINNED` = protected by `ob_exports` (bytearray's re-entrancy pin) · `RE-READ` = the state is
re-read after the call · `IMMUTABLE` = the carried object cannot change (bytes/tuple) · `ORDERED` = the call
happens before the state is read · `EXPOSED` = carried across with no protection I could find.

## B.1 — `Objects/listobject.c`

| # | site | function | user-Python call | carried across | verdict |
|---|---|---|---|---|---|
| L1 | `listobject.c:665` | `list_contains` (sq_contains) | `PyObject_RichCompareBool(item, el, Py_EQ)` | loop index `i` only | **RE-READ** — `list_get_item_ref` (`:354`/`:381`) re-reads `ob_item`+size and returns a **strong** ref every iteration; OOB ⇒ NULL ⇒ return 0 |
| L2 | `listobject.c:3345` | `list_index_impl` | `PyObject_RichCompareBool(obj, value, Py_EQ)` | `i`, `stop` (precomputed from `Py_SIZE` at `:3335`) | **RE-READ** — same `list_get_item_ref`; a stale `stop` only over-runs into the NULL/`break` at `:3341` |
| L3 | `listobject.c:3381` | `list_count_impl` | `PyObject_RichCompareBool(obj, value, Py_EQ)` | `i` | **RE-READ** — same |
| L4 | `listobject.c:3412` | `list_remove_impl` | `PyObject_RichCompareBool(obj, value, Py_EQ)` | **`i`, and a raw `self->ob_item[i]` read at `:3410`** | **RE-READ (bounds), semantically stale.** `Py_INCREF` at `:3411` pins the item; `Py_SIZE(self)` is the loop bound re-read at `:3409`; and `list_ass_slice_lock_held` **clamps `ilow`/`ihigh` to `Py_SIZE(a)` at `:979-987`**, so a shrink during `__eq__` cannot go OOB — it deletes the wrong element or nothing |
| L5 | `listobject.c:3467` | `list_richcompare_impl` (tp_richcompare) | `PyObject_RichCompareBool(vitem, witem, Py_EQ)` | `i`; raw `vl->ob_item[i]`, `wl->ob_item[i]` at `:3459-3460` | **RE-READ** — both INCREF'd at `:3465-3466`; loop bound is `Py_SIZE(vl)`/`Py_SIZE(wl)` re-read at `:3458` |
| L6 | `listobject.c:3494` | `list_richcompare_impl` | `PyObject_RichCompare(vl->ob_item[i], wl->ob_item[i], op)` | `i` (survives the L5 loop) | **RE-READ**, but note the smell: the call **re-reads `ob_item[i]`** instead of using the `vitem`/`witem` it just INCREF'd at `:3492-3493`. Benign today (nothing runs between `:3490` and `:3494`); one inserted statement makes it a UAF. Flag as fragile, not broken |
| L7 | `listobject.c:2993` | `list_sort_impl` | `PyObject_CallOneArg(keyfunc, saved_ob_item[i])` | `saved_ob_size`, `saved_ob_item`, `saved_allocated` (`:2968-2970`) | **DETACHED** — the strongest guard in the slice. `:2971-2973` sets `ob_size=0`, `ob_item=NULL`, `allocated=-1` **before** any user code, with the comment "allowing mutations during sorting is a core-dump factory, since ob_item may change". The local array owns the refs; the live list cannot alias it |
| L8 | `listobject.c:2770/2784/2791/2801` | `safe_object_compare` / `unsafe_object_compare` | `PyObject_RichCompareBool`, `ms->key_richcompare(...)`, `PyObject_IsTrue` | `ms->key_richcompare` (a **cached `tp_richcompare` slot pointer** set at `:3079`) | **RE-VALIDATED** — `:2783` re-checks `Py_TYPE(v)->tp_richcompare != ms->key_richcompare` on every compare and falls back to the generic path |
| L9 | `listobject.c:2903/2916` | `unsafe_tuple_compare` | `PyObject_RichCompareBool(vt->ob_item[i], ...)` | `vlen`/`wlen` (`:2899-2900`), raw `vt->ob_item[i]` | **IMMUTABLE** — `assert(Py_IS_TYPE(v, &PyTuple_Type))`; exact tuples, contents and size fixed; the tuples are kept alive by the detached `keys[]`/`saved_ob_item[]` |
| L10 | `listobject.c:2827/2858/2873` | `unsafe_latin_compare` / `unsafe_long_compare` / `unsafe_float_compare` | none (direct `memcmp` / word compare) | type homogeneity proved once at `:3026-3065` | **IMMUTABLE** — the `keys[]` array is a private local; nothing can substitute an element mid-sort. The `assert(Py_IS_TYPE(...))` guards are debug-only but the invariant is structurally held |
| L11 | `listobject.c:973` | `list_ass_slice_lock_held` | `PySequence_Fast(v, ...)` (runs `__iter__`/`__next__`) | nothing yet | **ORDERED** — `item = a->ob_item` is read **at `:997`, after** the call; `ilow`/`ihigh` clamped at `:979-987` after it |
| L12 | `listobject.c:1027-1030` | `list_ass_slice_lock_held` | `Py_XDECREF(recycle[k])` (`__del__`) | `item`, `ilow`, `n` | **ORDERED** — the `recycle` array exists precisely so DECREFs happen *after* the list is canonical (comment `:952-957`) |
| L13 | `listobject.c:1318` | `list_extend_iter_lock_held` | `iternext(it)` (arbitrary `__next__`) | `iternext` (slot ptr cached at `:1286`); **`Py_SIZE(self)`, `self->allocated`, `self->ob_item` are all re-read at `:1329-1332`** | **RE-READ.** Only the cached `tp_iternext` pointer is stale-able (heap-type `__next__` reassignment) |
| L14 | `listobject.c:1270-1274` | `list_extend_fast` | none inside; `n` from `:1244` | `n`, `m`, `PySequence_Fast_ITEMS` | **ORDERED** — comment `:1264-1267` "Just make sure to resize self before calling `PySequence_Fast_ITEMS`" is the file's own statement of the rule |
| L15 | `listobject.c:3694` | `list_subscript` (mp_subscript) | `PyNumber_AsSsize_t(item, PyExc_IndexError)` (`__index__`) | nothing | **RE-READ** — `PyList_GET_SIZE` at `:3698`, bounds check in `list_item` at `:678` |
| L16 | `listobject.c:3736` | `list_ass_subscript_lock_held` | `PyNumber_AsSsize_t(item, ...)` (`__index__`) | nothing | **RE-READ** — `PyList_GET_SIZE` at `:3740`, `list_ass_item_lock_held` bounds-checks |
| L17 | `listobject.c:3746` | `list_ass_subscript_lock_held` | `PySlice_Unpack(item, ...)` (`__index__` on slice fields) | nothing | **RE-READ** — `adjust_slice_indexes` re-reads `Py_SIZE(lst)` at `:3717`. **But see the asymmetry note below** |
| L18 | `listobject.c:3828` | `list_ass_subscript_lock_held` | `PySequence_Fast(value, ...)` | `start`/`stop`/`step` from L17 | **RE-READ** — `adjust_slice_indexes` runs at `:3835`, **after** the `PySequence_Fast` |
| L19 | `listobject.c:3872-3878` | `list_ass_subscript_lock_held` | `Py_DECREF(garbage[i])` (`__del__`) | `selfitems` (`:3868`), `seqitems` (`:3869`) | **ORDERED** — all writes finish at `:3875` before the first DECREF at `:3878` |
| L20 | `listobject.c:3809` | `list_ass_subscript_lock_held` (delete branch) | `Py_DECREF(garbage[i])` | `slicelength` | **ORDERED** — memmove `:3801`, `Py_SET_SIZE` `:3805`, `list_resize` `:3806`, *then* DECREF |
| L21 | `listobject.c:583/625/631` | `list_repr` (tp_repr) | `PyObject_Repr` on each item | — | **GUARDED** — `Py_ReprEnter`/`Py_ReprLeave` present. The only recursion guard in the slice |
| L22 | `listobject.c:887` | `list_clear_impl` | `Py_XDECREF(items[i])` (`__del__`) | `items` (`:875`) | **ORDERED** — list emptied at `:882-885` first; comment `:880-881` and the closing note at `:898-899` |

**The one asymmetry in listobject.c worth handing to Group A/B.**
`_PyList_BinarySlice:725-733` carries an explicit comment — *"Unpack the index values **before acquiring the lock**, since `_PyEval_SliceIndex` may call `__index__` which could execute arbitrary Python code"* — and `list_slice_subscript:3676` follows the same rule (`PySlice_Unpack` at `:3676`, lock taken later inside `list_slice_wrap:3655`).
**`list_ass_subscript_lock_held:3746` breaks it**: it calls `PySlice_Unpack` *inside* the critical section (the function asserts `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` at `:3732`). Same for `PyNumber_AsSsize_t` at `:3736` and `PySequence_Fast` at `:3828`. I found **no memory-safety consequence** (every index is re-derived from a re-read `Py_SIZE`), so this is a **discipline divergence, not a bug**, and I am reporting it as such — but it is the read-path/write-path split, and Group A/B should know which of the two rules the file actually follows.

## B.2 — `Objects/bytes_methods.c` (shared by **both** bytes and bytearray — a defect here is doubled)

`bytes_methods.c` has **no critical sections and no locks**. Every entry point receives `(const char *str, Py_ssize_t len)`
— a raw data pointer and a size snapshot taken by the caller — and then runs user Python on its *argument*.

| # | site | function | user-Python call | carried across | verdict |
|---|---|---|---|---|---|
| M1 | `bytes_methods.c:418` | `parse_args_finds_byte` | `PyNumber_AsSsize_t(*subobj, NULL)` (`__index__`) | **caller's `str` + `len`** | see M-note |
| M2 | `bytes_methods.c:469` | `find_internal` | `PyObject_GetBuffer(subobj, &subbuf, PyBUF_SIMPLE)` (`__buffer__`) | **`str`, `len`** — used at `:485-503` | see M-note |
| M3 | `bytes_methods.c:507` | `find_internal` | `PyBuffer_Release(&subbuf)` (`__release_buffer__`) | `res` only; `str`/`len` dead | ORDERED |
| M4 | `bytes_methods.c:578` | `_Py_bytes_count` | `PyObject_GetBuffer(sub_obj, ...)` | **`str`, `len`** — used at `:589-592` | see M-note |
| M5 | `bytes_methods.c:604/609` | `_Py_bytes_contains` | `PyNumber_AsSsize_t(arg,…)` / `PyObject_GetBuffer(arg,…)` | **`str`, `len`** — used at `:611`, `:621` | see M-note |
| M6 | `bytes_methods.c:642` | `tailmatch` | `PyObject_GetBuffer(substr, ...)` | **`str`, `len`** — used at `:648-664` | see M-note |
| M7 | `bytes_methods.c:685/695` | `_Py_bytes_tailmatch` | `tailmatch(...)` per tuple element | `str`, `len`, `i`, borrowed `item` from `PyTuple_GET_ITEM` | see M-note; `PyTuple_GET_SIZE` re-read each iteration |
| M8 | `bytes_methods.c:608` | `_Py_bytes_contains` | `PyErr_Clear()` after `PyNumber_AsSsize_t` — **unnarrowed** | — | this is one of the 3 `scan_error_paths` hits; hand to the pyerr-clear agent |

**M-note (the doubled-defect rule).** Every M-row's safety is decided **entirely by the caller**, because
`bytes_methods.c` cannot see whether `str` points into an immutable `bytes` or a resizable `bytearray`:

- **bytes callers** (`bytesobject.c:1625, 2038, 2056, 2074, 2092, 2243, 2534, 2559`) pass
  `PyBytes_AS_STRING(self)` / `PyBytes_GET_SIZE(self)`. **IMMUTABLE** — `bytes` is never resized in place after
  publication, so `str` cannot dangle. Real negative.
- **bytearray callers** pass `PyByteArray_AS_STRING(self)` — a pointer into a **resizable** buffer. All seven of
  them (`find`/`count`/`index`/`rfind`/`rindex`/`startswith`/`endswith`, `bytearrayobject.c:1270, 1286, 1334,
  1352, 1370, 1412, 1437`) route through **`_bytearray_with_buffer` (`:97-110`)**, which brackets the call with
  `self->ob_exports++` / `--`. **PINNED.** `bytearray_contains:1381-1385` does the same by hand.

So `bytes_methods.c` is clean *given* its current callers, and the pin is the invariant a reviewer must not break.
**Any new bytearray caller of a `_Py_bytes_*` function that does not bump `ob_exports` is a bug by construction.**

## B.3 — `Objects/bytesobject.c` (immutable — but the *writer* pointer is the carried state)

`bytes` cannot be resized after publication, so a cached `PyBytes_AS_STRING(self)` never dangles. The carried
state that *does* matter here is the raw write pointer into a `PyBytesWriter`.

| # | site | function | user-Python call | carried across | verdict |
|---|---|---|---|---|---|
| S1 | `bytesobject.c:750` | `_PyBytes_FormatEx` | `PyObject_GetItem(dict, key)` (arbitrary `mp_subscript`) | `res` (raw writer ptr), `fmt`/`fmtcnt` (ptrs into the format buffer), `writer` | **IMMUTABLE** for `fmt` (`self` is `bytes`); `writer` is a private local |
| S2 | `bytesobject.c:904` | `_PyBytes_FormatEx` | `PyObject_ASCII(v)` (`__repr__`) | `res`, `fmt`, `fmtcnt`, `width`, `prec` | same |
| S3 | `bytesobject.c:917/965/986/992/1003` | `_PyBytes_FormatEx` | `format_obj` (`__bytes__`), `formatlong` (`__index__`/`__int__`), `formatfloat` (`__float__`), `byte_converter` (`__index__`) | `res`, `fmt`, `fmtcnt` | same |
| S4 | `bytesobject.c:1162` | `_PyBytes_FormatEx` | `Py_DECREF(args)` (`__del__`) | **`res`, used at `:1164`** `PyBytesWriter_FinishWithPointer` | private writer; not reachable from Python |
| S5 | `bytesobject.c:3004` | `_PyBytes_FromList` | `PyNumber_AsSsize_t(item, NULL)` (`__index__`) | `str` (raw write ptr), `size` (=**allocated**, not list length) | **RE-READ + BOUNDS-CHECKED** — loop bound is `PyList_GET_SIZE(x)` re-read every iteration (`:2999`) and `if (i >= size)` grows the writer at `:3016-3020`. Verified by reading; this is the correctly-hardened twin |
| S6 | `bytesobject.c:3046` | `_PyBytes_FromTuple` | `PyNumber_AsSsize_t(item, NULL)` | `str`, `size` read **once** at `:3034`, no bounds check | **IMMUTABLE** — tuple size cannot change. The missing `i >= size` check that S5 has is *justified*; do not flag it |
| S7 | `bytesobject.c:3087/3095` | `_PyBytes_FromIterator` | `PyIter_Next(it)`, `PyNumber_AsSsize_t(item,…)` | `str`, `size`, `i` | writer-local |
| S8 | `bytesobject.c:2127` | `do_xstrip` | `PyBuffer_Release(&vsep)` (`__release_buffer__`) | **`s` (=`PyBytes_AS_STRING(self)`), `len`, `i`, `j`** used at `:2129`, `:2133` | **IMMUTABLE** — this is the *exact* shape that is a live UAF on the bytearray side (see F1 below). It is safe here **only** because `self` is `bytes`. Do not generalise the negative |
| S9 | `bytesobject.c:2358/2362` | `bytes_translate_impl` | `PyBuffer_Release(&table_view)` / `(&del_table_view)` | `del_table_chars`/`table_chars` — raw ptrs **into the released views' buffers** — used at `:2364-2377` | **EXPOSED-looking; needs Group A.** `trans_table` is a local `char[256]` copy, but `del_table_chars` at `:2313` points into `del_table_view.buf`, and `:2362` releases that view before `:2364-2377`. Worth one careful read |
| S10 | `bytesobject.c:2725` | `_PyBytes_FromHex` | `PyBuffer_Release(&view)` | **`buf`** (writer ptr) used at `:2727` | writer-local |
| S11 | `bytesobject.c:3290` | `PyBytes_Concat` | `PyObject_GetBuffer(w, &wb, ...)` (`__buffer__`) | the `_PyObject_IsUniquelyReferenced(*pv) && PyBytes_CheckExact(*pv)` decision taken at `:3285`, relied on at `:3302` for an in-place resize | **EXPOSED-looking; needs Group A.** A `__buffer__` that takes another reference to `*pv` between `:3285` and `:3302` invalidates the uniqueness premise |
| S12 | `bytesobject.c:1525/1663/1668` | `bytes_str`, `bytes_richcompare` | `PyErr_WarnEx(BytesWarning, …)` — runs the warnings machinery | borrowed `op`/`aa`/`bb` | held by caller frames |
| S13 | `bytesobject.c:3472` | `striter_reduce` | `_PyEval_GetBuiltin(&_Py_ID(iter))` | **nothing** — `it` deliberately re-loaded at `:3477` after the call, per gh-101765 | **RE-READ**; the in-file exemplar |

## B.4 — `Objects/bytearrayobject.c` (mutable + exportable — the real habitat)

| # | site | function | user-Python call | carried across | verdict |
|---|---|---|---|---|---|
| A1 | `bytearrayobject.c:107` | `_bytearray_with_buffer` | the `_Py_bytes_*` op (→ `__index__`, `__buffer__`) | `PyByteArray_AS_STRING(self)`, `Py_SIZE(self)` **as arguments** | **PINNED** `:106`/`:108` |
| A2 | `bytearrayobject.c:1382` | `bytearray_contains` | `_Py_bytes_contains` | data ptr + size as arguments | **PINNED** `:1381`/`:1385` |
| A3 | `bytearrayobject.c:1819` / `1943` | `bytearray_split_impl` / `rsplit_impl` | `PyObject_GetBuffer(sep, …)` | `sbuf` (`:1807`/`:1931`), `slen` | **PINNED** `:1806`/`:1930` |
| A4 | `bytearrayobject.c:2674` | `bytearray_hex_impl` | `_Py_strhex_with_sep` (dispatches on user `sep`) | `argbuf` (`:2668`), `arglen` | **PINNED** `:2673`. Comment cites **gh-143195** — *this is the fixed exemplar of the whole class* |
| A5 | `bytearrayobject.c:2566` | `bytearray_join_impl` | `stringlib_bytes_join` (`PySequence_Fast` + getbuffer on items) | sep data ptr | **PINNED** `:2565`/`:2567` |
| A6 | `bytearrayobject.c:2853` | `bytearray_mod_lock_held` | `_PyBytes_FormatEx` (runs `__index__`/`__bytes__`/`mp_subscript` with the format ptr live) | data ptr + size as arguments | **PINNED** `:2852`/`:2856` |
| **A7** | **`bytearrayobject.c:2391`** | **`bytearray_strip_impl_helper`** | **`PyBuffer_Release(&vbytes)` → `__release_buffer__`** | **`myptr` (`:2375`), `left`, `right` — used at `:2392`** | **EXPOSED — REPRODUCED UAF, see F1** |
| A8 | `bytearrayobject.c:695` | `bytearray_setitem_lock_held` | `_getbytevalue(value, &ival)` → `__index__` | index `i` | **RE-READ** — `:692-693` carries the gh-91153 comment: *"We need to do this **before** the size check, in case value has a nasty `__index__` method that changes the size of the bytearray"*. `Py_SIZE` re-checked at `:699-706`. **This is the file's canonical statement of the discipline** |
| A9 | `bytearrayobject.c:748` | `bytearray_ass_subscript_lock_held` | `_getbytevalue(values, &ival)` → `__index__` | `i` from `:738` | **RE-READ** — re-bounds-checked `:752-759`; `AS_STRING` re-read at `:770` |
| A10 | `bytearrayobject.c:775` / `502` | `bytearray_ass_subscript_lock_held` / `subscript_lock_held` | `PySlice_Unpack` (`__index__`) | nothing | **ORDERED** — comment at `:733-735` explicitly states no buffer pointer is held |
| A11 | `bytearrayobject.c:806` | `bytearray_ass_subscript_lock_held` | recursive self-call after `PyByteArray_FromObject(values)` at `:803` | `start`/`stop`/`step`/`slicelen` are **discarded** and recomputed | **RE-READ**, but note it **evaluates `index.__index__()` / `PySlice_Unpack` a second time** — a user slice object sees two calls. Behavioural, not memory-safety |
| A12 | `bytearrayobject.c:1077` / `1088` | `bytearray___init___impl` | `iternext(it)` / `_getbytevalue` (`__index__`) | cached `iternext` slot ptr (`:1069`) | **RE-READ** for sizes (`:1094-1100` re-read `Py_SIZE`, `ob_alloc`, `AS_STRING`). **But this path bypasses `_canresize` entirely — see F2** |
| A13 | `bytearrayobject.c:2216` / `2217` / `2227` | `bytearray_extend_impl` | `PyIter_Next(it)`, `_getbytevalue`, `Py_DECREF(item)` | `buf` (`:2214`), `buf_size`, `len` | **RE-READ + UNREACHABLE** — `buf` is recomputed at `:2248` with the comment *"Recompute the `buf' pointer, since the resizing operation may have invalidated it"*, and the target `bytearray_obj` is a **private local** the user cannot reach |
| A14 | `bytearrayobject.c:1176` | `bytearray_richcompare` | `PyObject_GetBuffer(other, …)` (`__buffer__`) | **`self_bytes.buf` + `self_size` (`:1174`)** used at `:1183-1191` | **PINNED** — the view on `self` holds `ob_exports`; a resize of `self` from `other.__buffer__` raises `BufferError` |
| A15 | `bytearrayobject.c:1194-1201` | `bytearray_richcompare` | `PyBuffer_Release(&self_bytes)` / `(&other_bytes)` | `cmp`, `self_size`, `other_size`, `op` — used at `:1197-1201` | **SCALARS ONLY** — no pointer survives |
| A16 | `bytearrayobject.c:1171` / `1177` | `bytearray_richcompare` | **`PyErr_Clear()` unnarrowed after `PyObject_GetBuffer`** | — | the 2 `scan_pyerr_clear` / `scan_error_paths` hits; hand to the pyerr-clear agent |
| A17 | `bytearrayobject.c:1213` | `bytearray_dealloc` | `PyErr_Print()` → `sys.excepthook` = arbitrary Python **during dealloc** | `self` at refcount 0, used at `:1215-1216` | **NEEDS Group A.** Only reachable when `ob_exports > 0` at dealloc — i.e. already a miscount |
| A18 | `bytearrayobject.c:1856` / `1896` | `bytearray_partition_impl` / `rpartition_impl` | `_PyByteArray_FromBufferObject(sep)` (getbuffer + release) | **nothing** — `AS_STRING(self)` is read *after*, at `:1862`/`:1902` | **ORDERED**. The `sep` buffer is materialised into a private bytearray first, so `stringlib_partition` never sees a user exporter. Real negative |
| A19 | `bytearrayobject.c:1670` | `bytearray_translate_impl` | `PyObject_GetBuffer(deletechars, …)` | **`table_chars`** — raw ptr into `vtable.buf` (`:1666`) — used at `:1692`, `:1702` | **NEEDS Group A** — same shape as S9 |
| A20 | `bytearrayobject.c:1554` | `bytearray_take_bytes_impl` | `PyNumber_AsSsize_t(n, PyExc_IndexError)` (`__index__`) | `size = Py_SIZE(self)` from `:1548`, used at `:1559/1567/1570/1579/1583` alongside `self->ob_start` at `:1587/1597/1604` | **NEEDS Group A** — `_canresize` is checked at `:1575`, but `PyBytes_FromStringAndSize` at `:1587` and `:1597` sits between the guard and the mutations at `:1591-1592`/`:1604-1606` |
| A21 | `bytearrayobject.c:2186` | `bytearray_extend_impl` | `bytearray_setslice(self, Py_SIZE(self), Py_SIZE(self), iterable_of_ints)` → `PyObject_GetBuffer` on a **user** object | `lo`/`hi` = `Py_SIZE(self)` **evaluated as arguments** before the getbuffer | **PARTIAL RE-READ** — `bytearray_setslice` re-clamps only `hi` (`:673-678`), not `lo`. Worth one read by Group A |
| A22 | `bytearrayobject.c:3027` | `bytearrayiter_reduce` | `_PyEval_GetBuiltin(&_Py_ID(iter))` | **nothing** — `it`/`it_index` re-loaded at `:3032-3033`, per gh-101765 | **RE-READ**; in-file exemplar |

---

# §b addendum — two things I measured, not argued

## F1 — REPRODUCED heap-use-after-free: `bytearray.strip()` / `lstrip()` / `rstrip()`

This is the un-fixed sibling of **gh-143195** ("fix UAF in `{bytearray,memoryview}.hex(sep)` via re-entrant
`sep.__len__`", commit `9976c2b6349`), whose fix — `ob_exports++` around the call — is quoted verbatim in the
code at `bytearrayobject.c:2670-2675` (row A4). `bytearray_strip_impl_helper` never got it.

```
bytearrayobject.c:2375   myptr = PyByteArray_AS_STRING(self);   // raw ptr into self's buffer
bytearrayobject.c:2391   PyBuffer_Release(&vbytes);             // -> slot_bf_releasebuffer -> __release_buffer__
bytearrayobject.c:2392   return PyByteArray_FromStringAndSize(myptr + left, right - left);
```

That `PyBuffer_Release` really does run user Python: `Objects/typeobject.c:11487` `slot_bf_releasebuffer` →
`releasebuffer_call_python` → `vectorcall_method(&_Py_ID(__release_buffer__), stack, 2)` at `typeobject.c:11452`.
`self`'s `ob_exports` is **0** throughout `strip` (no bump anywhere in `:2357-2393`), so `_canresize:115` permits
the reallocation.

**Measured** (`repro/mapper_probe_leads.py strip_uaf`, `strip_uaf_control`):

| build | result |
|---|---|
| `release-gil-nojit` | returns `b'P\x90kHU~\x00\x00P\x90kHU~\x00\x00'` — **freed-heap contents disclosed to Python** |
| `debug-gil-nojit` | returns `b'\x80\xc7\x10\xf0\x05\x7f\x00\x00…'` — same |
| `release-gil-nojit-asan` | **`AddressSanitizer: heap-use-after-free`, READ of size 30** |
| control (callback present but non-mutating) | returns `b'PAYLOADPAYLOAD'` — correct, on every build |

ASan frames, verbatim:
```
READ of size 30 ...
    #1 PyByteArray_FromStringAndSize   Objects/bytearrayobject.c:187:9
    #2 bytearray_strip_impl_helper     Objects/bytearrayobject.c:2392:12
freed by thread T0 here:
    #1 _PyBytes_Resize                 Objects/bytesobject.c:3389:9
    #2 bytearray_resize_lock_held      Objects/bytearrayobject.c:280:15
    #3 bytearray_iconcat_lock_held     Objects/bytearrayobject.c:368:9
```
Single-threaded, default GIL build, no `_testcapi`. **Guarded twin: `bytearray_hex_impl:2673`.**
I have **not** searched the CPython tracker for an existing report of the `strip` variant — that, and the
FIX/CONSIDER call, belong to Group A/B. I am reporting it because Phase 1 turned it up while mapping.

## F2 — Python-reachable failing `assert` in `bytearray.__init__`

```
bytearrayobject.c:937   assert(self->ob_bytes_object == Py_GetConstantBorrowed(Py_CONSTANT_EMPTY_BYTES));
bytearrayobject.c:938   assert(self->ob_exports == 0);
```

Trigger (`repro/mapper_probe_leads.py init_assert`): `b = bytearray(b"AB"); b.clear(); mv = memoryview(b); b.__init__("x", "ascii")`.
The `Py_SIZE(self) != 0` test at `:930` skips the guarded `PyByteArray_Resize(self, 0)` when the bytearray is
empty, so control reaches `:938` with `ob_exports == 1`.

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGABRT** — `Assertion 'self->ob_exports == 0' failed` |
| `debug-ft-nojit` (`PYTHON_GIL=0`) | **SIGABRT** — same |
| `release-gil-nojit` | `BufferError` |
| `release-ft-nojit` | `BufferError` |

Same family as CPY-0058 (assert reachable from Python). 4/4 consistent.

## F3 — `bytearray.__new__(bytearray).append(1)` → SIGSEGV, confirming `scan_init_bypass`

Measured (`repro/mapper_probe_structure.py new_bypass`): `len`, `bool`, `repr` all succeed on the bypassed
object; **`append` SIGSEGVs (exit 139) on `release-gil-nojit` and `debug-gil-nojit` alike**. This corroborates
both `scan_init_bypass` findings — `ob_bytes_object` is NULL and `_PyBytes_Resize(&obj->ob_bytes_object, …)`
at `bytearrayobject.c:280` dereferences `*pv`. Already named as a live class in the init-bypass-checker's own
description; recorded here as **confirmed, not novel**.

---

# §c — bytearray resize/export invariants (and whether CPY-0044 transfers)

## The mechanism

| element | location |
|---|---|
| counter | `Py_ssize_t ob_exports` — `Include/cpython/bytearrayobject.h:16` |
| the guard | `_canresize()` — `Objects/bytearrayobject.c:112-121`, raises `BufferError("Existing exports of data: object cannot be re-sized")` at `:116-117` |
| `bf_getbuffer` | `bytearray_getbuffer_lock_held:54-71` — `ob_exports++` at `:69`, **after** `PyBuffer_FillInfo` succeeds |
| `bf_releasebuffer` | `bytearray_releasebuffer:83-91` — `ob_exports--` at `:88`, `assert(>= 0)` at `:89` (debug-only) |
| the only reallocator | `bytearray_resize_lock_held:211-292` → `_PyBytes_Resize(&obj->ob_bytes_object, alloc)` at `:280` |

**This tree carries the bytes-backed-bytearray rework.** `ob_bytes` now points into a `PyBytesObject` held in the
new `ob_bytes_object` field (`Include/cpython/bytearrayobject.h:17`); there is **no** direct
`PyObject_Realloc`/`PyMem_Realloc` on `ob_bytes` left in the file. That is also where 6 of the 10
`scan_deprecated_apis` hits come from (`_PyBytes_Resize`) — they are the new architecture, not legacy debt.

## Every `_canresize` call site — 6, and they cover every reallocating path

| # | site | covers |
|---|---|---|
| 1 | `:235` `bytearray_resize_lock_held` | `PyByteArray_Resize`, `+=`, `*=`, `append`, `insert`, `extend`, `clear`, `resize`, `__init__` slow path, slice growth |
| 2 | `:563` `bytearray_setslice_linear` (shrink branch) | slice assignment that shrinks |
| 3 | `:833` `bytearray_ass_subscript_lock_held` | extended-slice delete |
| 4 | `:1575` `bytearray_take_bytes_impl` | `bytearray.take_bytes` |
| 5 | `:2308` `bytearray_pop_impl` | `pop` |
| 6 | `:2343` `bytearray_remove_impl` | `remove` |

**Measured** (`repro/mapper_probe_structure.py exported_resize`), holding a live `memoryview`:

```
append=BufferError   clear=BufferError    iadd=BufferError    pop=BufferError
remove=BufferError   delslice=BufferError delextslice=BufferError
resize=BufferError   init_nonempty=BufferError
```
**9 of 9 blocked, on both `release-gil-nojit` and `debug-gil-nojit`.**

## Verdict on the CPY-0044 analogy — **it does not transfer as stated**

The brief's §2 shape 1 is *"a live `memoryview` over a `bytearray` whose buffer is then reallocated."*
**That is disproven.** Every Python-visible resize path checks `ob_exports` and raises `BufferError`; I could not
construct a case where a live `memoryview` failed to block a reallocation.

What CPY-0044 *does* transfer as is the **family**: "read state, run user code, keep using the state". bytearray's
answer to that family is `ob_exports` used as a **general re-entrancy pin, not a buffer-protocol counter** —
11 hand-written `ob_exports++`/`--` pairs (`:106/108`, `:1381/1385`, `:1806/1828`, `:1930/1952`, `:2565/2567`,
`:2673/2675`, `:2852/2856`) with nothing to do with an actual exported buffer. The bug class here is therefore
**"a site that should have taken the pin and didn't"** — of which F1 is a reproduced instance and A20/A21 are
open questions. Point Group A/B at *that*, not at memoryview.

## Two residual observations for the FT and error-path agents

- **`ob_exports` has zero `FT_ATOMIC_*` accesses.** All 22 reads/writes are plain (`:69, 88, 89, 106, 108, 115,
  174, 927, 938, 1210, 1381, 1385, 1806, 1828, 1930, 1952, 2565, 2567, 2673, 2675, 2852, 2856`). 18 are inside a
  critical section; **4 are not**: `:174` (construction-time, object not yet reachable — benign), `:927` and
  `:938` (`bytearray___init___impl` — and I verified the clinic wrapper for `bytearray.__init__` takes **no**
  critical section), and `:1210` (`bytearray_dealloc`). For contrast `memoryview` uses `FT_ATOMIC_ADD_SSIZE`
  (`Objects/memoryobject.c:1632/1641`).
- **A failed `_PyBytes_Resize` silently empties the bytearray.** `:281-284` sets `ob_bytes_object` to the empty
  bytes and `size = alloc = 0` before returning `-1`. Data loss under OOM, with `MemoryError` set. Not a crash;
  worth one line from the error-path agent.

---

# §a — include / dependency graph, and what it constrains

## Tiers, scoped to the four files (72 directives, 40 distinct headers)

| tier | distinct | per file (list / bytes / bytearray / bytes_methods) |
|---|---|---|
| Public (`Include/*.h`) | 2 | 1 / 1 / 2 / 1 — `Python.h` everywhere, plus `bytesobject.h` in bytearrayobject.c |
| CPython (`Include/cpython/*.h`) | 0 | reached only transitively, through `Python.h` |
| **Internal (`Include/internal/pycore_*.h`)** | **24** | **15 / 15 / 9 / 2** |
| Generated (`Objects/clinic/*.c.h`) | 3 | 1 / 1 / 1 / 0 |
| Other local (`stringlib/*.h`) | 10 | 0 / 9 / 8 / 4 |
| Unresolved | 0 | — |
| System | 1 | 1 / 1 / 0 / 0 (`stddef.h`) |

No API-tier violation: all four are core-build translation units and are entitled to `pycore_*`.
**The only cycle in the whole checkout is the known `pycore_structs.h` ↔ `pycore_context.h`** — nothing in this
slice participates.

## The dependencies that constrain a fix

**1. `pycore_typeobject.h` — static type-version tags → the specializing interpreter.**
```
listobject.c:3969       .tp_version_tag = _Py_TYPE_VERSION_LIST        (= 3)
bytearrayobject.c:2942  .tp_version_tag = _Py_TYPE_VERSION_BYTEARRAY   (= 9)
bytesobject.c:3270      .tp_version_tag = _Py_TYPE_VERSION_BYTES       (= 10)
```
(`Include/internal/pycore_typeobject.h:20/26/27`; consumed by `_PyType_LookupByVersion`, `Objects/typeobject.c:1349-1373`.)

**This is the single most important constraint in the slice.** `list` has an *inline bytecode implementation*
that never calls the slots in `listobject.c`:

| uop / instruction | `Python/bytecodes.c` | what it does instead |
|---|---|---|
| `_BINARY_OP_SUBSCR_LIST_INT` | `:1144-1156` | `PyList_GET_ITEM(list, index)` direct — not `list_item`, not `list_get_item_ref` |
| `_STORE_SUBSCR_LIST_INT` | `:1422-1438` | `_PyList_ITEMS(list)[index]` direct store — not `list_ass_item` |
| `FOR_ITER_LIST` / `_ITER_NEXT_LIST*` | `:3935-4007` | `PyList_GET_ITEM` / `_PyList_GetItemRefNoLock` |
| `UNPACK_SEQUENCE_LIST` | `:2106-2113` | `_PyList_ITEMS` direct |
| `_BINARY_OP_SUBSCR_LIST_SLICE` | `:1174-1176` | `_PyList_SliceSubscript` |

⇒ **A guard added to `list_item` / `list_ass_item` / `list_subscript` in `Objects/listobject.c` does not cover
`a[i]`, `a[i] = v`, `for x in a`, or `a, b = lst` on the fast path.** Any such fix must be mirrored in
`Python/bytecodes.c` and regenerated into `generated_cases.c.h` / `executor_cases.c.h` / `optimizer_cases.c.h`
— all outside this slice. State that limitation rather than proposing a slot-only fix.

**2. `pycore_stackref.h` + `pycore_critical_section.h` + `pycore_pyatomic_ft_wrappers.h` — the free-threaded build.**
`listobject.c` uses deferred refcounting directly: `_Py_TryIncrefCompareStackRef` (`:446`), `_Py_TryXGetRef`
(`:374`), `_PyList_GetItemRefNoLock` (`:429`), `ensure_shared_on_resize` / `_PyObject_GC_SET_SHARED`
(`:76-89`, `:139`, `:340`, `:891`, `:3198`), and QSBR-delayed frees via `_PyMem_FreeDelayed` (`:65`).
`list_get_item_ref` has **two entirely different bodies** (`#ifdef Py_GIL_DISABLED` at `:354` vs `:381`).
Per the FP taxonomy, never reason across that `#if` boundary. `bytearrayobject.c` uses `FT_ATOMIC_*` only for
`ob_alloc` (`:51`, `:2538`, `:2747`) and the iterator's `it_index`.

**3. `pycore_dict.h` + `pycore_setobject.h` — listobject.c reads other components' internals.**
`_PyDict_Next` (`:1412`, `:1441`), `_PySet_NextEntryRef` (`:1386`), `_PyDictViewObject` (`:1488`, `:1494`, `:1500`),
`PyDict_GET_SIZE` / `PySet_GET_SIZE`. Six of `_list_extend`'s ten branches (`:1456-1511`) take a
`Py_BEGIN_CRITICAL_SECTION2` on a **dict or set** it does not own. A change to dict/set iteration invalidates
`list(d)` / `list(s)`; a change here must be checked against `Objects/dictobject.c` and `Objects/setobject.c`.

**4. `pycore_freelist.h`** — list and bytes both use freelists (`_Py_FREELIST_POP`/`_Py_FREELIST_FREE`), so
allocation changes interact with per-interpreter freelist state.

**5. `stringlib/` is textual inclusion with different macros per includer.** `bytesobject.c:1384-1395` pulls in
`stringdefs.h` then eight `stringlib` headers; `bytearrayobject.c:1223-1244` defines its own macro set
(`STRINGLIB_MUTABLE 1`, `STRINGLIB_STR PyByteArray_AS_STRING`) and includes the *same* eight. Every
`stringlib_*` function therefore exists **twice** with different semantics. A `Objects/stringlib/*.h` change is a
change to both types at once, and the `STRINGLIB_MUTABLE 1` side is the one that can have its buffer moved.

**6. Include hygiene (POLICY).** `bytesobject.c` and `bytearrayobject.c` use `_Py_TYPE_VERSION_BYTES` /
`_Py_TYPE_VERSION_BYTEARRAY` **without including `pycore_typeobject.h`** — they get it transitively via
`pycore_object.h:16`. `listobject.c:18` includes it directly, with a comment naming exactly that symbol.
Minor; noted so a later agent does not read it as a defect.

---

# §d — layout and the shared-implementation file

## Struct fields

| type | header | fields |
|---|---|---|
| `PyListObject` | `Include/cpython/listobject.h:5-22` | `ob_item` (`PyObject **`), `allocated`. Documented invariants: `0 <= ob_size <= allocated`; `ob_item == NULL ⇒ ob_size == allocated == 0`; **`list.sort()` temporarily sets `allocated` to -1 to detect mutations**; items must not be NULL except during construction |
| `PyBytesObject` | `Include/cpython/bytesobject.h:5-15` | `ob_shash` (`Py_DEPRECATED(3.11)` — the other 4 `scan_deprecated_apis` hits), `ob_sval[1]` flexible tail. Invariants: `ob_sval` has `ob_size+1` bytes; `ob_sval[ob_size] == 0` |
| `PyByteArrayObject` | `Include/cpython/bytearrayobject.h:6-18` | `ob_alloc`, **`ob_bytes`** (physical backing buffer), **`ob_start`** (logical start *inside* `ob_bytes`), `ob_exports`, **`ob_bytes_object`** (the backing `PyBytes`, new) |

**The three data-pointer accessors are not interchangeable:**
`PyBytes_AS_STRING(op)` → `ob_sval` (never moves) · `PyByteArray_AS_STRING(op)` → **`ob_start`**, i.e.
`ob_bytes + logical_offset`, and **both parts move** (`bytearray_reinit_from_bytes:49` reassigns them together;
`bytearray_setslice_linear:568` slides `ob_start` alone; `bytearray_take_bytes_impl:1591` and `:1606` do both).
`PyList_GET_SIZE` and `PyByteArray_GET_SIZE` are `_Py_atomic_load_ssize_relaxed` under `Py_GIL_DISABLED`;
`PyBytes_GET_SIZE` is a plain `Py_SIZE`.

## `bytes_methods.c` — what is shared, and therefore doubled

`Include/internal/pycore_bytes_methods.h` publishes **19 functions + 27 shared doc strings**. Measured call sites:

| function | called from bytesobject.c | called from bytearrayobject.c | doubled? |
|---|---|---|---|
| `_Py_bytes_find` | `:2038` | `:1270` (via `_bytearray_with_buffer`) | **yes** |
| `_Py_bytes_index` | `:2056` | `:1334` | **yes** |
| `_Py_bytes_rfind` | `:2074` | `:1352` | **yes** |
| `_Py_bytes_rindex` | `:2092` | `:1370` | **yes** |
| `_Py_bytes_count` | `:2243` | `:1286` | **yes** |
| `_Py_bytes_contains` | `:1625` | `:1382` | **yes** |
| `_Py_bytes_startswith` | `:2534` | `:1412` | **yes** |
| `_Py_bytes_endswith` | `:2559` | `:1437` | **yes** |
| `_Py_bytes_maketrans` | `:2404` | `:1751` | **yes** |
| `_Py_bytes_repr` | `:1437` (**defined** at `:1442`) | `:1118` | **yes** — note it lives in `bytesobject.c`, not `bytes_methods.c` |
| `_Py_bytes_is{space,alpha,alnum,ascii,digit,lower,upper,title}` (8) | via `stringlib/ctype.h` | via `stringlib/ctype.h` | yes, but pure `(const char*, len)` — no user code |
| `_Py_bytes_{lower,upper,title,capitalize,swapcase}` (5) | via `stringlib/ctype.h` | via `stringlib/ctype.h` | yes, pure |

**All 9 user-code-reaching shared functions are doubled.** A defect in `find_internal` (`:452-510`) lands on
`bytes.find/index/rfind/rindex` **and** `bytearray.find/index/rfind/rindex` at once — and it is the bytearray
side that can dangle (see the M-note). Give `bytes_methods.c` weight out of proportion to its 738 lines.

---

# §e — Argument Clinic and critical sections (the mod-io trap, quantified)

## The count the scanner reports is 57% of the truth

| file | CS **functions** | CS macro sites | scanned by `scan_lock_discipline`? |
|---|---|---|---|
| `Objects/listobject.c` | 22 | 33 (23 `…SECTION(` + 10 `…SECTION2(`) | ✅ |
| `Objects/bytearrayobject.c` | 32 | 34 (32 + 2) | ✅ |
| `Objects/bytesobject.c` | 0 | 0 | ✅ |
| `Objects/bytes_methods.c` | 0 | 0 | ✅ |
| **scanned subtotal** | **54** | **67** | — |
| `Objects/clinic/listobject.c.h` | **8** | 8 | ❌ **never scanned** |
| `Objects/clinic/bytearrayobject.c.h` | **33** | 33 | ❌ **never scanned** |
| `Objects/clinic/bytesobject.c.h` | 0 | 0 | ❌ |
| **TOTAL** | **95** | **108** | **54 / 95 = 57% covered** |

`scan_lock_discipline.sample.json` confirms it: `files_analyzed: 4`, `_sample.files_scanned` lists exactly the
four `.c` files, and `vocabulary_counts` (`Py_BEGIN_CRITICAL_SECTION: 55, …SECTION2: 12`) matches the `.c` files
alone. **The 41 clinic-emitted regions are invisible to the scanner and to anyone reading the `.c`.**

Coincidentally the same grand total as mod-io (95); the split is different — 41 of 95 here vs 87 of 95 there.

## Can a later agent trust the `.c` file alone? **No — for two independent reasons.**

**(i) The lock is in the header.** The 41 clinic wrappers hold `Py_BEGIN_CRITICAL_SECTION(self)` around an
`_impl` whose body in the `.c` shows no macro at all. Example, `Objects/clinic/listobject.c.h:48-50`:
```c
    Py_BEGIN_CRITICAL_SECTION(self);
    return_value = list_insert_impl((PyListObject *)self, index, object);
    Py_END_CRITICAL_SECTION();
```
The 41: `list_{append,copy,insert,pop,remove,reverse,sort}`, `py_list_clear`; and 33 bytearray methods
(`append, copy, count, decode, endswith, extend, find, hex, index, insert, join, lstrip, partition, pop, reduce,
reduce_ex, remove, removeprefix, removesuffix, replace, resize, reverse, rfind, rindex, rpartition, rsplit,
rstrip, split, splitlines, startswith, strip, take_bytes, translate`).
Conversely `bytearray.__init__` has **no** clinic critical section — the `.c` alone cannot tell you that either.

**(ii) User Python runs before the lock and before the `.c` is entered.** 18 clinic wrappers execute an
arbitrary-Python converter *ahead* of `Py_BEGIN_CRITICAL_SECTION`:

| converter | wrappers (clinic line → CS line) |
|---|---|
| `_PyNumber_Index` (`__index__`) | `list_insert` 37→48, `list_pop` 178→189; `bytearray_resize` 618→628, `bytearray_replace` 869→880, `bytearray_split` 970→981, `bytearray_rsplit` 1130→1141, `bytearray_insert` 1202→1215, `bytearray_pop` 1314→1325, `bytearray_hex` 1788→1799 |
| `_PyEval_SliceIndex` (`__index__`) | `bytearray_find` 143/149→153, `count` 194/200→204, `index` 289/295→299, `rfind` 342/348→352, `rindex` 395/401→405, `startswith` 448/454→458, `endswith` 501/507→511 |
| `PyObject_GetBuffer` (`__buffer__`) | `bytearray_removeprefix` 541→544, `removesuffix` 579→582, `replace` 858/861→880 |

Reading `list_insert_impl` or `bytearray_pop_impl` in the `.c` you see a plain `Py_ssize_t index` and no sign
that a user `__index__` already ran and may have emptied the object. **Every agent triaging an index or a
buffer argument in this slice must open the matching `Objects/clinic/*.c.h`.**

Note the direction is the *safe* one here (converter before lock — the same rule `_PyList_BinarySlice:725-733`
states in prose), and I found no bug caused by it. The trap is purely one of visibility.

---

# §f — what I can disprove in the brief

## §2, shape 1 — "a live `memoryview` over a `bytearray` whose buffer is then reallocated"

**DISPROVEN as stated.** 9 of 9 Python-visible resize paths raise `BufferError` with a live `memoryview`, on
release and debug (§c). All 6 `_canresize` call sites are present and cover every reallocating route. The
CPY-0044 *family* does transfer, but through `ob_exports`-as-re-entrancy-pin, not through memoryview — and the
un-pinned site (F1) is a UAF I reproduced. Re-aim the slice at "which sites forgot the pin".

## §2, shape 2 — "`list.sort`, `list.remove`, `list.index`, `in`, `count` … all call back into Python while holding a borrowed pointer or an index into `ob_item`"

**Wrong for 4 of the 5 named operations.** Measured by reading:

| operation | claim holds? | why |
|---|---|---|
| `list.sort` | **No — the opposite.** | `list_sort_impl:2963-2973` *detaches* `ob_item`, `ob_size` and `allocated` before any user code runs, with a comment saying mutation-during-sort "is a core-dump factory". No borrowed pointer into a live `ob_item` exists during the sort |
| `list.index` | **No** | `list_index_impl:3340` uses `list_get_item_ref`, which bounds-checks and returns a **strong** reference each iteration |
| `list.count` | **No** | `list_count_impl:3371` — identical |
| `in` (`list_contains`) | **No** | `list_contains:660` — identical |
| `list.remove` | **Yes, but guarded** | `list_remove_impl:3410` does read `self->ob_item[i]` raw — but INCREFs at `:3411`, re-reads `Py_SIZE` as the loop bound at `:3409`, and `list_ass_slice_lock_held` clamps `ilow`/`ihigh` at `:979-987`. Worst case is removing the wrong element, not a memory error |
| `list_richcompare` | (not named) **Yes, but guarded** | `:3459-3467` — both operands INCREF'd before the compare, size re-read at `:3458` |

This is the claim that would have sent Group A–E hunting a borrowed-`ob_item` UAF in `listobject.c` that does not
exist. The habitat for that shape in this slice is **bytearray**, not list.

## §3 — scanner-baseline readings

| brief's line | status |
|---|---|
| `scan_lock_discipline` "0 … denominator **54 critical-section fns** … real negative — **54 regions**, no leaks" | **Two errors.** (a) 54 is *functions*, not regions — there are **67** macro sites in the `.c` files. (b) The denominator is **incomplete**: 41 further CS functions live in the two clinic headers and were never scanned. The honest reading is "0 leaks across **54 of 95** CS functions (57%)" |
| `scan_gil_usage` "**STRUCTURAL ZERO. Do not certify clean.**" | **Resolved: genuinely absent, not mis-spelled.** `grep` over all four files for `Py_BEGIN_ALLOW_THREADS`, `Py_END_ALLOW_THREADS`, `PyGILState_*`, `PyEval_SaveThread`, `PyEval_RestoreThread`, `Py_BLOCK_THREADS`, `Py_UNBLOCK_THREADS`, `HEAD_LOCK` returns **nothing**. These four files never release the GIL. The zero is real; the rule has nothing to check here |
| `scan_recursion_guards` "**1 of 1** recursion-prone slot fn — triage it carefully" | **The denominator is an undercount.** The single hit is `list_richcompare_impl:3467`, correctly typed as `guarded_by_dispatcher` (`PyObject_RichCompare` wraps `_Py_EnterRecursiveCallTstate`, `Objects/object.c:1099`) — an auditability note, not a finding. But `slot_classification` reports `from_slot_map: 0, from_name_suffix: 13`: `PyList_Type` (`listobject.c:3926`) is a **mixed positional/designated** table (positional slots with trailing comments, plus `.tp_vectorcall` / `.tp_version_tag` / `._tp_iteritem`), so the slot map resolved nothing and the classifier fell back to name-suffix guessing. Slots the rule did **not** classify include `bytes_richcompare`, `bytearray_richcompare`, `bytearray_repr`, `bytes_repr`, `bytearray_contains`. This is the FP-taxonomy "markers only exist in comments" trap firing on a *denominator* |
| `scan_refcounts` "37 is a headline, not a denominator — type them" | **The 37 sites are not enumerated in the sample JSON** (`findings: []`, `field_accessors_discovered: ["list_capacity"]`, only the aggregate `borrowed_slot_load_sites: 37` survives), so I cannot type them from the artifact. My read of the slice by hand says the real population of borrowed `PyObject*` loads that reach a Python-calling site is **6**: `listobject.c:3410` (remove), `:3459`, `:3460`, `:3490`, `:3491` (richcompare), `:2993` (sort keyfunc); of those, all six are either INCREF'd across the call or operate on the detached array. **Recall gap to propose: have `scan_refcounts` emit the site list whenever the denominator is non-zero, even with zero findings** |
| `scan_uninit_dealloc` "1, real, and non-zero unlike mod-io" | **Correct, and it is a known finding**: `PyList_New` `Objects/listobject.c:250/262` = **CPY-0014**. Confirmed present, not re-litigated. Useful recall data point: the rule re-found a catalog entry unaided |
| `scan_init_bypass` "2, narrow but real" | **Confirmed dynamically** — F3 above, SIGSEGV on release and debug |
| `scan_ft_races` "6, real" | Corroborated on the two I checked: `bytearray_reinit_from_bytes:49` (plain `ob_start` write vs the guarded `bytearray_setslice_linear:568`) and `bytearray___init___impl:924` lazy init with no critical section — and I independently verified `bytearray.__init__` has **no** clinic critical section at all, which strengthens the second |

## One hypothesis of my own that I falsified

I predicted `bytearray_extend_impl:2241` would trip `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` at
`bytearray_resize_lock_held:214`, because it resizes the private local `bytearray_obj` while the clinic wrapper
holds the lock on `self`, not on `bytearray_obj`. **Measured: it does not.** `b.extend(iter([1,2,3]*200))`
completes cleanly on `debug-ft-nojit` under `PYTHON_GIL=0` and on `release-ft-nojit`. Recorded so nobody spends
time on it.

---

# Handoff summary for Groups A–E

1. **Read `Objects/clinic/listobject.c.h` and `Objects/clinic/bytearrayobject.c.h`.** 41 of the slice's 95
   critical-section functions and 18 arbitrary-Python argument converters live only there.
2. **The re-entrancy habitat is bytearray, not list.** listobject.c is systematically hardened (detach-during-sort,
   `list_get_item_ref`, INCREF-before-compare, recycle-then-DECREF, clamped `list_ass_slice`); §f documents which
   of the brief's list claims fail.
3. **The bug shape to hunt is "should have taken the `ob_exports` pin and didn't."** Guarded twins:
   `bytearray_hex_impl:2673` (with its gh-143195 citation), `_bytearray_with_buffer:106`,
   `bytearray_setitem_lock_held:692` (gh-91153 comment). F1 is a reproduced instance; A19/A20/A21, S9 and S11
   are open candidates.
4. **`bytes_methods.c` defects are doubled** — 9 user-code-reaching shared functions, safe on the bytes side by
   immutability and on the bytearray side only by the pin.
5. **A slot-level fix in `listobject.c` may not run.** `Python/bytecodes.c` reimplements list subscript, store,
   iteration and unpack inline (§a). Say so instead of proposing a slot-only patch.
6. **Files are byte-identical to the build-matrix commit** — every matrix build is valid evidence here.

**Artifacts:** `reports/obj-sequences/repro/mapper_probe_structure.py`,
`reports/obj-sequences/repro/mapper_probe_leads.py`, `reports/obj-sequences/repro/mapper_probe_ft.py`.
