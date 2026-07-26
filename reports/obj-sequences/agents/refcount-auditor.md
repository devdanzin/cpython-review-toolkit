# refcount-auditor — slice `obj-sequences` (Group A)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** re-confirmed independently — `git diff a1d580430c8 4f3be1b5777 --` over the four files is empty. Every line cited here is valid on every matrix build without adjustment.

**Pre-run scanner:** `scan_refcounts.sample.json` — **0 findings**, denominators `borrowed_slot_load_sites: 37`, `borrowed_accessor_load_sites: 2`, `borrowed_field_accessors: 1`.

**Headline:** the scanner's zero is *earned* against its real population, which is **5**, not 37 — and one reproduced crash sits inside that population that the rule cannot see because the defect is concurrency, not re-entrancy. Separately, nine of the toolkit's thirteen scanners are structurally blind to **1,083 lines of `Objects/bytesobject.c`**, including that crash site.

---

## 1. Findings

### [FIX] `striter_next` drops its only owning `it_seq` reference with a plain store + plain `Py_DECREF` and no critical section — `Objects/bytesobject.c:3451-3452` · **NOVEL, REPRODUCED**

```c
3441:    seq = it->it_seq;                            /* plain load  */
3442:    if (seq == NULL) return NULL;
3444:    assert(PyBytes_Check(seq));                  /* deref of seq */
3446:    if (it->it_index < PyBytes_GET_SIZE(seq)) {
3448:        ... seq->ob_sval[it->it_index++] ...     /* plain RMW    */
3449:    }
3451:    it->it_seq = NULL;                           /* plain store  */
3452:    Py_DECREF(seq);                              /* the ONLY owning reference */
```

**What.** Two threads calling `next()` on one shared `bytes` iterator can both fail the `it_index < size` test, both read the same non-NULL `seq`, both store NULL, and both `Py_DECREF` it. The refcount goes negative and the `bytes` is freed while a third thread is still dereferencing it at `:3444`.

**Guarded twins — three of them, two in the same slice, all applying the identical fix:**

| twin | what it does that `striter_next` does not |
|---|---|
| `Objects/bytearrayobject.c:2996-2999` `bytearrayiter_next` | `FT_ATOMIC_LOAD_SSIZE_RELAXED(it->it_index)`, `Py_BEGIN_CRITICAL_SECTION(seq)` for the data read, and the drop is `#ifndef Py_GIL_DISABLED Py_CLEAR(it->it_seq); #endif` — under free-threading it **does not drop at all** |
| `Objects/listobject.c:4078-4082` `listiter_next` | identical `#ifndef Py_GIL_DISABLED` elision + `FT_ATOMIC` index |
| `Objects/listobject.c:4236-4240` `listreviter_next` | identical |

`striter_next` has **none** of the three: no `FT_ATOMIC_*`, no critical section, no `#ifndef` guard. `grep` over `bytesobject.c:3400-3520` for `FT_ATOMIC|CRITICAL_SECTION|Py_GIL_DISABLED` returns nothing.

**Why it matters — this is a bug under CPython's own stated policy.** gh-124397 ("Strategy for Iterators in Free Threading", open) point 3: *"Other iterators implemented in C will get only the minimal changes necessary to cause them to not crash in a free-threaded build. … Concurrent access is allowed to return duplicate values, skip values, or raise an exception."* Duplicate/skipped values are sanctioned; a crash is not. The three twins above are exactly that minimal change; the `bytes` iterator was skipped.

**Measured** (`repro/bytes_iterator_ft_double_decref.py`, 8 threads draining one shared `iter(bytes(range(256))*4)`):

| build | runs | result |
|---|---|---|
| `debug-ft-nojit` (`PYTHON_GIL=0`) | **6/6 crash** | 4 × SIGSEGV (rc=139), 2 × SIGABRT (rc=134) |
| `release-ft-nojit` (`PYTHON_GIL=0`) | **7/8 hang** | rc=124 at 25–90 s |
| `release-ft-nojit`, **1 thread**, 20 rounds | 1/1 | rc=0 in seconds — the control that separates concurrency from workload size |
| `debug-gil-nojit` | 2/2 | rc=0 |
| `release-gil-nojit` | 1/1 | rc=0 |

SIGABRT text, verbatim:
```
Objects/bytesobject.c:3452: _Py_NegativeRefcount: Assertion failed: object has negative ref count
<object at 0x20002703c10 is freed>
```
gdb frame for the SIGSEGV (`debug-ft-nojit`, four threads parked on the same object `op=0x20002152e50`):
```
Thread 11 "Thread-10 (drai" received signal SIGSEGV
#0  striter_next (op=0x20002152e50) at Objects/bytesobject.c:3444
#1  _PyForIter_VirtualIteratorNext          Python/ceval.c:3775
#2  _PyEval_EvalFrameDefault                Python/generated_cases.c.h:6352
```
`:3444` is `assert(PyBytes_Check(seq))` — the use-after-free *read* of the object the racing thread dropped at `:3452`.

**What I did not measure.** I did not run this under TSan (ptrace attach is blocked on this host by `yama/ptrace_scope`, and the debug-FT evidence is already a hard crash). The release-FT **hang** is real (7/8, with a clean 1-thread control) but I have **not** established its mechanism; my hypothesis — the freed `seq` is recycled, `PyBytes_GET_SIZE(seq)` reads garbage, and `for _ in it` never exhausts — is unverified. Falsifier: a hung `release-ft` process whose threads are not inside `striter_next`.

