# recursion-guard-auditor — obj-mappings slice

**Scope:** exactly `Objects/dictobject.c` (8,597 lines) and `Objects/setobject.c` (3,228 lines)
in `/home/danzin/projects/cpython` @ `4f3be1b5777`.

**Build provenance.** All builds are `~/projects/python_build_matrix/builds/<name>/python`,
compiled at `a1d580430c8` (3.16.0a0, Clang 21.1.8). `git diff a1d580430c8 4f3be1b5777 --
Objects/dictobject.c Objects/setobject.c` is **empty** — the two slice files are byte-identical
between the build commit and the target ref, so every line number below is valid for both, and
the ASan frame line numbers need no reconciliation. `ulimit -s` = 16384 kB on this host; the
depth numbers are only meaningful against that.

---

## 1. Denominators

| denominator | value | source |
|---|---|---|
| scanner findings (slice-scoped) | **25** | `scanners/scan_recursion_guards.sample.json` |
| — `missing_recursion_guard` | 25 | ↑ |
| — `recursion_descent_guarded_by_dispatcher` | **0** | ↑ |
| by shape: `hash_entry_point` / `slot_helper_descent` | **24 / 1** | ↑ |
| by confidence: low / high | 24 / 1 | ↑ |
| functions analysed | 404 (292 dict + 112 set) | ↑, reproduced independently |
| envelope `recursion_prone_slot_functions` | 17 | ↑ — **this number is wrong, see §4.1** |
| true tp_hash/parameter_walk denominator (measured) | **6** | `_slot_for` re-run by hand |
| hash-dispatch call sites read by hand | 32 | grep + read (§3.6, §5.3) |
| repr/str/richcompare descent sites read by hand | 15 | grep + read (§3.2) |
| `Py_ReprEnter` / `Py_ReprLeave` sites | 3 / 6 | grep |
| `Py_EnterRecursiveCall` sites in either file | **0** | grep |
| self-recursion candidates (mechanical) | 3 → 1 real | tree-sitter pass, §3.4 |
| lines read closely | ≈2,050 | — |
| reproducer runs executed | 69 subprocess runs across 4 builds | §5 |

**Net-new FIX from the scanner in this slice: 0.** The one high-confidence finding is CPY-0019,
already recorded and already upstream as gh-154318.

### Answering the ratio question: 25 of 61 from 2 files

I re-ran the scanner over all of `Objects/` to get the shape split rather than guess:

| shape | Objects/ (113 files) | this slice | slice share |
|---|---|---|---|
| `hash_entry_point` | 32 | **24** | **75%** |
| `self_recursion` | 11 | 0 | 0% |
| `guarded_dispatcher_descent` | 7 | 0 | 0% |
| `field_element_descent` | 6 | 0 | 0% |
| `mutual_recursion` | 2 | 0 | 0% |
| `slot_helper_descent` | 1 | **1** | 100% |
| `container_element_descent` | 1 | 0 | 0% |
| `temporary_container_descent` | 1 | 0 | 0% |
| **total** | **61** | **25** | 41% |

**Neither "noisy here" nor "recursion-dense here" — it is one shape, and that shape is
structural.** dict and set *are* the hash tables, so every public lookup/insert/delete/contains
entry point necessarily calls `PyObject_Hash` on a caller-supplied key. Three quarters of the
tree-wide `hash_entry_point` population lives here for the same reason that `Objects/listobject.c`
would dominate a "calls `PySequence_GetItem` in a loop" rule. The shapes that actually carry bugs
— `self_recursion`, `field_element_descent`, `container_element_descent`, `mutual_recursion`,
20 findings tree-wide — have **zero** instances in this slice.

The agent contract already says `hash_entry_point` sites "are **not** bugs in themselves and
should not be reported as FIX". Applying that literally: the rule's precision *as a bug detector*
in this slice is **1/25 = 4%**, and that 1 was already in the catalog. Its precision as
*blast-radius evidence* is 24/24 — I drove a SIGSEGV through 8 of the 24 (§5.3).

---

## 2. Findings

### 2.1 [CONFIRMED — already recorded, not re-litigated] CPY-0019 / gh-154318

