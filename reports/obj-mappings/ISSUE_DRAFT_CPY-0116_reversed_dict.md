# Issue draft — CPY-0116

**Title:** `reversed(dict)` can read out of bounds and segfault (`dictreviter_iter_lock_held`)

**Labels:** `type-crash`, `interpreter-core`
**Versions:** reproduced on **3.14.4**, **3.14.6** and **`main` (3.16.0a0)**, release and debug
builds alike. The 3.12 branch carries the identical shape by inspection — combined-table
seed `dk_nentries - 1` and `if (i < 0)` as the only bound — so I expect 3.12 and 3.13 are
affected too, but I have only run the three above.

---

`reversed()` on a dict can dereference an entry index that is far past the end of the
dict's current keys table, from ordinary pure-Python code. On a release build this
segfaults.

## Reproducer

No C API, no `_testcapi`, no threads:

```python
d = {}
for i in range(1000):
    d["k%d" % i] = i          # combined table, dk_nentries == 1000
for i in range(1, 1000):
    del d["k%d" % i]          # ma_used == 1, dk_nentries still 1000

it = reversed(d)              # di_pos = dk_nentries - 1 = 999

d.clear()                     # fresh PyDict_MINSIZE keys object
d["k0"] = 0                   # ma_used == 1 again

for k in it:                  # reads DK_UNICODE_ENTRIES(k)[999] on a 5-slot table
    pass
```

```
$ python3 reversed_oob.py
Segmentation fault (core dumped)

$ python3 -VV
Python 3.14.4 (main, Jun 18 2026, 14:25:02) [GCC 15.2.0]
```

Under ASan:

```
ERROR: AddressSanitizer: heap-use-after-free
READ of size 8 at 0x6d5563df4aa0 thread T0
    #0 dictreviter_iter_lock_held  Objects/dictobject.c:6284:31
    #1 dictreviter_iternext        Objects/dictobject.c:6354:13
    #2 _PyForIter_VirtualIteratorNext  Python/ceval.c:3775:22
```

(The out-of-bounds address happens to land in a block freed by `d.clear()`, so ASan
labels it use-after-free; other variants of the same trigger report
`heap-buffer-overflow` instead. The defect is the unbounded index either way.)

All five reverse-iterator entry points are affected (`reversed(d)`, `reversed(d.keys())`,
`reversed(d.values())`, `reversed(d.items())`, and `dict.__reversed__`).

## Mechanism

`dictiter_new()` seeds `di_pos` differently per table kind
([`Objects/dictobject.c:5632-5637`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L5632-L5637)):

```c
if (_PyDict_HasSplitTable(dict)) {
    di->di_pos = used - 1;                        /* ma_used - 1 */
}
else {
    di->di_pos = load_keys_nentries(dict) - 1;    /* dk_nentries - 1 */
}
```

For a combined table `di_pos` is therefore bounded by `dk_nentries`, which can be far
larger than `ma_used` after deletions.

`dictreviter_iter_lock_held()` then has exactly one staleness check and one bound
([`:6261`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L6261) and
[`:6271`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L6271)):

```c
if (di->di_used != d->ma_used) {          /* :6261 — compares ma_used only */
    PyErr_SetString(PyExc_RuntimeError, "dictionary changed size during iteration");
    ...
}

Py_ssize_t i = di->di_pos;
PyDictKeysObject *k = d->ma_keys;

if (i < 0) {                              /* :6271 — the ONLY bound */
    goto fail;
}
...
    PyDictUnicodeEntry *entry_ptr = &DK_UNICODE_ENTRIES(k)[i];   /* :6283 */
    while (entry_ptr->me_value == NULL) {                        /* :6284 — OOB read */
```

`ma_used` says nothing about `dk_nentries`, so the guard cannot see a keys object that
was replaced by a *smaller* one while the element count stayed the same. `d.clear()`
followed by a single insertion does exactly that: `di_used == ma_used == 1` passes, and
`di_pos` is still 999.

The split-table branch has no explicit bound either — `get_index_from_order()` guards
only with `assert(i < mp->ma_values->size)`, which compiles out under `NDEBUG`.

## The forward iterators already have the check

All three forward iterators bound `i` against the *current* table before dereferencing —
e.g. `dictiter_iternextkey_lock_held`
([`:5732`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L5732) and
[`:5740-5747`](https://github.com/python/cpython/blob/main/Objects/dictobject.c#L5740-L5747)):

```c
if (_PyDict_HasSplitTable(d)) {
    if (i >= d->ma_used)
        goto fail;
    ...
}
else {
    Py_ssize_t n = k->dk_nentries;
    ...
    if (i >= n)
        goto fail;
}
```

They can afford a weaker seed because `di_pos` starts at 0 and only grows. The reverse
iterator starts at the far end, so it is precisely the one that needs the bound — and it
is the only one without it.

## Where the check went

This is not an oversight; the bound was written, reviewed, and removed.

PR [GH-16846](https://github.com/python/cpython/pull/16846) (bpo-38525, 2019) fixed a
different reverse-iterator crash. Its final commit,
[`bf61754`](https://github.com/python/cpython/pull/16846/commits/bf61754fdf62), does one
thing:

```diff
     if (d->ma_values) {
-        if (i < 0 || i >= d->ma_used) {
+        if (i < 0) {
             goto fail;
         }
```

in response to a review exchange on `Objects/dictobject.c`:

> **serhiy-storchaka:** Is this change still needed?
> **corona10:** No, it is not needed. I removed it on the latest commit.

The removal was reasonable **for the branch it was on**: the same PR had just changed the
split-table seed to `ma_used - 1`, which makes `i >= ma_used` redundant there given the
`di_used != ma_used` check. It never applied to the combined branch, which is seeded from
`dk_nentries` and is where this segfaults.

## Suggested fix

Give the reverse iterator the same two-branch bound its forward twins have — for the
combined branch, load `n = k->dk_nentries` and reject `i >= n` before forming the entry
pointer; for the split branch, reject `i >= d->ma_used` before calling
`get_index_from_order`.

## Scope

I checked the other reverse iterators in the tree, and dict's is the only one that indexes
a raw C array directly. The others delegate the bound to a checked accessor:

- `reversed_next` (`Objects/enumobject.c:440`, the builtin `reversed()`) goes through
  `PySequence_GetItem`, which bounds-checks and raises `IndexError`.
- `listreviter_next` (`Objects/listobject.c:4220`) goes through `list_get_item_ref`, which
  tests `valid_index(i, size)` and again against the allocated capacity, returning `NULL`.
- `dequereviter_next` (`Modules/_collectionsmodule.c`) walks a block list, not an indexed
  array, under a critical section.

So this looks like a single-site bug rather than a class — dict's reverse iterator forms
`&DK_UNICODE_ENTRIES(k)[i]` itself, with nothing between `i` and the dereference.

I have reproducers for the split-table variant and the other four entry points if they'd
be useful.