**Fix.** Apply the twins' pattern verbatim:
```c
    FT_ATOMIC_STORE_SSIZE_RELAXED(it->it_index, -1);   /* and load it atomically at :3446 */
#ifndef Py_GIL_DISABLED
    Py_CLEAR(it->it_seq);
#endif
```
No slot-level constraint applies here: unlike `list`, `bytes` iteration is not reimplemented inline in `Python/bytecodes.c` — the gdb frame shows `_PyForIter_VirtualIteratorNext` dispatching through `tp_iternext`.

**Prior art: none found.** `gh api -X GET search/issues` for `bytes iterator free-threading race`, `striter_next`, `bytes iterator thread safety`, `bytes iterator double decref`, `bytesobject.c iterator critical section`, `iter(bytes) thread` returns only the gh-124397 umbrella and gh-153928 (`unicodeobject.c`, a different file). This is the same shape as **CPY-0067** (`arrayiter_next`) and **CPY-0062** (`elementiter_next`) — a third instance, in a builtin type.

---

### [CONFIRMED — known] CPY-0180 · `bytearray_strip_impl_helper` — `Objects/bytearrayobject.c:2375 → 2391 → 2392`

Re-confirmed at the review ref on `release-gil-nojit-asan` via the mapper's existing `repro/bytearray_strip_release_buffer_uaf.py`:
```
AddressSanitizer: heap-use-after-free    READ of size 11
    #1 PyByteArray_FromStringAndSize   Objects/bytearrayobject.c:187
    #2 bytearray_strip_impl_helper     Objects/bytearrayobject.c:2392
freed by thread T0:
    #1 _PyBytes_Resize                 Objects/bytesobject.c:3389
    #2 bytearray_resize_lock_held      Objects/bytearrayobject.c:280
```
One line, per informed-mode rule 1. Guarded twin `bytearray_hex_impl:2673` (gh-143195 cited in its own comment). Siblings hunted in §2 — **there are none left of this exact spelling**.

### [CONFIRMED — known upstream, fixed 9 days AFTER the review ref] gh-153570 · `bytearray_take_bytes_impl` — `Objects/bytearrayobject.c:1548 → 1554 → 1559/1566/1597`

I found and reproduced this by reading before searching the tracker; the prior-art search then turned up gh-153570 / PR #153572, **merged 2026-07-24**, while the review target is dated 2026-07-15. It is therefore **live at `4f3be1b5777`** and fixed on today's main. Recording the evidence because it is stronger than the issue's own and because its fix is the template for §2.

```c
1548:    Py_ssize_t size = Py_SIZE(self);
1554:    to_take = PyNumber_AsSsize_t(n, PyExc_IndexError);   /* user __index__ */
1566:    if (to_take < 0 || to_take > size)                    /* STALE size     */
1575:    if (!_canresize(self))                                /* ob_exports only — passes */
1597:    PyBytes_FromStringAndSize(self->ob_start + to_take, remaining_length)
```
`repro/bytearray_take_bytes_index_uaf.py`, `release-gil-nojit-asan`:
```
AddressSanitizer: heap-buffer-overflow   READ of size 2000
    #1 PyBytes_FromStringAndSize        Objects/bytesobject.c:157
    #2 bytearray_take_bytes_impl        Objects/bytearrayobject.c:1597
    #3 bytearray_take_bytes             Objects/clinic/bytearrayobject.c.h:666
0x71efd8e32c40 is located 1998 bytes after 34-byte region
```
On the plain `release-gil-nojit` build there is **no crash, rc=0** — `take_bytes` returns freed heap containing pointers and the string `open`, and the `grow` scenario returns 3,999 bytes of uninitialised heap (ASan `\xbe` poison). Same disclosure-not-crash signature as CPY-0180. Control (non-mutating `__index__`) is correct on every build.

Upstream fix (`Objects/bytearrayobject.c`, `+2/-0`): `size = Py_SIZE(self);` re-read immediately after the converter. Note the shape of the fix — **a re-read, not the pin**; see §2's closing note.

### [CONFIRMED — known, STILL OPEN] gh-153578 · `bytearray_extend_impl:2186` via `bytearray_setslice:673-678`

PR #153579 verified **open / unmerged** today (`gh api repos/python/cpython/pulls/153579` → `"merged": false`). Live at the ref and on main. `repro/bytearray_extend_setslice_oob.py`:

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGABRT** — `Objects/bytearrayobject.c:560: Assertion 'avail >= 0' failed` (rc=134) |
| `release-gil-nojit` | rc=0, `len(b) == 4004` with 4,000 bytes of stale heap |
| `release-gil-nojit-asan` | rc=0, `b[:16] == b'\xbe'*16` — **uninitialised heap disclosed to Python** |

The upstream PR is a `+3/-0` clamp reorder in `bytearray_setslice`. Guarded twin `bytearray_iconcat_lock_held:356-362`, verified by reading: it calls `PyObject_GetBuffer(other, …)` **first** and reads `Py_ssize_t size = Py_SIZE(self)` afterwards.

### [POLICY] `list_richcompare_impl:3490-3496` pins two locals and then calls with the raw slots

```c
3490:    PyObject *vitem = vl->ob_item[i];
3491:    PyObject *witem = wl->ob_item[i];
3492:    Py_INCREF(vitem);
3493:    Py_INCREF(witem);
3494:    PyObject *result = PyObject_RichCompare(vl->ob_item[i], wl->ob_item[i], op);
```
The INCREF/DECREF pair is written against `vitem`/`witem` but the call re-reads the slots. It is **correct today** — nothing runs between `:3490` and `:3494`, so the two loads are provably the same objects, and `i` was re-bounds-checked at `:3476` against a freshly re-read `Py_SIZE`. It is one inserted statement away from a UAF, and passing the pinned locals costs nothing. The mapper (L6) called this "fragile, not broken"; I confirm that reading and add the reason it is not broken. **Not a bug.**