`Objects/dictobject.c:8427` `frozendict_pair_hash` — guardless copy of `tuple_hash`; `hash()` of
a frozendict nested through its **values** overflows the native C stack. Still live at
`4f3be1b5777`. Reproduced **5/5** on `release-gil-nojit` and **3/3** on `debug-gil-nojit` at
depth 1,000,000; ASan (`release-gil-nojit-asan`, depth 400,000) reports
`stack-overflow ... T0` with strictly alternating
`frozendict_pair_hash dictobject.c:8427` / `frozendict_hash dictobject.c:8462` frames.
Upstream: gh-154318 open, fix PR gh-154362 open. Repro:
`reports/obj-mappings/repro/CPY-0019-frozendict-hash.py`.

Two refinements worth carrying into the record, both from running it:

- **Depth threshold on a 16 MB stack is between 200,000 and 300,000**, not the 1,000,000 I first
  tried. 200,000 **survives** 1/1; 300,000/400,000/500,000/600,000/1,000,000 all SIGSEGV. My
  first probe run at 200,000 reported this finding as `ok` — a false negative produced by my own
  harness, not by the code. Recording the threshold stops the next reader repeating it.
- **Nothing is memoised on the way in.** `frozendict_hash:8450` caches into `ma_hash`, and
  `frozendict_hash:8461` reads the *cached key* hash out of `_PyDict_Next` — but a frozendict's
  **values are never hashed at insertion**, so building `frozendict({0: prev})` N times leaves
  every level with `ma_hash == -1` and no primed value hashes anywhere. A single `hash()` call
  walks all N levels. This is the opposite of `tuple_hash`, where the briefing's "prime the
  caches" advice is needed; here the natural construction is already the worst case.

### 2.2 [POLICY] The dict/set repr family's **depth** bound is the dispatcher, not `Py_ReprEnter`

`Objects/dictobject.c:3690` (`anydict_repr_impl`), `Objects/setobject.c:760`
(`set_repr_lock_held`), `Objects/dictobject.c:6600` (`dictview_repr`).

**What is correct.** All three take `Py_ReprEnter` and release it on **every** exit including the
error paths — `anydict_repr_impl` at `:3696` (empty shortcut), `:3757` (success) and `:3762` (the
`error:` label, reached by all nine `goto error`s); `set_repr_lock_held` at `:770` (empty
shortcut) and `:806` (the `done:` label, reached by all three failure `goto`s); `dictview_repr`
at `:6612` (the `Done:` label). All
three correctly *omit* the leave on the `status != 0` early return, where the enter did not
succeed. **No missed `Py_ReprLeave` anywhere in the slice.** Verified by reading and by
`repro/cases.py::dict_repr_cycle / frozendict_repr_cycle / dictview_repr_cycle / set_repr_cycle`
(4/4 clean).

**What the guard does not do — lesson 3.** `Py_ReprEnter` is a **cycle** guard: it keys on object
identity in the thread state's repr-tracking list. It says nothing about **depth**. A
non-cyclic 400,000-deep `{0: {0: ...}}` visits a distinct object at every level, so
`Py_ReprEnter` never fires. What actually stops it is that `PyUnicodeWriter_WriteRepr`
(`:3732`, `:3745`), `PyObject_Repr` (`setobject.c:788`) and `%R` (`:6608`) all route through
`PyObject_Repr` at `Objects/object.c:759`, which *is* wrapped in
`_Py_EnterRecursiveCallTstate`. Confirmed: depth-400,000 `repr()` of a nested dict, frozendict,
set, `.items()` view and `.values()` view all raise
`RecursionError: Stack overflow (used 16,3xx kB) while getting the repr of an object` — the
dispatcher's message, 5/5 cases.

So the citation "these reprs are guarded, `Py_ReprEnter` is the fix pattern" is **half true and
the wrong half is load-bearing**. If a future refactor ever replaced a `PyUnicodeWriter_WriteRepr`
with a direct `tp_repr` slot call — or, more plausibly, added a `PyObject_Hash` to one of these
functions — the `Py_ReprEnter` already present would give a reader (and the scanner, see §4.2)
every reason to believe the obligation was discharged. POLICY, not FIX: no live defect, but the
guard that is present is not the guard that is doing the work.

**Guarded twin, correctly cited:** `Objects/object.c:759` `PyObject_Repr` — that, and not
`Py_ReprEnter`, is what these three functions depend on.

