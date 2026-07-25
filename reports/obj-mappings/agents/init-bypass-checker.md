# init-bypass-checker — obj-mappings slice

**Scope:** `/home/danzin/projects/cpython/Objects/dictobject.c` (8,597 lines) and
`Objects/setobject.c` (3,228 lines) @ `4f3be1b5777`.

**Build provenance:** every build in `~/projects/python_build_matrix/builds/` is at
`a1d580430c8`. `git diff a1d580430c8 4f3be1b5777 -- Objects/dictobject.c Objects/setobject.c`
is **empty** — the binaries I ran contain byte-identical copies of both slice files.

---

## 1. Denominator line

| quantity | value |
|---|---|
| **Scanner population (`scanners/scan_init_bypass.sample.json`)** | **0 findings out of 0 nullable fields**, 404 functions / 2 files |
| Scanner population, `Objects/` for comparison | 6 findings / 38 nullable fields |
| Scanner population, `Modules/` for comparison | 37 findings / 123 nullable fields |
| Types defined in the two files | **15** (11 in dictobject.c, 4 in setobject.c) |
| …Python-reachable | 14 (`_PySetDummy_Type` is internal-only) |
| …instantiable via `T.__new__(T)` | **4** — `dict`, `set`, `frozenset`, `frozendict` |
| …structurally un-instantiable | **10** — every view and every iterator |
| `tp_init` slots wired | **2** — `dict_init` (:5535), `set_init` (:2929) |
| `PyMemberDef` tables | **0** in either file |
| `PyGetSetDef` entries | **1** — `dictview_getset["mapping"]`, setter `NULL` → read-only |
| Nullable pointer fields found **by reading** | 4 — `di_dict`, `si_set`, `ma_values`, `di_result` |
| Field-read sites hand-audited | **47** (`di_dict` 7, `si_set` 4, `dv_dict` 12, `ma_values` 19, `di_result` 5) |
| Dynamic construction/method sweep | **512 probes × 4 builds = 2,048 runs, 0 crashes** |
| Layout-confusion sweep | **15 routes × 2 builds = 30 runs, 0 crashes** |
| Lines read | ≈ 2,100 of 11,825, plus `Objects/typeobject.c:9410-9455` |
| **FIX / CONSIDER / POLICY / ACCEPTABLE** | **0 / 0 / 1 / 4** |

### Is the zero STRUCTURAL or EVIDENTIAL?

**STRUCTURAL, at two independent levels — and toolkit defect D-8 is definitively NOT the cause.**