### [ACCEPTABLE] `bytearray_dealloc:1212` runs `PyErr_Print()` (→ `sys.excepthook`) at refcount 0

Reachable only when `ob_exports > 0` at dealloc, which already implies a miscount elsewhere: every in-tree `ob_exports++` is bracketed inside a single function on a live object, and a real buffer export is held by a `memoryview` that owns a reference. The only route is an extension that calls `PyObject_GetBuffer` and then `Py_DECREF`s without `PyBuffer_Release` — an extension bug. One line, no action.

---

## 2. THE MAIN JOB — the cached-raw-pointer class, exhaustively

**Method.** `scan_refcounts._borrowed_slot_loads()` only matches `local = owner->field;`. The habitat's shape is broader: *any* raw value derived from the receiver, cached into a local, carried across a call that reaches user Python. I swept all four files for
`{PyByteArray_AS_STRING, PyBytes_AS_STRING, PyByteArray_GET_SIZE, PyBytes_GET_SIZE, PyList_GET_SIZE, Py_SIZE(, ->ob_start, ->ob_bytes, ->ob_sval, ->ob_item, .buf, .len}` on the RHS, followed by any of 40 Python-reaching APIs, followed by a use — then hand-read every hit. Script: `/tmp/.../scratchpad/sweep_cached_raw.py`.

**Population: 56 functions across the four files; 23 in `bytearrayobject.c`.** Then I cross-checked against every occurrence of `PyByteArray_AS_STRING(self|op|ba)` / `self->ob_start` / `obj->ob_start` in `bytearrayobject.c` (51 sites, `grep`) so that argument-position uses — the `_bytearray_with_buffer(self, op, …)` spelling, which the local-assignment sweep cannot see — are also covered.

### The load-bearing negative: allocation in these files cannot run user Python

`Python/gc.c:1965-1971`:
```c
_Py_ScheduleGC(PyThreadState *tstate)
{ if (!_Py_eval_breaker_bit_is_set(tstate, _PY_GC_SCHEDULED_BIT)) {
      _Py_set_eval_breaker_bit(tstate, _PY_GC_SCHEDULED_BIT); } }
```
A GC-tracked allocation only *schedules* a collection; the collection runs at the next bytecode boundary, never inside the allocator. And `bytearray` is not GC-tracked at all — `gc.is_tracked(bytearray(b'x'))` is `False`, `bytearray.__flags__ & Py_TPFLAGS_HAVE_GC` is 0, `PyByteArray_FromStringAndSize:167` uses `PyObject_New` — so `PyByteArray_FromStringAndSize` / `PyBytes_FromStringAndSize` cannot execute a `__del__` even indirectly.

**This one fact dismisses 14 of the 56 sweep rows.** Without it a reader would classify `bytearray_subscript_lock_held:515→519→526`, `bytearray_repeat_lock_held:396→402→405`, `bytearray_removeprefix_impl:1458→1466`, `bytearray_removesuffix_impl:1491→1500`, `bytearray_translate_impl:1680→1682` and `bytes_*`'s equivalents as live re-entrancy windows. They are not.

### The pin inventory — who takes it, who should

**Takes the `ob_exports++/--` pin (7 brackets, covering 15 Python-visible methods):**

| bracket | function | covers |
|---|---|---|
| `:106/:108` | `_bytearray_with_buffer` | `find`, `count`, `index`, `rfind`, `rindex`, `startswith`, `endswith` (call sites `:1270, 1286, 1334, 1352, 1370, 1412, 1437`) |
| `:1381/:1385` | `bytearray_contains` | `in` |
| `:1806/:1828` | `bytearray_split_impl` | `split` — gh-142560 |
| `:1930/:1952` | `bytearray_rsplit_impl` | `rsplit` — gh-142560 |
| `:2565/:2567` | `bytearray_join_impl` | `join` |
| `:2673/:2675` | `bytearray_hex_impl` | `hex(sep)` — gh-143195, the canonical twin, issue URL in its comment |
| `:2852/:2856` | `bytearray_mod_lock_held` | `%` — gh-142557 |

(`:69/:88` is `bf_getbuffer`/`bf_releasebuffer` itself, not a re-entrancy pin.)

**Does not take the pin, and is correct by ORDERING — the value is read after the last Python-reaching call (8):**

| function | ordering evidence |
|---|---|
| `bytearray_iconcat_lock_held:356→362` | `PyObject_GetBuffer` first, `Py_SIZE(self)` after. **The named twin for gh-153578.** |
| `bytearray_setitem_lock_held:687-706` | `_getbytevalue` above the size check, gh-91153 round 1, comment in place |
| `bytearray_ass_subscript_lock_held:738-778` | `PyNumber_AsSsize_t` and `_getbytevalue` both above every `Py_SIZE`/`GET_SIZE` read (gh-91153 round 2); the `values == self \|\| !PyByteArray_Check(values)` case copies-and-recurses at `:802-808`, re-deriving `start/stop/step/slicelen`; `buf` is re-loaded at `:831` and `:872`; the only intervening `PyErr_Format` (`:875`) has an all-`%zd` format |
| `bytearray_partition_impl:1857→1862` / `rpartition_impl:1897→1902` | `sep` is materialised into a private bytearray by `_PyByteArray_FromBufferObject` *before* `AS_STRING(self)` is read |
| `bytearray_translate_impl:1670→1680/1685` | both `PyObject_GetBuffer`s precede `inlen` and `input`; both `PyBuffer_Release`es (`:1720`, `:1722`) follow the last use of `table_chars`/`input`; `result` is a private local. **Resolves the mapper's A19 as a real negative.** |
| `bytearray_extend_impl:2214→2248` | `buf` recomputed after every resize, with the comment saying why; the target is a private local |
| `bytearray_repeat_lock_held:402→403` | `buf` read after the allocation |
| `bytearray_subscript_lock_held:489→505` | `PySlice_Unpack` (user `__index__`) precedes `PySlice_AdjustIndices(PyByteArray_GET_SIZE(self), …)` |

