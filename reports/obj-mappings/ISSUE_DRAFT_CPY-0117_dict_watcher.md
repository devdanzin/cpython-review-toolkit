# Issue draft — CPY-0117

**Title:** A dict watcher that follows the documented error protocol makes CPython re-enter Python mid-update

**Labels:** `type-crash`, `interpreter-core`

**Versions:** reproduced on `main` (3.16.0a0), release and debug, GIL and free-threaded. The mechanism (`_PyDict_SendEvent` → `PyErr_FormatUnraisable`) has been present since watchers landed in 3.12.

---

`Doc/c-api/dict.rst` tells a `PyDict_WatchCallback` two things:

> The callback may inspect but must not modify *dict*; doing so could have unpredictable effects, including infinite recursion. **Do not trigger Python code execution in the callback**, as it could modify the dict as a side effect.
>
> — [`dict.rst:582-584`](https://github.com/python/cpython/blob/main/Doc/c-api/dict.rst#L582-L584)

> **If the callback sets an exception, it must return `-1`**; this exception will be printed as an unraisable exception using `PyErr_WriteUnraisable`.
>
> — [`dict.rst:595-596`](https://github.com/python/cpython/blob/main/Doc/c-api/dict.rst#L595-L596)

A callback that does exactly the second thing causes CPython to do exactly the first thing on its behalf. `_PyDict_SendEvent` ([`Objects/dictobject.c:8309-8317`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L8309-L8317)):

```c
if (cb && (cb(event, (PyObject*)mp, key, value) < 0)) {
    PyErr_FormatUnraisable(
        "Exception ignored in %s watcher callback for <dict at %p>",
        dict_event_name(event), mp);
}
```

`PyErr_FormatUnraisable` runs `sys.unraisablehook`, which is settable from pure Python. So the "do not trigger Python code execution" requirement is discharged by the callback and then violated by the runtime — inside the notify window, while the dict is mid-update.

The docs do say the exception becomes an unraisable. What they don't say is that this happens *synchronously, inside the operation*, at a point where the calling code has already captured an entry index, an entry pointer, a bound, or a borrowed reference that it will use afterwards.

## Thirteen of the fourteen notify sites hold stale state across that window

I drove all fourteen. Thirteen hold state across the notify and all thirteen produce an observable failure; twelve of those are memory corruption or a memory-invariant assertion. One site is safe.

| site | enclosing function | stale value | worst observed |
|---|---|---|---|
| [`:1917`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L1917) | `insert_combined_dict` | `dk_usable` bound | ASan **heap-buffer-overflow WRITE**, "0 bytes after" a 1520-byte region; **release: silent corruption, exit 0** |
| [`:1997`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L1997) | `_PyDict_InsertSplitValue` | `ix`, split-table-ness | SIGSEGV at `:1998`, `ma_values == NULL` — 5/5 on each of four builds |
| [`:2003`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L2003) | `_PyDict_InsertSplitValue` | `ix`, `old_value` | SIGSEGV at `:2004` 5/5 on four builds; embedded variant asserts in `clear_freelist` |
| [`:2060`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L2060) | `insertdict` | `ix`, `old_value` | ASan heap-UAF READ at `:2076`; heap-buffer-overflow **WRITE** at `:2068`; `_Py_NegativeRefcount` 5/5 |
| [`:2103`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L2103) | `insert_to_emptydict` | unpublished `newkeys` | `ma_used` desync puts a NULL in a `list` → SIGSEGV in `list_sort_impl` |
| [`:3038`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L3038) | `_PyDict_DelItem_KnownHash_LockHeld` | `ix` | debug `assert(hashpos >= 0)`; release `Py_DECREF(NULL)` |
| [`:3083`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L3083) | `delitemif_lock_held` | `ix`, `old_value`, `hash` | ASan SEGV `Py_DECREF(NULL)`; `len()` → NULL 5/5 release |
| [`:3142`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L3142) | `clear_lock_held` | keys pointer | ASan use-after-free at `dictkeys_decref:496` |
| [`:3307`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L3307) | `_PyDict_Pop_KnownHash` | `ix`, `old_value` | ASan heap-UAF READ at `:3308`; SIGSEGV 5/5 release |
| [`:4234`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L4234) | `dict_dict_merge` | the whole guard set | debug assert in `clone_combined_dict_keys`; **silent data loss** 5/5 — no memory corruption reached |
| [`:5051`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L5051) | `dict_popitem_impl` (unicode) | `ep0` | ASan UAF R+W; **`popitem()` returns a tuple whose `[1]` is a raw C NULL** |
| [`:5066`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L5066) | `dict_popitem_impl` (general) | `ep0`, `i` | ASan heap-UAF READ **at the site**, `:5067` |
| [`:7510`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L7510) | `store_instance_attr_lock_held` | `values`, `dict`, `ix`, `old_value` | `assert(i < size)`; ASan overflow WRITE + UAF READ; **`len()`, `list()` and `vars()` all raise** |
| [`:3652`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L3652) | `dict_dealloc` | *(none)* | **safe** — reads `ma_values`/`ma_keys` at `:3656-3657`, after the notify |

Also reached from these paths: `delete_index_from_values:2943`, whose search loop is bounded only by `assert(i < size)` — debug SIGABRT, and on plain release it **silently loses a dict entry**.

## Four that are worth calling out individually

**`:1997`/`:2003` are in `_PyDict_InsertSplitValue`, which is `PyAPI_FUNC` and carries the comment `// Exported for external JIT support`.** The hook that kills it is `d.clear()` after `del obj` has detached the dict, which makes `clear_lock_held` take its `set_values(mp, NULL)` branch; the split-table store then dereferences NULL. gdb confirms `ma_values == 0x0`, `ma_keys == &empty_keys_struct`, `ix == 2`.

**`:3083` breaks a promise the file makes in a comment.** [`dictobject.c:3090-3094`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L3090-L3094) tells callers the predicate → deletion sequence is atomic *provided the predicate does not re-enter*. `is_dead_weakref` does not re-enter; the notify does. Reachable as `_weakref._remove_dead_weakref(dct, key)`.

**`:3307` hands freed memory back to Python.** `*result` at `:3312` is the dict's own reference, returned straight to the `dict.pop()` caller after the hook dropped it.

**`:7510` gives the sharpest Python-visible invariant break.** `delete_index_from_values` does `size--` on a size the hook zeroed, and `values->size` is a `uint8_t`, so it wraps to 255 while `:7527` drives `ma_used` to `-1`. After a plain `del o.attr`, on `release-gil-nojit`, 5/5:

```
len(d)   -> SystemError: <built-in function len> returned NULL without setting an exception
list(d)  -> ValueError: length must be positive
vars(o)  -> ValueError: length must be positive
```

## The one I could not corrupt

`:4234` (`dict_dict_merge`, `PyDict_EVENT_CLONED`) holds every guard established at `:4219` and `:4228-4232` across the notify, and those guards are genuinely unchecked afterwards — but I could not reach memory corruption in roughly nine hook variants (0/3 under ASan). The two guards whose falsification would give a type-confused dict are out of reach from Python: `mp` is a plain dict so a hook cannot make `mp->ma_values` non-NULL, and a combined `other` cannot be turned back into a split table. What it does give is a 5/5 debug assertion violation in `clone_combined_dict_keys` and **5/5 silent data loss on all four builds** — `d.update(src)` discards everything the hook wrote to `d`, with no error. On release, that path also leaves `d` owning a *heap* keys object carrying `_Py_DICT_IMMORTAL_INITIAL_REFCNT` (memcpy'd from `empty_keys_struct`), which can never be freed and permanently falsifies `clear_lock_held`'s `assert(oldkeys->dk_refcnt == 1)` at `:3148`.

## Reproducer

The callback is CPython's own — `dict_watch_callback_error` in [`Modules/_testcapi/watchers.c:90-98`](https://github.com/python/cpython/blob/main/Modules/_testcapi/watchers.c#L90-L98), in its entirety:

```c
static int
dict_watch_callback_error(PyDict_WatchEvent event, PyObject *dict,
                          PyObject *key, PyObject *new_value)
{
    PyErr_SetString(PyExc_RuntimeError, "boom!");
    return -1;
}
```

That is the documented protocol and nothing else: it sets an exception, returns `-1`, and runs no Python. The re-entry comes entirely from CPython's response to it.

```python
import sys, _testcapi

d = {}
for i in range(200):
    d["k%d" % i] = i

fired = []
def hook(unraisable):
    if fired:
        return
    fired.append(1)
    d.clear()                       # re-enter the dict being mutated

sys.unraisablehook = hook
wid = _testcapi.add_dict_watcher(1) # installs dict_watch_callback_error
_testcapi.watch_dict(wid, d)

del d["k100"]                       # notify -> -1 -> unraisable -> hook -> clear()

print("len now %d" % len(d))
```

```
# debug build -- exit 134 (SIGABRT)
python: Objects/dictobject.c:2963: void delitem_common(...): Assertion `hashpos >= 0' failed.

# release build -- exit 1
SystemError: <built-in function len> returned NULL without setting an exception
```

One per-site reproducer for each of the thirteen is attached, along with negative controls — for `:2060`, hooks that store immortal small ints or force a resize do **not** crash it, so the claim is not "any hook corrupts anything". That site needs heap values specifically: with small ints the stale `Py_XDECREF(old_value)` at `:2076` is a no-op because they are immortal, which is why an earlier attempt at this site came back clean.

Registering a watcher requires the C API, so this is not reachable from pure Python alone — but `sys.unraisablehook` is pure Python, and any extension that installs a conforming watcher hands that reach to arbitrary Python code.

## The one safe site, and why

`dict_dealloc` ([`:3650-3658`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L3650-L3658)) is the only notify site that survives this. It brackets the notify in `_PyObject_ResurrectStart`/`End`, but that bracket defends against **resurrection** — the comment at `_PyDict_SendEvent:8309-8312` names that threat and only that threat. Its mutation-safety is incidental: it re-reads `ma_keys`/`ma_values` *after* the notify rather than before.

So the thing to propagate is the **ordering**, not the bracket.

## Notes toward a fix

Three directions, not mutually exclusive:

1. **Re-validate after the notify** at each of the thirteen sites — reload the index, entry pointer, or bound. Correct but repetitive, and easy to regress: this is thirteen independent chances to get it wrong again.
2. **Defer the unraisable** until the dict operation completes, so the callback's error is still reported but not from inside the window.
3. **Guard the re-entry centrally** in `format_unraisable_v` ([`Python/errors.c:1737`](https://github.com/python/cpython/blob/main/Python/errors.c#L1737)), which is the single choke point every unraisable site funnels through.

Given that thirteen of fourteen sites are affected, the central options look more attractive than the per-site one.

`Objects/typeobject.c:1219` already carries the observation, added in gh-127266:

```c
// Note that PyErr_FormatUnraisable is potentially re-entrant and the watcher
// callback might be too
```

`dictobject.c` has no equivalent, and its notify sites were never audited against it.

## Related

- CPython's own reference watcher, `dict_watch_callback` ([`watchers.c:31-70`](https://github.com/python/cpython/blob/main/Modules/_testcapi/watchers.c#L31-L70)), violates *both* halves of the documented contract: `PyUnicode_FromFormat("new:%S:%S", ...)` calls `PyObject_Str` (forbidden by `dict.rst:583-584`), and it calls `PyUnicode_FromFormat` + `PyList_Append` with no save/restore (required by `dict.rst:599-603`). If the example cannot follow the contract, it is worth asking whether the contract is stateable.
- `PyType_WatchCallback` ([`Doc/c-api/type.rst:138-155`](https://github.com/python/cpython/blob/main/Doc/c-api/type.rst#L138-L155)) documents **no error protocol at all**, yet `typeobject.c:1222` treats `< 0` as "exception set" and reports it the same way.