---

## 3. Classes bounded — clean negatives, each with its bound

### 3.1 `frozenset_hash` is bounded by design, and it is the twin that explains CPY-0019

`Objects/setobject.c:972` `frozenset_hash_impl` / `:1014` `frozenset_hash`. **Bound: 0.**

`frozenset_hash_impl:989` XORs `entry->hash` — the hash **cached in the setentry at insertion
time**. It never calls `PyObject_Hash`. Every element of a set is hashed exactly once, on the way
in (`set_add_key:603`), so at hash time the descent is already paid for and the loop is a flat
scan of `Py_hash_t`s. `frozenset_hash` additionally memoises into `so->hash` (`:1020`–`:1025`).

`setobject.c:969` carries the reciprocal comment *"If you update this code, update also
frozendict_hash() which copied this code"*, and `dictobject.c:8445` carries *"Code copied from
frozenset_hash()"*. **The copy is faithful on the key axis and adds an axis the original does not
have.** frozenset has only elements, all pre-hashed. frozendict has keys *and* values;
`frozendict_hash:8461` correctly reuses the cached `key_hash`, then hands the **value** to
`frozendict_pair_hash`, which hashes it fresh at `:8427`. That second axis is the entire bug. The
"guarded twin" here is not a guard at all — it is a data-structure property, and it does not
survive being copied into a type that has values.

**Measured A/B at identical depth on `release-gil-nojit`:**

| depth | `frozenset([...])` chain, `hash()` | `frozendict({0: ...})` chain, `hash()` |
|---|---|---|
| 500,000 | **ok** | **SIGSEGV** |
| 1,200,000 | **ok** | **SIGSEGV** |

(`repro/cases.py::frozenset_hash_deep` vs `CPY-0019-frozendict-hash.py`.)

I also ran `frozenset_hash_deep_no_prime`, which tries to build the chain without priming: it
cannot, because `frozenset([x])` *must* hash `x` to insert it. The priming is structural, not
incidental.

### 3.2 Every repr/str/richcompare descent in the slice is dispatcher-bounded

Complete inventory — 15 sites, all read:

| site | function | descends via | bound |
|---|---|---|---|
| `dictobject.c:1168` | `compare_unicode_generic` | `PyObject_RichCompareBool` | dispatcher `object.c:1099` |
| `dictobject.c:1224` | `compare_generic` | ↑ | ↑ |
| `dictobject.c:1499` | `compare_unicode_generic_threadsafe` | ↑ | ↑ |
| `dictobject.c:1577` | `compare_generic_threadsafe` | ↑ | ↑ |
| `dictobject.c:3732`, `:3745` | `anydict_repr_impl` | `PyUnicodeWriter_WriteRepr` → `PyObject_Repr` | `object.c:759` |
| `dictobject.c:4713` | `dict_equal_lock_held` | `PyObject_RichCompareBool` | `object.c:1099` |
| `dictobject.c:6608` | `dictview_repr` | `%R` → `PyObject_Repr` | `object.c:759` |
| `dictobject.c:6823` | `dictitems_xor_lock_held` | `PyObject_RichCompareBool` | `object.c:1099` |
| `dictobject.c:7079` | `dictitems_contains` | ↑ | ↑ |
| `setobject.c:112` | `set_compare_threadsafe` | ↑ | ↑ |
| `setobject.c:155` | `set_compare_entry_lock_held` | ↑ | ↑ |
| `setobject.c:185` | `set_compare_frozenset` | ↑ | ↑ |
| `setobject.c:288` | `set_add_entry_takeref` | ↑ | ↑ |
| `setobject.c:788` | `set_repr_lock_held` | `PyObject_Repr(keys)` | `object.c:759` |

Confirmed empirically at depth 200,000–400,000, all `RecursionError`, never a signal:
`dict_eq_deep`, `frozendict_eq_deep`, `frozenset_eq_deep`, `dictview_eq_deep`,
`frozenset_issubset_deep` (which exercises `issubset`, `-`, `|`, `&`, `^` and `isdisjoint` in one
shot), `dict_repr_deep`, `frozendict_repr_deep`, `set_repr_deep`, `dictview_items_repr_deep`,
`dictview_values_repr_deep`. 10/10.