**Does not take the pin and does not need it — the only intervening call cannot reach Python (8):** `bytearray_irepeat_lock_held:441`, `bytearray_reverse_impl:1971`, `bytearray_insert_impl:2013`, `bytearray_pop_impl:2311`, `bytearray_remove_impl:2336`, `bytearray_splitlines_impl:2609`, `_common_reduce:2693`, `bytearray_repr_lock_held:1118`, plus `bytearray_setslice_linear:557/606/618` and `bytearray___init___impl:1038` (whose fast path is gated by `PyLong_CheckExact(items[i])` at `:1041`, so `_getbytevalue` at `:1052` never sees a user `__index__`).

**Does not take the pin and is BROKEN (3) — all three known upstream, one still unfixed:**

| site | shape | status |
|---|---|---|
| `bytearray_strip_impl_helper:2375` | raw `char*` across `PyBuffer_Release` → `__release_buffer__` | CPY-0180, **no upstream issue found**; PR #14771 on these functions open since 2019 |
| `bytearray_take_bytes_impl:1548` | `Py_ssize_t` size across `PyNumber_AsSsize_t` → `__index__` | gh-153570, fixed **after** the ref |
| `bytearray_setslice:673` (`lo`) via `bytearray_extend_impl:2186` | `Py_ssize_t` bound across `PyObject_GetBuffer` → `__buffer__` | gh-153578, PR **open** |

### The answer to "how many *should* take the pin"

**One.** The history agent's FP-3 table listed 16 functions and asked which of the unguarded ones want the bracket. The correct answer is narrower than the framing suggests, and the distinction is the useful output:

- **A cached raw *pointer* wants the pin.** `ob_exports++` is the only thing that stops `_canresize` from letting the callback reallocate the buffer under a live `char*`. That is exactly one unguarded site: `bytearray_strip_impl_helper`. Its guarded twin `bytearray_split_impl:1806/1828` performs the identical `GetBuffer → scan → Release` dance *inside* the window.
- **A cached *size* wants a re-read, not the pin.** `ob_exports` does not stop `__index__`/`__buffer__` from shrinking `self` — a bump only blocks the *reallocation*, and `Py_SIZE` changes on paths that stay inside the allocation. Upstream chose a re-read for gh-153570 (`size = Py_SIZE(self);` after the converter) and a clamp reorder for gh-153578. `bytearray_setitem_lock_held:699-706` is the in-file model.

Anyone applying `ob_exports++` mechanically to the size cases would produce a patch that looks right and fixes nothing. That is worth saying in the slice's write-up.

### `bytes_methods.c` — the shared file (task e)

Weighted at 2×, read cold, **clean**, and the reason is entirely in its callers.

Every entry point takes `(const char *str, Py_ssize_t len)` — a snapshot the caller computed — and then runs user Python on its *argument*: `parse_args_finds_byte:418` (`PyNumber_AsSsize_t` → `__index__`), `find_internal:469` / `_Py_bytes_count:578` / `_Py_bytes_contains:609` / `tailmatch:642` (`PyObject_GetBuffer` → `__buffer__`), and `PyBuffer_Release` (→ `__release_buffer__`) at `:507`, `:595`, `:613`, `:663`, `:668`.

- **bytes callers** pass `PyBytes_AS_STRING(self)`: `bytes` is never resized after publication, so the pointer cannot dangle. Real negative.
- **bytearray callers**: all nine `_Py_bytes_*` call sites that can reach user code go through a pin — seven via `_bytearray_with_buffer`, `contains` hand-rolled at `:1381`. Verified by `grep -n "_Py_bytes_[a-z_]*(" Objects/bytearrayobject.c`: the only two unpinned callers are `_Py_bytes_repr:1118` and `_Py_bytes_maketrans:1751`, and neither can run user Python (`_Py_bytes_repr` takes a raw buffer and only calls the unicode writer; `_Py_bytes_maketrans` takes two `Py_buffer`s the clinic wrapper already filled and never touches `self`).
- `_Py_bytes_tailmatch:685-695` holds a borrowed `PyTuple_GET_ITEM(subobj, i)` across `tailmatch()`, which runs the item's `__buffer__`. Not a finding: the tuple is immutable, `PyTuple_GET_SIZE` is re-read each iteration, and `subobj` is held by the caller's frame — the briefing's "borrowed ref under a known-live owner" class, and the owner really is provable here.

**The invariant to protect:** any *new* bytearray caller of a `_Py_bytes_*` function that does not bump `ob_exports` is a bug by construction, and it will be a two-type bug the moment `bytes_methods.c` grows a mutable-side assumption. I second the mapper on this.

### Mapper open questions closed