Evidence script: `reports/obj-mappings/repro/scanner_zero_is_structural.py` (runs the
scanner's own internals; reproduce with `~/venvs/cpython-review-toolkit/bin/python`).

*Level 1 — the tp_init/tp_new pairing filter fires first, before D-8 is reachable.*
`_positional_bypassable_inits()` returns `[]` for **both** files. It finds
`dict_init, /* tp_init */` (dictobject.c:5535) and `set_init, /* tp_init */`
(setobject.c:2929), then pairs each with the `tp_new` of the *same* `PyTypeObject`
block — `dict_new` (:5537) and `set_new` (:2931). Neither is in
`_INHERITED_NEW = {0, NULL, PyType_GenericNew}`, so both are dropped. `init_names`
is empty and `_collect_nullable_fields` returns **before** ever looking up a function
by name. **D-8's naming heuristic is never executed on this slice.**

*Level 2 — even with the filter defeated, there is nothing to find.* I patched
`tp_new` to `0` in `PyDict_Type` and `PySet_Type` and rescanned. The pairing filter
now passes, D-8's name lookup runs — and **succeeds**: it resolves `dict_init` and
`set_init` by their literal names (neither is an Argument Clinic `_impl` rename, so
D-8's fragile spelling assumption is not even stressed here). The result:

```
COUNTERFACTUAL (tp_new forced to 0)
  dictobject.c  positional bypassable tp_inits: ['dict_init']   nullable fields: {}
  setobject.c   positional bypassable tp_inits: ['set_init']    nullable fields: {'hash': {'new_bypass'}}
  -> still 0 findings
```

`dict_init` (dictobject.c:5398) is a one-line tail call to `dict_update_common` and
assigns **no struct field at all**. `set_init` (setobject.c:2763) assigns only the
scalar `self->hash = -1`. A `Py_hash_t` is consumed by no sink in the scanner's sink
set (`Py_INCREF`/`Py_NewRef`, the call family, deref macros, `_PyBytes_Resize`).

So the zero survives the removal of the only mechanism that produced it. That is the
definition of structural.

### Recall canary — the rule *can* fire on this corpus

Per the standing rule, a zero next to a zero denominator is silence unless the rule is
shown to be alive. I built a three-case C fixture and ran the real scanner over it:

| case | shape | scanner verdict | ground truth |
|---|---|---|---|
| A | `tp_init` + `tp_new = PyType_GenericNew` (the `bytearray`/`super`/gh-144330 shape) | **reported** `canarya_get` / `ca_field` / `Py_INCREF` | correct — bypassable |
| B | `tp_init` + a real `tp_new` (**the dict/set shape**) | silent | correct — not bypassable |
| C | static `PyTypeObject`, `tp_init` set, `tp_new = 0`, `tp_base = 0` | **reported** `canaryc_get` / `cc_field` | **false positive** — see §4 |

Case A firing is the canary: the rule is alive and would have surfaced a real bug in
these files had one existed. Case B firing silently is exactly why the slice is 0.

---

## 2. Findings

### [POLICY] Nine `dv_dict == NULL` guards defend a state that has never existed — and they read as a guarded twin for three unguarded siblings (`Objects/dictobject.c:6455`, `:6622`, `:6632`, `:7047`, `:7059`, `:7071`, `:7159`, `:7171`, `:7249` vs `:6489`, `:6657`, `:6878`)

**Mechanism.** `_PyDictViewObject.dv_dict` is read at 12 guard-relevant sites across
the three view types. Nine of them test `if (dv->dv_dict == NULL)` first —
`dictview_len` (:6455), `dictkeys_iter` (:6622), `dictkeys_contains` (:6632),
`dictkeys_reversed` (:7047), `dictitems_iter` (:7059), `dictitems_contains` (:7071),
`dictitems_reversed` (:7159), `dictvalues_iter` (:7171), `dictvalues_reversed` (:7249).
Three do not:

- `dictview_mapping` (:6489) → `PyDictProxy_New(mapping)`, which reaches `PyDict_Check`
  → `Py_TYPE(NULL)`.
- `dictviews_to_set` (:6657) → `PyAnyDict_CheckExact(dict)`, a `Py_IS_TYPE` deref.
- `dictitems_xor` (:6878-6879) → `Py_BEGIN_CRITICAL_SECTION2(d1, d2)`, which on a
  free-threaded build takes `&d1->ob_mutex`.

**This is not a bug**, and the guarded twin is not evidence that it is. Lesson 3
applied literally:

- `dv_dict` is written in exactly one place, `_PyDictView_New` (:6478), which always
  stores a real dict.
- `git log --all -S "dv_dict = NULL" -- Objects/dictobject.c` returns **nothing**:
  the field has never been assigned NULL anywhere in CPython's recorded history.
- None of the three view types has a `tp_clear` (all three tables spell `0` for it),
  and `git log -S "dictview_clear"` is empty.
- All three view types are structurally un-instantiable (§3), so no zeroed instance
  can exist.

The guards date to `3ac6741f792` (2007-02-10, Guido's original `__contains__` for
views) and belong to the abandoned design recorded in the TODO still sitting at
`dictobject.c:6499` — *"The views objects are not complete… support arbitrary
mappings? — either these should be static or exported in dictobject.h"*. **What that
guard actually addresses is a detachable / lazily-bound view that was never
implemented**, and that `type_ready_set_new` now structurally forecloses. It is not a
defence against any reachable NULL.

**Impact.** No crash. The cost is a false signal: an informed sibling-hunt that treats
the nine guards as the "correct handling" will promote the three unguarded sites as
missing-guard bugs. I nearly did; the taxonomy has no entry for this shape.

**Reproduction.** N/A — nothing to reproduce. Bounded statically by the four facts
above plus the 2,048-run dynamic sweep in §3.

**Suggested resolution (POLICY, for a maintainer):** either delete the nine dead
guards and state the invariant (`dv_dict` is non-NULL for the lifetime of a view) as
an `assert`, or add the missing three for symmetry. The current 9-vs-3 split is the
worst of both — it costs a branch on every view operation and misleads every reader.
Deleting is the honest option given the 2007 TODO is still unimplemented.

---

## 3. Classes bounded — what I checked and found clean *here*

### 3a. `T.__new__(T)` and the subclass bypass — 10 of 14 types are structurally immune (denominator 14)

`Objects/dictobject.c` and `Objects/setobject.c` contain **zero** occurrences of the
token `DISALLOW_INSTANTIATION`. That is not an omission. `type_ready_set_new`
(`Objects/typeobject.c:9428-9437`) adds the flag **implicitly**:

```c
    if (type->tp_new == NULL
        && base == &PyBaseObject_Type
        && !(type->tp_flags & Py_TPFLAGS_HEAPTYPE))
    {
        if (initial) {
            type_add_flags(type, Py_TPFLAGS_DISALLOW_INSTANTIATION);
        }
```

Every dict view, every dict iterator and the set iterator is a static `PyTypeObject`
that spells no `tp_new` and no `tp_base`. All ten therefore carry the flag. Verified
from Python (`release-gil-nojit`):

```
dict_keys                  flags=0x5182  DISALLOW_INSTANTIATION=True
dict_values                flags=0x5182  DISALLOW_INSTANTIATION=True
dict_items                 flags=0x5182  DISALLOW_INSTANTIATION=True
dict_keyiterator           flags=0x5182  DISALLOW_INSTANTIATION=True
set_iterator               flags=0x5182  DISALLOW_INSTANTIATION=True
dict  DISALLOW = False        set  DISALLOW = False
```

**This is the single fact that bounds the entire slice**, and it is exactly the
construction shape the brief pointed at (`bytearray.__new__(bytearray).append(1)`,
`super.__new__(super).__get__(1)`). Those two crash because their tables spell
`PyType_GenericNew` — an *explicit, non-NULL* `tp_new`, so the auto-flag does not
apply to them. dict/set's iterators and views spell nothing, so it does.

Dynamic confirmation — `repro/init_bypass_sweep.py`, four construction routes
(`T.__new__(T)`, a subclass whose `__init__` skips `super().__init__()`,
`object.__new__(T)`, plain subclassing) × 14 types × every callable attribute and 12
operators, each probe in a forked child so one SIGSEGV cannot hide the rest:

| build | probes | crashes |
|---|---|---|
| `release-gil-nojit` | 512 | **0** |
| `debug-gil-nojit` | 512 | **0** |
| `debug-ft-nojit` | 512 | **0** |
| `release-ft-nojit` | 512 | **0** |

**2,048/2,048 runs clean.** All 10 iterator/view types refuse every route
(`TypeError: cannot create 'dict_keyiterator' instances` /
`type 'dict_keys' is not an acceptable base type`). The 4 constructible types all
route through a real `tp_new` that fully builds the struct:

- `dict_new` → `anydict_new_untracked` (:5354) sets `ma_used`, `_ma_watcher_tag`,
  `ma_keys = Py_EMPTY_KEYS`, `ma_values = NULL`.
- `set_new` / `frozenset_new` → `make_new_set_untracked` (:1351) sets `fill`, `used`,
  `mask`, `table = smalltable`, `hash`, `finger`, `weakreflist`.
- `frozendict_new` → `frozendict_new_untracked` (:8488), which is
  `anydict_new_untracked` plus `ma_hash = -1`.

`dict_init` and `set_init` assign no `PyObject *` field, so even a hypothetical bypass
would leave nothing NULL. The subclass bypass is equally inert: `D()` for
`class D(dict): def __init__(self): pass` still runs the inherited `dict_new`.

### 3b. Deletable members and getsets — denominator 1, zero deletable

Neither file defines a `PyMemberDef` table. The only `PyGetSetDef` is
`dictview_getset` (dictobject.c:6493), one entry, `{"mapping", dictview_mapping, NULL, …}`
— setter `NULL`, so `del v.mapping` raises `AttributeError` and cannot NULL anything.
There is no `del obj.attr` path into either file. **The gh-152817 mechanism has no
surface here.**

### 3c. Fields that ARE legitimately NULL — 47 read sites, all correctly guarded

The brief asked specifically about `di_dict` / `si_set`. Both are genuinely nullable,
not via init bypass but via **exhaustion** — the CPY-0026 / CPY-0047 mechanism.

`di_dict` is set to NULL and its reference dropped at five exhaustion sites
(dictobject.c :5773, :5896, :6028, :6158, :6338). Seven read sites; every one is
guarded:

| site | function | guard |
|---|---|---|
| :5682 | `dictiter_len` | `di->di_dict != NULL &&` |
| :5784 | `dictiter_iternextkey` | `if (d == NULL) return NULL;` |
| :5907 | `dictiter_iternextvalue` | same |
| :6185 | `dictiter_iternextitem` | same |
| :6347 | `dictreviter_iternext` | same |
| :6393/:6395 | `dictiter_reduce` | `Py_XINCREF` / `Py_XDECREF` (NULL-safe) |

`si_set` (setobject.c) is identical: NULLed at :1130, four read sites, guarded at
:1062 (`si->si_set != NULL &&`), :1101-1103 (`if (so == NULL) return NULL;`),
:1076/:1080 (`Py_XINCREF`/`Py_XDECREF`). Dealloc and traverse use `Py_XDECREF` /
`Py_VISIT` throughout on both types.

`di_result` is NULL for key and value iterators. Its five reads are all inside
type-discriminated branches (`dictiter_iternextitem` :6197 runs only on
`PyDictIterItem_Type`; `dictreviter_iter_lock_held` :6315 is inside
`else if (Py_IS_TYPE(di, &PyDictRevIterItem_Type))`) or NULL-safe macros.

`ma_values` is NULL for every combined table — the single most-often-NULL field in the
slice. All 19 `ma_values->` derefs are inside a `_PyDict_HasSplitTable(mp)` branch
(which *is* `mp->ma_values != NULL`), an `#ifdef`-guarded consistency `CHECK`, or an
`assert`. No unguarded deref.

**Guarded twin for this class:** `dictiter_iternextkey` (:5784-5787). It is a real
twin — its guard defends against the *actually reachable* post-exhaustion state, and
the same guard is present at all four sibling iternext entry points and both
`__length_hint__` implementations. Contrast with the `dv_dict` guards in §2, which
look identical but defend nothing.

### 3d. `frozendict` — the newest type, and the one layout risk worth probing (30 runs, 0 crashes)

`PyFrozenDictObject` is `PyDictObject` plus a trailing `Py_hash_t ma_hash`
(`__basicsize__` 56 vs 48). If a dict-layout instance could be retyped to a
frozendict-layout type, `frozendict_hash` (:8447) would read `ma_hash` past the end of
the allocation. All four allocation sites set it — `new_dict_impl` (:960, under
`if (frozendict)`), `copy_lock_held_untracked`'s split-table branch (:4525),
`frozendict_new_untracked` (:8498) — and `frozendict_new` is a real `tp_new`.

`repro/frozendict_layout_confusion.py` probes 15 confusion routes on `debug-gil-nojit`
and `release-gil-nojit`. **All refused, 30/30 runs clean:**

- `__class__` assignment between heap subclasses of `dict` and `frozendict` → refused
  both directions (`object layout differs`), because their solid bases differ.
- `class M(dict, frozendict)` / `(frozendict, dict)` → `instance lay-out conflict`.
- `frozendict.__hash__` applied to a `dict` or a `dict` subclass → refused by the
  descriptor type check (3 variants).
- Eight `dict` mutators (`__init__`, `update`, `__setitem__`, `clear`, `pop`,
  `popitem`, `setdefault`, `__ior__`) applied to a `frozendict` → all refused by the
  descriptor check, so the immutability guarantee is not bypassable via the unbound
  `dict` methods.
- `frozendict.__new__(frozendict)` then `hash()` → **works correctly**, returning the
  empty-frozendict hash. `ma_hash` was initialised by `frozendict_new`.

### 3e. Previously-recorded findings in my lane — confirmed, not re-litigated

- **CPY-0079** (`copy_lock_held_untracked`:4494, assert over an unchecked allocation)
  — I read the function while auditing `ma_hash` initialisation. Still present at the
  target ref, unchanged. Not my class; the OOM re-run it needs (its clean sweep had a
  denominator of 2) is still owed.
- **CPY-0015** (`dictiter_new`:5646) — the `_PyTuple_FromPairSteal` failure path
  `Py_DECREF`s a never-tracked iterator. Still present, unchanged. Adjacent to my
  class (a half-built object) but it is the uninit-dealloc agent's finding.
- **CPY-0019** (`frozendict_pair_hash`:8427) — still present, unchanged.

---

## 4. Toolkit feedback

### 4a. RECALL/PRECISION GAP (highest value) — `Py_TPFLAGS_DISALLOW_INSTANTIATION` is applied implicitly, and neither the scanner nor the agent prompt models it

The agent prompt states the `new_bypass` precondition as:

> its `tp_new` must be `0` (inherited) or `PyType_GenericNew`, with no
> `DISALLOW_INSTANTIATION` — then `T.__new__(T)` … yields NULL

**That is factually wrong for a static `PyTypeObject` whose `tp_base` is `object`.**
`type_ready_set_new` (`Objects/typeobject.c:9428-9437`) adds
`Py_TPFLAGS_DISALLOW_INSTANTIATION` to exactly that shape, so `tp_new == 0` on a
static type with no `tp_base` means **not bypassable**, the opposite of what the
prompt says. The scanner has no `tp_base` model at all
(`grep -n "tp_base\|HEAPTYPE\|PyBaseObject" scan_init_bypass.py` → no matches).

Measured cost, both directions:

- **Hand-triage (the expensive direction).** The brief tells agents to READ, not
  triage scanner output. Applying the prompt's rule by hand to this slice promotes
  **10 of 14 types** — every dict view and every dict/set iterator — as CONSIDER
  candidates. That is the whole slice's worth of wasted effort, and it is the exact
  scenario the brief's hunt list describes ("can they be constructed directly from
  Python … and then have `__next__` called on a never-initialized struct?"). The
  answer is no, for a reason written 3,000 lines away in another file.
- **Scanner precision (latent, currently 0 active instances).** Canary case C shows the
  scanner reports this shape. I measured the live rate: 43 `new_bypass` findings across
  `Objects/` (6) and `Modules/` (37), and **none** is this shape — the `Objects/` ones
  are `bytearray`/`super`/`apply_slot_updates` (explicit `PyType_GenericNew` or a spec
  table) and every `Modules/` one is a `PyType_Spec` heap type, where the auto-flag
  does not apply. So the defect is real in the model but currently unexercised. It
  bites *triage*, not the finding count.

**Concrete proposals.**

1. *Prompt / FP-taxonomy entry (do this first — it is where the cost is).* Add to
   `data/cpython_non_bugs.md`, under a new "Init bypass" heading:

   > **`tp_new == 0` on a static type is NOT a bypass — it is the opposite.**
   > `type_ready_set_new` (`Objects/typeobject.c:9428-9437`) adds
   > `Py_TPFLAGS_DISALLOW_INSTANTIATION` to any type with `tp_new == NULL`,
   > `tp_base == &PyBaseObject_Type`, and no `Py_TPFLAGS_HEAPTYPE`. The token
   > `DISALLOW_INSTANTIATION` therefore appears **nowhere** in `Objects/dictobject.c`
   > or `Objects/setobject.c`, yet all 10 view/iterator types carry the flag —
   > verify with `bool(type(iter({})).__flags__ & (1 << 7))` → `True`.
   > The bypassable static shape is `tp_new = PyType_GenericNew` (an explicit,
   > non-NULL slot), which is why `bytearray` and `super` crash and the dict/set
   > iterators do not. `tp_new == 0` **is** bypassable when `tp_base` names another
   > built-in, or on a heap type from `PyType_FromSpec` — that is why gh-152954
   > (`sqlite3.Connection`) and gh-144330 are real.

   This is the same failure mode already catalogued for `Py_TRASHCAN_BEGIN`: a
   marker-based test where the real mechanism is implicit and lives elsewhere.

2. *Scanner (cheap, but low yield — do it for correctness, not for noise).* In
   `_positional_bypassable_inits`, when the resolved `tp_new` is literally `0`/`NULL`,
   also read the block's `tp_base`; if that is `0`/`NULL` too, suppress. Keep
   `PyType_GenericNew` bypassable unconditionally. Leave `_spec_bypassable_inits`
   alone — spec/heap types genuinely do inherit.

3. *Envelope.* Emit `types_seen` and `types_suppressed_disallow_instantiation` next to
   `total_nullable_fields`. Today an agent cannot tell "no type in this file has the
   shape" from "the slot tables did not parse". On this slice the honest envelope is
   `types_seen: 15, tp_init_wired: 2, suppressed_real_tp_new: 2`.

### 4b. Precision, per rule, on this slice

`new_bypass`: 0 findings / 0 candidates — correct, and correct *for the right reason*
(§1 level 1). `deletable_member`: 0 / 0 — correct, no member table exists.
`deletable_getset`: 0 / 1 candidate examined — correct, the one getset has a `NULL`
setter. No false positives, no false negatives that I could find by reading.

### 4c. A guard-asymmetry heuristic the scanner does not have

The §2 `dv_dict` situation is a shape worth detecting mechanically and is currently
invisible: *a field guarded `== NULL` at ≥ N sites in one file and read unguarded at
≥ 1 other, where the field has no NULL store anywhere in the file*. That combination
is either (a) a real missing guard, or (b) dead defensive code, and it is cheap to
compute — the scanner already collects per-field guard state in `_has_null_guard`. It
would have to be reported as `guard_asymmetry_no_null_store`, explicitly **not** as a
`new_bypass` finding, and triaged POLICY by default. On this slice it would have
surfaced the 9-vs-3 split; tree-wide it is the fastest route to the CPY-0030 /
CPY-0057 shape (a guard added to one arm of a mutually exclusive pair).

### 4d. D-8 assessment

D-8 is real but **did not fire here, and would not have** even with the upstream filter
removed: `dict_init` and `set_init` are plain C function names that
`_collect_nullable_fields` resolves directly, with no Argument Clinic `_impl` rename in
play. Verified by the counterfactual in §1. D-8 remains a live risk for
Clinic-generated `tp_init`s in `Modules/`; this slice provides no evidence either way.

---

## 5. Noticed outside slice (one line each)

- `Objects/typeobject.c:9428-9437` `type_ready_set_new` — the implicit-DISALLOW
  mechanism; belongs to the obj-typeobject slice, noted here only because it is the
  fact that bounds this one.
- `Objects/descrobject.c` `PyDictProxy_New` — no NULL guard on its argument; the only
  slice caller (`dictview_mapping`:6489) provably cannot pass NULL, so this is latent,
  not live.
- `Objects/dictobject.c:6388` `dictiter_reduce` / `Objects/setobject.c:1070`
  `setiter_reduce` copy the whole iterator struct (`dictiterobject tmp = *di;`),
  including `ob_mutex` on a free-threaded build — the CPY-0028 `odictiter_reduce`
  shape. It does **not** reproduce here: neither `dictiter_reduce` nor the iternext
  path it drives takes a critical section on the *iterator* (both lock the dict/set
  instead), so the copied mutex is never held at copy time. Flagging for the ft-race
  agent to confirm rather than dismissing outright.
- `Objects/dictobject.c:6499` — the 2007 TODO ("The views objects are not complete…")
  is still live and is the stated rationale for the dead guards in §2.

---

## 6. Artifacts

All under `/home/danzin/projects/cpython-review-toolkit/reports/obj-mappings/repro/`:

| file | purpose |
|---|---|
| `init_bypass_enumerate.py` | one line per type: does `T.__new__(T)` succeed? |
| `init_bypass_sweep.py` | the 512-probe × 4-build construction + method sweep |
| `frozendict_layout_confusion.py` | the 15-route dict/frozendict layout probe |
| `scanner_zero_is_structural.py` | the counterfactual proving the zero is structural and D-8 is not the cause |

Run every one with an explicit interpreter — `~/venvs/cpython-review-toolkit/bin/python`
for the scanner script, `~/projects/python_build_matrix/builds/<name>/python` for the
three probes. Bare `python` is RustPython here and prints nothing.