Specifically on the parent's set-operations question: `set_issubset`, `set_union`,
`set_difference*`, `set_symmetric_difference*`, `set_intersection` and `set_isdisjoint` all reach
element comparison through `set_do_lookup` → one of the three `set_compare_*` helpers →
`PyObject_RichCompareBool`. There is no other element-descent path in `setobject.c`. `set_merge`
and `set_update_dict_lock_held` copy `entry->hash` straight across without re-hashing.

**A self-containing frozenset is not constructible** (`repro/cases.py::frozenset_selfcontaining`):
`s.add(s)` → `TypeError: cannot use 'set' as a set element (unhashable type: 'set')`, and
`frozenset([s])` → the same. The membership-testing escape hatch at `_PySet_Contains:2570` /
`frozenset___contains___impl:2627` hashes a *set* key with `frozenset_hash_impl` — which is the
bound-0 cached-hash function — so it opens no new descent either. The cyclic case the parent
asked about therefore does not exist for sets; only the deep case does, and it is bounded.

### 3.3 `Py_ReprEnter` inside the per-object critical section does not deadlock

`set_repr:815` and `dict_repr:3780` take `Py_BEGIN_CRITICAL_SECTION(self)` **before**
`Py_ReprEnter` runs, so a user `__repr__` that calls `repr(self)` re-acquires the same object's
lock on the same thread before the cycle guard can short-circuit. Tested on
`release-gil-nojit` and `debug-ft-nojit` × {set, dict, frozendict, items-view}: **8/8 return
`{set(...)}` / `{0: {...}}` and exit 0.** No hang, no deadlock. `repro/repr_reentry.py`.

### 3.4 Self-recursion: 1 real candidate, bounded at one frame

A tree-sitter pass over all 404 functions found 3 name-in-own-body hits.
`PyDict_GetItem:2469` and `PyDict_GetItemString:5546` are artefacts — the name appears inside the
`"Exception ignored in PyDict_GetItem()..."` **string literal**, not as a call. The one real hit
is `set_richcompare:2509`, which calls itself with `Py_EQ` to implement `Py_NE`; `Py_EQ` never
re-enters `set_richcompare`, so the chain is exactly one extra frame. This is precisely the
opcode-delegating shape the scanner already discards — **a correct suppression, counted as a
precision win.**

### 3.5 `dict_merge` / `update` re-entry is bounded

`dict_merge:4300` slow path runs `PyMapping_Keys(b)` and `PyObject_GetItem(b, key)` — arbitrary
Python — inside `Py_BEGIN_CRITICAL_SECTION(a)`. A mapping whose `keys()` calls `d.update(self)`
re-enters `dict_merge` on the same dict. Every level passes through a Python frame, so the eval
loop's own `Py_EnterRecursiveCall` bounds it; the question was only whether the C frames
`dict_merge` adds outrun that bound. They do not: `RecursionError`, exit 0, on
`release-gil-nojit`, `debug-gil-nojit` and `debug-ft-nojit` (3/3). `repro/dict_merge_reentry.py`.

### 3.6 Statically-typed hash dispatches — bound 0/1

`dictobject.c:1258`, `:1334`, `:7451` call `PyUnicode_Type.tp_hash(key)` on a value already
`PyUnicode_CheckExact`-gated. **Bound 1** (str hash, no descent) — the taxonomy's
"statically-known type slot" class.

`dictobject.c:2683` `_PyDict_GetItemWithError` calls `Py_TYPE(kv)->tp_hash(kv)` — a *runtime*
slot read — but the preceding statement is `assert(PyUnicodeCheckExact(kv))` and the function is
private C API whose documented contract is an exact `str`. **Bound 1 by contract**, with the
honest caveat that on a release build the assert is compiled out and a caller violating the
contract would reach an arbitrary `tp_hash`. Tree-wide there are only 5 such direct-slot
spellings, all of them either statically typed or inside `object.c`'s own dispatcher, so this is
a real but currently empty hole (§4.3).

### 3.7 Deep-copy / reduce / pickle paths