| mapper row | verdict | mechanism |
|---|---|---|
| **S9** `bytes_translate_impl:2358/2362` | **real negative** | `PyBuffer_Release(&table_view)` at `:2358` runs *after* `table_chars` is fully copied into the local `trans_table[256]` (`:2350-2355`); `PyBuffer_Release(&del_table_view)` at `:2361` runs after `del_table_chars` is consumed at `:2359-2360`. `:2364+` touches only `trans_table`, `input` (a `bytes`, immutable) and `output` (a private `result`). |
| **S11** `PyBytes_Concat:3285→3290→3302` | **real negative — but the guard is in the callee** | `_PyBytes_Resize:3370` **re-tests** `_PyObject_IsUniquelyReferenced(v)` and takes a copy path if the premise no longer holds. A `__buffer__` that grabs a reference between `:3285` and `:3302` is therefore handled. This is exactly the briefing's "guard implemented in a callee" FP class; a scanner cannot see it. |
| **A19** `bytearray_translate_impl:1670` | **real negative** | ordering, above |
| **A20** `bytearray_take_bytes_impl:1554` | **REAL BUG** | reproduced, gh-153570 |
| **A21** `bytearray_extend_impl:2186` | **REAL BUG** | reproduced, gh-153578 |

One negative worth recording because it looked promising and is not: `_PyBytes_Resize` can never be handed a *shared* `ob_bytes_object`. Every assignment to that field (`:180`, `:282`, `:971`, `:1616`) stores a freshly-created or constant bytes, and `_PyBytes_Resize:3370` copies rather than reallocs if the object is shared anyway. No route to a dangling `ob_bytes`.

---

## 3. The typed denominator (task a)

I re-ran `scan_refcounts._borrowed_slot_loads()` over exactly the four files (`scratchpad/enumerate_slot_loads.py`) and reproduced the headline **37** exactly, then typed every site from the declaration.

| type | count | sites |
|---|---|---|
| `PyObject **` array base (`ob_item`) — **not a reference at all** | **22** | `listobject.c:152, 216, 502, 710, 785, 786, 791, 835, 845, 875, 997, 1016, 1022, 1103, 1603, 2969, 3186, 3283, 3303, 3640, 3641, 3868` |
| `const char *` out of a **`Py_buffer`** (not from `self`) | 4 | `bytesobject.c:2456, 2494`; `bytearrayobject.c:1460, 1493` |
| `Py_ssize_t` scalar | 3 | `listobject.c:2350, 2488` (`min_gallop`), `:2970` (`saved_allocated`) |
| `sortslice` struct copied **by value** | 2 | `listobject.c:2341, 2475` (`ssa = ms->a`, `baseb = ms->a`) |
| `PyObject *` from a **private C struct**, not a Python object | 1 | `bytesobject.c:3768` `result = writer->obj` (`PyBytesWriter`) |
| **genuine `PyObject *` borrowed from a Python object's slot** | **5** | `listobject.c:4079`, `listobject.c:4229`, `bytesobject.c:3441`, `bytearrayobject.c:2983` (all `seq = it->it_seq`); `bytearrayobject.c:1615` (`result = self->ob_bytes_object`) |

**Quote this: the borrowed-`PyObject*` population in this slice is 5, not 37.** 22 of the 37 — 59% — are `PyObject **` array bases, which is the *pointer-invalidation* class, not the refcount class; the rules cannot say anything about them by construction. The pattern matches mod-io exactly (26 → 5) and for the same reason: `_SLOT_LOAD_RE` is type-agnostic, so a file whose dominant idiom is `items = self->ob_item` inflates the denominator by an order of magnitude.

**Audit of all 5** (task c):

| site | verdict |
|---|---|
| `listiter_next:4079` | **safe.** Loaded inside the exhaustion branch, after `list_get_item_ref` already returned NULL. `list_get_item_ref` runs no Python in either `#ifdef` arm (`Py_NewRef(PyList_GET_ITEM(…))` / `_Py_NewRefWithLock` under a critical section). `Py_DECREF(seq)` at `:4081` is the last statement; a re-entrant `next()` from a released item's `__del__` finds `it_seq == NULL` and `it_index == -1`. |
| `listreviter_next:4229` | **safe**, identical reasoning. |
| `striter_next:3441` | **safe against re-entrancy** — `_PyLong_FromUnsignedChar` runs no Python — **and it is the FIX above** under concurrency. |
| `bytearrayiter_next:2983` | **safe.** Data read under `Py_BEGIN_CRITICAL_SECTION(seq)`; drop is `Py_CLEAR` inside `#ifndef Py_GIL_DISABLED`. |
| `bytearray_take_bytes_impl:1615` | **safe as a borrow.** `result = self->ob_bytes_object` at `:1615` and `self->ob_bytes_object = remaining` at `:1616` are adjacent; nothing runs between. The function's bug is at `:1548`, not here. |

Three of the four iterators open-code `it->it_seq = NULL; Py_DECREF(seq);` where `bytearrayiter_next` uses `Py_CLEAR` — the CPY-0003 spelling. Benign in all three, because no Python-reaching call sits between the load and the drop. Recording it so a later reader does not re-derive it.

**Also confirmed in one line, per instruction:** `list.sort` detaches (`list_sort_impl:2971-2973` sets `ob_size=0, ob_item=NULL, allocated=-1` before any user code); `list.index`, `list.count` and `in` use `list_get_item_ref`, which returns a strong reference; `list.remove` reads `self->ob_item[i]` raw at `:3410` but `Py_INCREF`s at `:3411`. Not re-litigated.

---

## 4. `list_remove_impl` vs its three siblings (task d) — **defensible difference, ACCEPTABLE**

The history agent (FP-5) framed `list_remove_impl:3410-3412`'s raw `self->ob_item[i]` + `Py_INCREF` as "a free-threading retrofit that reached three of four call sites". I checked it three ways and **the framing does not survive**.

