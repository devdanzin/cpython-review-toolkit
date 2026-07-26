# Issue draft — CPY-0117

**Title:** A dict watcher that follows the documented error protocol makes CPython re-enter Python mid-update

**Labels:** `type-crash`, `interpreter-core`

**Versions:** reproduced on `main` (3.16.0a0), release and debug builds. The mechanism (`_PyDict_SendEvent` → `PyErr_FormatUnraisable`) has been present since watchers landed in 3.12.

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

The docs do say the exception becomes an unraisable. What they don't say is that this happens *synchronously, inside the operation*, at a point where the calling code has already captured an entry index or a keys pointer it will use afterwards.

## The notify sites that hold stale state across that window

| site | value captured before the notify | consequence |
|---|---|---|
| [`insert_combined_dict:1910`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L1910) | `dk_usable` bound | ASan **heap-buffer-overflow WRITE**, "0 bytes after" a 1520-byte region from `new_keys_object:860`; debug `assert(dk_usable >= 0)`; **release: silent corruption, exit 0** |
| [`dict_popitem_impl:5043`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L5043) | entry pointer `ep0` | ASan use-after-free R+W; release SIGSEGV; **`popitem()` returns a tuple whose second element is a raw C NULL** |
| [`_PyDict_DelItem_KnownHash_LockHeld:3038`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L3038) | index `ix` | debug `assert(hashpos >= 0)`; release `Py_DECREF(NULL)` (gdb-confirmed) |
| [`delete_index_from_values:2943`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L2943) | loop bound | debug SIGABRT; **plain release silently loses a dict entry**; ASan heap-buffer-overflow READ |
| [`clear_lock_held:3136`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L3136) | keys pointer | ASan use-after-free at `dictkeys_decref:496` |
| [`insert_to_emptydict:2103`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L2103) | unpublished `newkeys` | `ma_used` desync puts a NULL into a Python `list` → SIGSEGV in `list_sort_impl` |

<!-- PENDING: fold in the follow-up sweep of the remaining notify sites
     (:1997 :2003 :2060 :3083 :3307 :4234 :5066 :7510) once measured. -->

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

Registering a watcher requires the C API, so this is not reachable from pure Python alone — but `sys.unraisablehook` is pure Python, and any extension that installs a conforming watcher hands that reach to arbitrary Python code.

## The one safe site, and why

`dict_dealloc` ([`:3650-3658`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L3650-L3658)) is the only notify site that survives this. It brackets the notify in `_PyObject_ResurrectStart`/`End`, but that bracket defends against **resurrection** — the comment at `_PyDict_SendEvent:8309-8312` names that threat and only that threat. Its mutation-safety is incidental: it re-reads `ma_keys`/`ma_values` *after* the notify rather than before.

So the thing to propagate is the **ordering**, not the bracket.

## Notes toward a fix

Three directions, not mutually exclusive:

1. **Re-validate after the notify** at each affected site — reload the index, entry pointer, or bound. Correct but repetitive, and easy to regress.
2. **Defer the unraisable** until the dict operation completes, so the callback's error is still reported but not from inside the window.
3. **Guard the re-entry centrally** in `format_unraisable_v` ([`Python/errors.c:1737`](https://github.com/python/cpython/blob/main/Python/errors.c#L1737)), which is the single choke point every unraisable site funnels through.

`Objects/typeobject.c:1219` already carries the observation, added in gh-127266:

```c
// Note that PyErr_FormatUnraisable is potentially re-entrant and the watcher
// callback might be too
```

`dictobject.c` has no equivalent, and its notify sites were never audited against it.

## Related

- CPython's own reference watcher, `dict_watch_callback` ([`watchers.c:31-70`](https://github.com/python/cpython/blob/main/Modules/_testcapi/watchers.c#L31-L70)), violates *both* halves of the documented contract: `PyUnicode_FromFormat("new:%S:%S", ...)` calls `PyObject_Str` (forbidden by `dict.rst:583-584`), and it calls `PyUnicode_FromFormat` + `PyList_Append` with no save/restore (required by `dict.rst:599-603`). If the example can't follow the contract, it's worth asking whether the contract is stateable.
- `PyType_WatchCallback` ([`Doc/c-api/type.rst:138-155`](https://github.com/python/cpython/blob/main/Doc/c-api/type.rst#L138-L155)) documents **no error protocol at all**, yet `typeobject.c:1222` treats `< 0` as "exception set" and reports it the same way.