`set___reduce___impl:2721`, `setiter_reduce:1070`, `dictiter_reduce:6388`,
`frozendict_getnewargs:8347`, `dict_copy_impl`, `copy_lock_held_untracked:4474`,
`frozendict_copy_impl:8555`. All shallow: they build a list/tuple of borrowed elements or copy a
keys table. **No descent, bound 0.** `frozendict_getnewargs` calls `dict(op)` which reaches
`dict_dict_merge` — a flat table copy.

---

## 4. Toolkit feedback

### 4.1 [HIGH — denominator integrity] `recursion_prone_slot_functions` counts the wrong thing

`scan_recursion_guards.py:1330-1332`:

```python
if slot is not None:
    slot_functions += 1
```

but the field is emitted as `recursion_prone_slot_functions` (`:1398`) and appears in
`denominators` (`:1403`). `slot is not None` is true for **any** of tp_hash / tp_repr / tp_str /
tp_richcompare. The recursion-prone set is a strict subset —
`_RECURSION_PRONE_CALLER_SLOTS = {"tp_hash", "parameter_walk"}` (`:195`).

Measured on this slice: the envelope says **17**; re-running `_slot_for` over the same 404
functions and filtering by `_RECURSION_PRONE_CALLER_SLOTS` gives **6**
(`unicode_get_hash:449`, `setitem_take2_lock_held_known_hash:2805`, `frozendict_pair_hash:8414`,
`frozendict_hash:8446`, `frozenset_hash_impl:972`, `frozenset_hash:1014`). Tree-wide the envelope
claims 158.

This matters because AGENT_BRIEF lesson 2 instructs every agent to *quote the envelope's
denominator*. An agent doing exactly that reports "6 recursion-prone slots examined" as "17" — a
2.8× overstatement of how much was covered, in the one field that exists to prevent unearned
zeros. **Fix:** either rename the field to `slot_functions`, or gate the increment on
`slot in _RECURSION_PRONE_CALLER_SLOTS` and emit both counts.

Second-order: 2 of the true 6 (`unicode_get_hash`, `setitem_take2_lock_held_known_hash`) are not
slots at all — they are matched by the `_SLOT_NAME_SUFFIX` `("_hash", "tp_hash")` fallback.
So the *real* recursion-prone-slot denominator here is **4**, and 33% of the reported 6 is
misattribution.

### 4.2 [MEDIUM — model] `Py_ReprEnter` is in `_GUARD_TOKENS` and discharges the whole obligation

`scan_recursion_guards.py:182-192` lists `Py_ReprEnter` / `Py_ReprLeave` alongside
`Py_EnterRecursiveCall`, and `_has_guard` (`:358`) suppresses the entire function if any token is
present anywhere in the body. As §2.2 establishes, `Py_ReprEnter` bounds **cycles**, not
**depth** — the two guards address different threat models and are not interchangeable. A
`tp_repr` that holds `Py_ReprEnter` and also calls `PyObject_Hash` would be silently suppressed,
even though the hash descent is unguarded at every level.

**Measured impact today: zero.** I swept `Objects/` + `Modules/` + `Python/` for functions
containing `Py_ReprEnter`: **19 functions**, of which **0** also contain `PyObject_Hash` or
`_PyObject_HashDictKey`. So this is a latent model defect with a denominator of 19 and no live
consequence — worth fixing cheaply (drop the two repr tokens from `_GUARD_TOKENS` and give them
their own `cycle_guard` classification that suppresses only repr/str shapes), not worth
escalating.

### 4.3 [MEDIUM — recall] Direct `tp_hash` slot calls are outside the dispatch vocabulary

`_UNGUARDED_DISPATCH = {"PyObject_Hash", "_PyObject_HashDictKey"}` (`:143`). The spellings
`Py_TYPE(x)->tp_hash(x)` and `SomeType.tp_hash(x)` are invisible. Found by reading at
`dictobject.c:2683`, `:1258`, `:1334`, `:7451`.

Honest sizing: tree-wide there are exactly **5** such sites (`Objects/object.c:1162`, `:1172` —
the dispatcher itself; `Objects/dictobject.c:2683`; `Objects/typeobject.c:10932`
`PyLong_Type.tp_hash`; plus the `_Type.tp_hash` forms above). All are statically typed or are
`PyObject_Hash`'s own body. **The gap is real, the yield is currently zero.** Add
`->tp_hash(` / `_Type.tp_hash(` to the vocabulary with a bound-0 exemption for a receiver that a
preceding `*_CheckExact` or `assert` pins — cost is one regex, and it closes a hole that a future
type could walk through.