**1. The four sites are not comparable, because only one of them holds a lock.** From `Objects/clinic/listobject.c.h`:

| operation | clinic critical section | accessor |
|---|---|---|
| `list.index` (`:318-346`) | **none** | `list_get_item_ref` |
| `list.count` (`:358-368`) | **none** | `list_get_item_ref` |
| `list.__contains__` (`sq_contains`, not clinic) | none | `list_get_item_ref` |
| `list.remove` (`:383-394`) | **`Py_BEGIN_CRITICAL_SECTION(self)` at `:391`** | raw `ob_item[i]` + `Py_INCREF` |

The three that use the FT-safe accessor are exactly the three that run unlocked. `list_get_item_ref`'s `Py_GIL_DISABLED` body (`listobject.c:354-377`) exists to make an *unsynchronised* read safe — atomic `ob_item` load, `list_capacity` bound check, `_Py_TryXGetRef` with a fallback to the locked `list_item_impl`. Under a held per-object lock all of that is redundant.

**2. The one hazard I could construct does not materialise.** `Python/pystate.c:2323` calls `_PyCriticalSection_SuspendAll` on detach, so a user `__eq__` that sleeps or triggers a stop-the-world *does* drop the lock mid-loop. But `_PyCriticalSection_Resume` (`pystate.c:2304`) re-acquires it before the C code continues, so the plain loads at `:3409`/`:3410` always execute under the lock — and every writer takes the same lock, including the specialising interpreter: `Python/bytecodes.c:1424` `_STORE_SUBSCR_LIST_INT` opens with `DEOPT_IF(!LOCK_OBJECT(list))` and uses `FT_ATOMIC_STORE_PTR_RELEASE` inside it.

**3. Measured.** `repro/list_remove_ft_plain_read.py`, 3 remover + 3 resizer threads for 6 s, `release-ft-nojit-tsan`, `PYTHON_GIL=0`, `__eq__` calling `time.sleep(0)` to force the detach window: **0 ThreadSanitizer warnings**, process exited normally (rc=0), log complete — I checked the process had terminated before reading it, per the briefing's partial-log trap.

**Residual, and it is a correctness wart not a memory error:** during a detach window another thread can shrink the list, so `i` may be out of range when `list_ass_slice_lock_held(self, i, i+1, NULL)` is called at `:3415`. That function clamps `ilow`/`ihigh` to `Py_SIZE(a)` at `:979-987`, so the worst case is removing the wrong element or none — identical to the accepted single-threaded mutate-during-`remove` semantics.

**Falsifier for my verdict:** a writer to `self->ob_item` or `Py_SIZE(self)` that does not take `self`'s critical section. I checked `list_resize`, `list_ass_slice_lock_held`, `app1`, and the four specialised list uops in `bytecodes.c`; all lock. If `ft-race-scanner` finds one, this flips.