### 4.4 [MEDIUM — recall] Positional `PyTypeObject` slot registration is not parsed

`_collect_slot_map` (`:321`) matches only `.tp_hash = f` (`_SLOT_DESIGNATED_RE`) and
`{Py_tp_hash, f}` (`_SLOT_SPEC_RE`). Measured on this slice:

| file | positional (`f, /* tp_x */`) | designated (`.tp_x =`) | entries `_collect_slot_map` found |
|---|---|---|---|
| `Objects/setobject.c` | 119 | 4 | **0** |
| `Objects/dictobject.c` | 197 | 44 | **3** |

`PySet_Type`, `PyFrozenSet_Type`, `PyDict_Type` and every dict-view type register positionally
and are entirely invisible to the slot map; only `PyFrozenDict_Type` (the newest type, written
with designated initialisers) is seen, and it supplies exactly the 3 entries found. Everything
else — `set_repr`, `frozenset_hash`, `set_richcompare`, `dict_repr`, `dict_richcompare` — is
rescued only by the `_SLOT_NAME_SUFFIX` fallback, i.e. by CPython's naming convention rather than
by evidence.

This is the briefing's own standing trap #1 ("*the positional static `PyTypeObject` form names
its slots in trailing comments — 42 occurrences in `Objects/` versus 2 designated; any check for
slot registration must run on the raw source*"), unimplemented in this scanner. The failure mode
is silent and asymmetric: a positional-form type whose hash function is *not* named `*_hash`
disappears from the analysis entirely, and there is no signal that it did.

**Fix:** add a third pattern over the **raw** (un-scrubbed, comment-preserving) source:
`^\s*(?:\(\w+\))?\s*(\w+)\s*,\s*/\*\s*tp_(hash|repr|str|richcompare)\s*\*/`.

### 4.5 [LOW — recall] The `guarded_by_dispatcher` bucket is empty for the wrong reason

Zero `recursion_descent_guarded_by_dispatcher` findings here, against 15 hand-found
dispatcher-bounded descents (§3.2). Two causes, both benign but worth knowing:

- Shape 3 requires `slot is not None` (`:1006`). `dict_equal_lock_held`, `all_contained_in` and
  the four `set_compare_*` / `compare_*_generic` helpers are not slots, so the file's entire
  comparison machinery is outside the auditability bucket.
- `_GUARDED_DISPATCH` (`:157`) omits `PyUnicodeWriter_WriteRepr`, which is how
  `anydict_repr_impl` descends — the very spelling the briefing names as an indirect repr route.
  (`set_repr_lock_held` and `anydict_repr_impl` would still be suppressed by §4.2's
  `Py_ReprEnter` rule, so adding the spelling alone changes nothing until §4.2 is also fixed.)

Low priority: this bucket is `low` confidence and explicitly "for auditability only". But
"0 guarded descents" next to 15 real ones misdescribes the file.

### 4.6 [LOW] Self-recursion detection should strip string literals

My own mechanical self-recursion pass over the slice produced 2 false positives out of 3, both
from the function's name appearing in a `"Exception ignored in <name>()"` string literal
(`dictobject.c:2472`, `:5552`). `strip_comments` is applied; string literals are not. Cheap to
add, and it is the mirror image of the briefing's standing trap #2 ("*some markers only exist in
string literals*") — there, literals must be preserved; here, they must be removed.

### 4.7 PR #29's graph-field rule: does it earn its keep here? **No — and that is fine.**

The `->tp_base` / `->tp_bases` / `->tp_mro` / `->tp_subclasses` / `->tp_dict` read rule
contributed **0** findings. Denominator, measured rather than assumed: 8 textual occurrences
across the two files, of which **6 are positional slot-table comments** (`0, /* tp_base */`,
`0, /* tp_dict */` in the four type tables) and are not reads at all. The 2 genuine reads are
`dictobject.c:7279`/`:7280`, `cls->ht_type.tp_dict` feeding a `PyDict_GetItem` for
`__static_attributes__` in `_PyDict_NewKeysForClass` — a single flat lookup, not a graph descent.

**Verdict: structural zero, cost zero.** The rule cannot fire on this code because dict and set
do not walk the type graph; they *are* what the type graph is built out of. It produced no false
positives on its first exposure to code outside `typeobject.c`, which for a rule tuned on
`typeobject.c` is the right outcome — but this slice provides no evidence *for* it either. It
should be re-evaluated on a slice that touches the type/class machinery (`Objects/classobject.c`,
`Objects/moduleobject.c`, `Objects/abstract.c`), not retired on this one.

### 4.8 Precision per rule, this slice

| rule / shape | fired | true FIX | verdict |
|---|---|---|---|
| `slot_helper_descent` | 1 | 1 (CPY-0019, known) | **100% precision**, 0 net-new |
| `hash_entry_point` | 24 | 0 (0 expected by contract) | correct-by-contract; 8/8 reproduced as blast radius |
| `self_recursion` (opcode-delegating suppression) | 0 | — | correct suppression of `set_richcompare:2509` |
| `guarded_dispatcher_descent` | 0 | — | under-reports 15 real descents (§4.5) |
| graph-field read (PR #29) | 0 | — | structural zero, denominator ≈2 (§4.7) |
| `bound_zero_excluded` filter | n/a | — | correctly kept `frozenset_hash_impl`'s `entry->hash` scan out of the finding set |

---

## 5. Reproduction ledger

All files in `reports/obj-mappings/repro/`. Every row was run; none is inferred.

| repro | build | depth | result |
|---|---|---|---|
| `CPY-0019-frozendict-hash.py` | `release-gil-nojit` | 1,000,000 | **SIGSEGV 5/5** |
| ↑ | `debug-gil-nojit` | 1,000,000 | **SIGSEGV 3/3** |
| ↑ | `release-gil-nojit` | 300k / 400k / 500k / 600k | **SIGSEGV 1/1 each** |
| ↑ | `release-gil-nojit` | 200,000 | survived 1/1 (below threshold) |
| ↑ | `release-gil-nojit-asan` | 400,000 | **stack-overflow**, alternating `:8427` / `:8462` frames |
| `cases.py::frozenset_hash_deep` | `release-gil-nojit` | 500k, 1.2M | **ok 2/2** (the A/B control) |
| `cases.py::frozenset_hash_deep_no_prime` | `release-gil-nojit` | 1,200,000 | ok 1/1 |
| `cases.py` — 8 `entry_*` cases | `release-gil-nojit` | 1,200,000 | **SIGSEGV 8/8** (§5.3) |
| `cases.py` — 10 repr/compare depth cases | `release-gil-nojit` | 200k–400k | **RecursionError 10/10** |
| `cases.py` — 4 cycle cases | `release-gil-nojit` | — | clean `{...}` output 4/4 |
| `repr_reentry.py` | `release-gil-nojit`, `debug-ft-nojit` | — | exit 0, **8/8**, no deadlock |
| `dict_merge_reentry.py` | `release-gil-nojit`, `debug-gil-nojit`, `debug-ft-nojit` | — | RecursionError **3/3** |

### 5.3 Blast radius of the `hash_entry_point` sites (evidence, not findings)

Each of these adds exactly one C frame; the recursive frames belong to `frozendict_hash` (or
`tuple_hash`). At depth 1,200,000 on `release-gil-nojit`, all eight SIGSEGV:

`{deep}` (`set_add_key:603`) · `{deep: 1}` (`setitem_take2_lock_held:2823`) ·
`deep in {}` (`dict_contains:5278`) · `deep in set()` (`set_contains_key:614`) ·
`dict.fromkeys([deep])` · `d.items() | set()` (`dictviews_to_set:6662` → `PySet_New`) ·
`{deep_tuple}` and `hash(frozendict({0: deep_tuple}))`.

Two of these are worth calling out because they hash values that are *never otherwise hashed*:
`d.items() | set()` (`dictviews_or:6773`) and `d1.items() ^ d2.items()`
(`dictitems_xor_lock_held:6843`, `PySet_Add(result_set, pair)`) both build a set of `(key, value)`
tuples, so a dict whose **values** are deep tuples or frozendicts segfaults on a set operation
over its items view even though nothing ever hashed those values before. That widens CPY-0019 /
CPY-0001's reachable surface from `hash()` to ordinary view arithmetic.

---

## 6. Copy-paste propagation (Phase 4)

Complete tree-wide inventory of the two algorithms CPY-0019 descends from:

| algorithm | sites | descends into arbitrary PyObjects? |
|---|---|---|
| `tuple_hash` xxHash lane loop | `Objects/tupleobject.c:383` (original; `:367` names the copy) | **yes** — CPY-0001 / gh-154318 |
| ↑ | `Objects/dictobject.c:8413` `frozendict_pair_hash` | **yes** — CPY-0019 / gh-154318 |
| ↑ | `Modules/_cursesmodule.c:1616` `complexstr_hash` | **no** — see below |
| `frozenset_hash` xor-shuffle loop | `Objects/setobject.c:972` (original; `:969` names the copy) | **no** — cached `entry->hash` |
| ↑ | `Objects/dictobject.c:8445` `frozendict_hash` | **key axis no; value axis yes** |

`_shuffle_bits` is itself duplicated verbatim at `setobject.c:954` and `dictobject.c:8407`.

The reciprocal "update also…" comments exist in both directions for tuple↔frozendict
(`tupleobject.c:367` ↔ `dictobject.c:8413`) and frozenset↔frozendict (`setobject.c:969` ↔
`dictobject.c:8445`) — the comments landed, the guard landed in neither, exactly as gh-154318
notes.

**The third copy is clean and should not be swept up in the fix.** `complexstr_hash`
(`Modules/_cursesmodule.c:1616`) runs the same lane loop but over
`curses_cell_hash(state, &s->cells[i])` — a hash of a C struct of `chtype`/`wchar_t` cells, not a
`PyObject`. **Bound 0**: it cannot re-enter the object graph at any depth. It carries no
"copied from" comment, which is how it stayed invisible to the grep the two commented copies
answer to.

---

## 7. Noticed outside slice (one line each, no further work)

- `Modules/_cursesmodule.c:1616` `complexstr_hash` — a third, **correct** copy of the tuple-hash
  lane loop, with no "copied from" comment linking it to the other two.
- `Objects/odictobject.c` contributes 4 `hash_entry_point` findings to the `Objects/` scan — same
  structural class as this slice's 24; belongs to the odict slice.
- `set_repr_lock_held:788` calls `PyObject_Repr(keys)` — arbitrary Python — inside
  `Py_BEGIN_CRITICAL_SECTION(so)` (`set_repr:815`); `dict_equal:4728` and `dict_merge:4321`
  likewise run user `__eq__` / `keys()` / `__getitem__` under a critical section. Same shape as
  CPY-0096; for the ft-race agent, not this one. (It does **not** deadlock on same-thread
  re-entry — measured, §3.3.)
- `dictitems_contains:7077` reaches `PyDict_GetItemRef` → `_PyObject_HashDictKey` from inside an
  already-deep `dictview_richcompare` descent: the compare axis burns recursion budget and the
  hash axis burns none. No live bug (the hash descent needs deep *keys*, itself the known entry
  class), but it is the one place in the slice where a guarded and an unguarded descent
  interleave.

---

## 8. Classification summary

| verdict | count | items |
|---|---|---|
| **FIX** | 0 net-new | CPY-0019 confirmed (already recorded, gh-154318, PR gh-154362) |
| **CONSIDER** | 0 | — |
| **POLICY** | 1 | §2.2 — repr family's depth bound is the dispatcher, not `Py_ReprEnter` |
| **ACCEPTABLE** | 7 classes | §3.1–§3.7, each with its bound stated |
| **Toolkit** | 7 | §4.1 (high) · §4.2, §4.3, §4.4 (medium) · §4.5, §4.6 (low) · §4.7 (PR #29 verdict) |

The honest headline: **this slice is clean for the recursion-guard class apart from the one
already-known bug, and the scanner's 25-finding population here is 96% a shape its own contract
declares not to be a bug.** The value delivered is the measured bound behind each negative, the
depth threshold and blast radius for CPY-0019, and the four scanner defects in §4 — of which
§4.1 (a denominator field that overstates coverage 2.8×, in the exact field lesson 2 tells agents
to quote) and §4.4 (positional slot registration unparsed, so `setobject.c` has a slot map of
size zero) are the ones worth fixing before the next slice.