*(One asymmetry that is real but cosmetic: `list_remove_impl:3409` uses `Py_SIZE(self)` where the siblings' accessor uses `PyList_GET_SIZE`, which is `_Py_atomic_load_ssize_relaxed` under `Py_GIL_DISABLED`. Under the lock the relaxed load and the plain load are equivalent; switching would cost nothing and would silence a future scanner.)*

---

## 5. Classes bounded

| class | denominator | verdict |
|---|---|---|
| Borrowed `PyObject*` from a Python object's slot, used across a Python-reaching call | **5 typed sites** (of a 37-site headline) | **clean for re-entrancy, 5/5** — reasoning per site in §3 |
| Borrowed `PyObject**` array base held across a Python-reaching call | 22 sites | **clean** — `list_sort_impl` detaches; every other site is `ORDERED` (write completes before the first `Py_DECREF`) per the mapper's L11–L20, which I spot-checked at `:875-887`, `:997-1030`, `:3868-3878` |
| Raw `char*` from `self` across a Python-reaching call | **51 `AS_STRING`/`ob_start` sites; 23 functions** in `bytearrayobject.c` | **1 broken** (`strip`, CPY-0180); 7 pinned; 15 correct by ordering or non-Python-reaching |
| Cached `Py_ssize_t` size from `self` across a Python-reaching call | same 23 functions | **2 broken** (`take_bytes` gh-153570, `setslice`/`extend` gh-153578); the rest re-read |
| Raw `char*` from `self` in `bytesobject.c` | 15 sweep rows | **clean by immutability** — `bytes` is never resized after publication; `do_xstrip:2101` is the exact shape that is a UAF on the bytearray side and is safe only for that reason |
| `bytes_methods.c` shared entry points | 9 user-code-reaching functions, ×2 types | **clean, given its callers** — all 9 bytearray routes pinned |
| Iterator owning-reference drop under concurrency | **4** `tp_iternext` slots in the slice | **1 broken** (`striter_next`), 3 guarded |
| `PyModule_AddObject`-style steal/drop, stolen-ref misuse, new-reference leak | `scan_refcounts` summary all-zero across 345 functions | **clean** — no `PyModule_*`, no `Py_BuildValue` `N`, no `SET_ITEM` on a published container in these four files |

**Deliberately not re-litigated:** `list_sort`/`index`/`count`/`in` (Phase 1 SAFE, confirmed by reading in §3); CPY-0014 (`PyList_New`, `scan_uninit_dealloc`'s hit — owned by that agent); the `PyErr_Clear` sites at `bytearrayobject.c:1171/1177` and `bytes_methods.c:608` (pyerr-clear-auditor); `bytearray___init___impl`'s gh-153419 (error-path-analyzer).

---

## 6. Toolkit assessment

### 6.1 [CRITICAL] Nine of thirteen scanners see 67 of 102 functions in `Objects/bytesobject.c`

`tree_sitter_utils.extract_functions` **stops at line 2779** in `bytesobject.c`. The last function it returns is `bytes_getnewargs` (`start_line 2779`); the file is 3,926 lines. **1,083 lines and 35 functions are invisible.**

**Root cause, exact.** The `bytes_methods[]` `PyMethodDef` table at `:2787-2843` interleaves brace initialisers with bare Argument-Clinic macro names that expand to `{…},` *including the trailing comma*:
```c
    {"__getnewargs__", bytes_getnewargs,  METH_NOARGS, ...},
    BYTES_LSTRIP_METHODDEF
    BYTES_MAKETRANS_METHODDEF
    BYTES_PARTITION_METHODDEF
```
tree-sitter-c cannot parse two adjacent identifiers with no separator inside an initialiser list, so it emits an ERROR cascade from `:2787` and the recovery swallows the rest of the translation unit. Verified by walking the parse tree: `ERROR at line 2787-2820`, then eight more through `:2844`.

**Blast radius, measured** — `functions_analyzed` on `Objects/bytesobject.c` alone:

```
scan_ft_races 67   scan_lock_discipline 67   scan_stw_safety 67   scan_recursion_guards 67
scan_pyerr_clear 67  scan_init_bypass 67   scan_uninit_dealloc 67  scan_memory_patterns 67
scan_gil_usage 67                          (scan_refcounts: 102 — it has its own regex find_functions)
```

Everything after `:2843` is unreviewed by all nine: `striter_next` (**the reproduced FT crash in this report**), `striter_len`, `striter_reduce`, `bytes_iter`, `_PyBytes_FromList`, `_PyBytes_FromTuple`, `_PyBytes_FromIterator`, `_PyBytes_FromHex`'s callers, `PyBytes_Concat`, `PyBytes_ConcatAndDel`, `_PyBytes_Resize`, and the entire 12-function `PyBytesWriter_*` public API.

The visible symptom in the slice envelope is a **structural zero disguised as a real one**: `scan_ft_races.sample.json` reports `iternext_functions: 3` for a slice containing **four** registered `tp_iternext` slots (`listobject.c:4022`, `:4176`, `bytesobject.c:3541`, `bytearrayobject.c:3104`). The scanner's own `_collect_iternext_names()` finds `striter_next` correctly from the positional slot comment — it is the *function* discovery that drops it, so `_is_iternext` is never called on it. Run on `bytesobject.c` alone, `iternext_functions` is **0**.

**Proposal (do not implement from here — this is the sibling-family fix):**
1. Make `extract_functions` **error-tolerant at the top level**: when a top-level child is an `ERROR` node, skip it and keep walking siblings rather than letting recovery consume the remainder. Today one bad initialiser truncates a file.
2. Add a **canary to every envelope**: `functions_analyzed` next to a cheap independent count (`grep -c '^[A-Za-z_].*(' `, or the regex `find_functions` `scan_refcounts` already ships). A >20% disagreement should surface as a `notes[]` entry — *"tree-sitter parsed N of M functions in `<file>`; results for that file are partial."* This is precisely the "check the denominator" discipline the run context demands, applied to the scanner's own input.
3. Regression fixture: `Objects/bytesobject.c:2787-2843` verbatim. This is the **second** instance of this family in the toolkit line (`extract_functions` skipping `preproc_ifdef`, fixed as cext #64 / ft #24) — a fixture corpus of real CPython constructs that break tree-sitter-c would have caught both.

### 6.2 `scan_refcounts` — precision N/A, denominator overstated 7×

Zero findings, so no precision to report. The denominator is the problem: `borrowed_slot_load_sites: 37` reads as strong coverage and the real borrowed-`PyObject*` population is **5**. A reader who trusts the headline concludes "37 borrows, all clean"; the honest statement is "5 borrows, all clean, plus 32 sites the rules structurally cannot speak about".

**Proposal.** Emit the site list whenever the denominator is non-zero, even at zero findings — and type it:
```json
"borrowed_slot_load_sites": 37,
"borrowed_slot_loads": [
  {"file": "Objects/listobject.c", "line": 4079, "local": "seq", "owner": "it",
   "field": "it_seq", "decl": "PyListObject *", "class": "pyobject_ptr"},
  {"file": "Objects/listobject.c", "line": 152, "local": "old_items", "owner": "self",
   "field": "ob_item", "decl": "PyObject **", "class": "array_base"}
]
```
with `class ∈ {pyobject_ptr, array_base, scalar, struct_value, buffer_derived, private_struct}` from the declaration text `_SLOT_LOAD_RE` already captures in its optional type group. Then `denominators.borrowed_slot_load_sites_typed` gives the number a reader should quote. Cost: the regex already matches the type prefix; only the group needs keeping. This is the mapper's proposal too; I am seconding it with the measured 22/4/3/2/1/5 split as the evidence.

### 6.3 The missing rule — `raw_self_state_across_python_call`

The three real bugs in this slice all have the same shape and **none of them is expressible in `scan_refcounts`'s vocabulary**, because the carried value is not a `PyObject*`:

> a local of type `char*` / `const char*` / `Py_ssize_t` / `size_t` is assigned from a **receiver-derived** expression, a Python-reaching call intervenes, and the local is used afterwards.

Naively this fires on 56 functions across four files — unusable. With four suppressors and one promoter it fires on exactly three, and they are the three bugs:

| gate | kind | effect on this slice |
|---|---|---|
| **G1** Intervening call is allocation-only (`PyBytes_FromStringAndSize`, `PyByteArray_FromStringAndSize`, `PyUnicode_*`, `PyList_New`, `PyTuple_New`) → **suppress**. Rationale in the envelope: `_Py_ScheduleGC` (`Python/gc.c:1965-1971`) only sets an eval-breaker bit; GC runs at the next bytecode boundary. `Py_DECREF` is **not** covered — `tp_dealloc` runs `__del__` synchronously. | suppressor | −14 rows |
| **G2** Intervening call is `PyErr_Format`/`PyErr_SetString` whose format literal contains no `%R`/`%S`/`%A`/`%T`/`%V` → suppress | suppressor | −3 rows |
| **G3** Both the cache and the use lie inside an `ob_exports++ … ob_exports--` bracket (or the function is `_bytearray_with_buffer`) → suppress | suppressor | −7 functions |
| **G4** Receiver is an immutable type (`PyBytesObject*`, `PyTupleObject*`) **and** the carried value is a data pointer → suppress; a carried *size* still counts | suppressor | −15 rows (all of `bytesobject.c`) |
| **G5** Intervening call is `PyBuffer_Release`, `PyObject_GetBuffer`, `PyNumber_AsSsize_t`, `_PyNumber_Index`, or `_getbytevalue` → **promote to high confidence** | promoter | the 3 true positives |

Expected on this slice: `bytearray_strip_impl_helper:2375`, `bytearray_take_bytes_impl:1548`, `bytearray_setslice:673` — **3/3 precision, 3/3 recall** against the slice's known instances. The rule generalises directly to the `_struct` `s_codes` family (CPY-0048 / CPY-0050) and to CPY-0044/0049, which are the same shape with a `PyMem_Malloc` buffer instead of an object-owned one — a family the FP taxonomy already documents as unreachable by any current rule. My sweep script is at `scratchpad/sweep_cached_raw.py`; it is 90 lines and already produces the pre-gate candidate set.

**G1 is worth landing on its own, for every scanner.** "Allocation cannot synchronously run user Python in CPython ≥3.12" is a fact that suppresses candidates in `scan_error_paths`, `scan_pyerr_clear`, `scan_ft_races` and `scan_memory_patterns` too, and it is currently rediscovered by hand every slice. It belongs in the FP taxonomy with the `Python/gc.c:1965` citation.

### 6.4 `scan_ft_races` — a second recall gap independent of 6.1

Even given a correct function list, T3 as written would need one extension to catch `striter_next`. `_MEMBER_SET_NULL_RE` + `_DECREF_RE` do match `it->it_seq = NULL;` / `Py_DECREF(seq);`, so the rule *should* fire once discovery is fixed — worth a regression test asserting it does. Beyond that: the twins here (`#ifndef Py_GIL_DISABLED` around the drop) are already modelled as the `gil_only` suppressor, which is the right design and is what makes `listiter_next`/`listreviter_next`/`bytearrayiter_next` correctly silent. So the fix in 6.1 plus a fixture should close this without new rule logic.

### 6.5 What worked

The informed-mode discipline paid twice. The guarded-twin rule found the `striter_next` bug directly — I was not looking for an FT defect; I was typing the 37 slot loads for task (a), noticed that three of the four `it_seq` drops carried an `#ifndef Py_GIL_DISABLED` and the fourth did not, and the crash followed in one repro. And Phase 1's correction of §2 saved the whole group from hunting a borrowed-`ob_item` UAF in `listobject.c` that provably is not there.

---

## 7. Artifacts

- `reports/obj-sequences/repro/bytes_iterator_ft_double_decref.py` — the FIX; 6/6 debug-FT crashes, 7/8 release-FT hangs, GIL and single-thread controls
- `reports/obj-sequences/repro/bytearray_take_bytes_index_uaf.py` — gh-153570, 3 scenarios incl. a control
- `reports/obj-sequences/repro/bytearray_extend_setslice_oob.py` — gh-153578, still open
- `reports/obj-sequences/repro/list_remove_ft_plain_read.py` — the negative for task (d)
- `scratchpad/enumerate_slot_loads.py` — reproduces the 37 with exact lines and types
- `scratchpad/sweep_cached_raw.py` — the 56-function cached-raw-state sweep
- `scratchpad/check_extract.py`, `scratchpad/find_parse_boundary.py` — the 6.1 evidence

## 8. What I did not do

- **No TSan run for the `striter_next` race.** `yama/ptrace_scope` blocks gdb attach on this host and the debug-FT evidence is already a hard crash with a frame; a TSan report would add the write/write pair but change nothing.
- **The release-FT hang is unexplained.** 7/8 reproducible with a clean 1-thread control, but I did not capture a stack from a hung process. Mechanism stated as a hypothesis only.
- **I did not file anything upstream.** gh-153570 and gh-153578 exist; CPY-0180's `strip` variant and the `striter_next` race have no issue I could find, and filing is the campaign's call, not mine.
- **`bytesobject.c`'s `_PyBytes_FormatEx`** (480 lines, CC 159, rank 1 in all of `Objects/`) got only the sweep and the mapper's S1–S4 rows, not a line-by-line read. Its carried state is a private `PyBytesWriter` and a pointer into an immutable `bytes` format string, so nothing in my class lands there — but it is the file's repeat offender (gh-142557 UAF, gh-140939 leak) and it is in the 1,083 lines nine scanners cannot see. It deserves a dedicated pass once 6.1 is fixed.
