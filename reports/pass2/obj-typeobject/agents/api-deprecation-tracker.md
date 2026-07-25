# API deprecation tracker — `Objects/typeobject.c` **PASS 2**

**Slice:** `obj-typeobject`, pass 2 (informed-explore)
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0)
**Scope:** `Objects/typeobject.c`, with the compatibility surface it *defines* followed out
of the file where the answer lives there.
**Pass 1's report:** `reports/obj-typeobject/agents/api-deprecation-and-macros.md` — read, not redone.

---

## Headline

This file consumes almost no deprecated API (1 hit, already triaged in pass 1). It **defines**
a large one, and the interesting result is a matched pair:

1. **The entire `PyType_FromSpec` family was soft-deprecated in 3.16 — the version in this
   tree — in favour of `PyType_FromSlots`. CPython has migrated 0 of its own 173 call sites.**
   Not one. The only `PyType_FromSlots` callers in the tree are three lines of
   `Modules/_testlimitedcapi/slots.c`.
2. **The new API's own deprecations are enforced only against the new API.** 81 of 111 slots
   are declared `nulls = 'deprecated'` / `duplicates = 'deprecated'` in `Python/slots.toml`,
   and `Python/slots.c:350`/`:383` gate the `DeprecationWarning` on
   `states[0].slot_struct_kind == _PySlot_KIND_SLOT` — true only for `PyType_FromSlots`.
   Every legacy `PyType_Spec` caller — i.e. all 173 in CPython and effectively all of the
   ecosystem — takes the `else` arm, which is `MSG(...)`, which compiles to nothing.

   **Reproduced** on `debug-gil-nojit`: 8/8 new-API paths warn, 0/8 legacy-spec paths warn.

So a deprecation was added in 3.16 that, today, reaches no code that exists. That is a
defensible transitional choice, but it is a *decision*, it is undocumented, and it has an owner.

Nothing in this lane is a FIX in `Objects/typeobject.c`. As the brief predicted, this is a
POLICY lane — three POLICY items with concrete, decidable questions, one CONSIDER outside the
slice, and four earned negatives.

---

## 0. Method, denominators, and the `functions_analyzed=0` question

**The scanner reported 1 finding on this file against 141 across all of `Objects/`.** That is a
real ratio over a real denominator: the vocabulary holds 93 APIs, pass 1 swept all 93 by word
boundary across all 13,068 lines independently of the scanner and found exactly the one reported
hit. Recall on this file is 1/1. `typeobject.c` is genuinely not a deprecated-API consumer.

**`functions_analyzed=0` is not a measurement.** It is hard-coded:
`plugins/cpython-review-toolkit/scripts/scan_deprecated_apis.py:480` writes
`functions_analyzed=0` unconditionally, because `scan_deprecated_apis` is a **line-based** rule —
it matches API tokens against source lines and never builds a function table. Reporting it as a
zero next to `files_analyzed=1` invites the reading "no functions were analysed", which is false.
The honest denominator for this rule is `files_analyzed × vocabulary_size` = 1 × 93. See §6.2 for
the proposed envelope fix.

**Vocabulary coverage of this file's subject matter is one entry.** Of the 93 APIs, exactly one
is type-related: `Py_TPFLAGS_HAVE_FINALIZE`. The `PyType_From*` family, `Py_tp_base`,
`Py_TPFLAGS_HAVE_VERSION_TAG` and `Py_TPFLAGS_VALID_VERSION_TAG` are all absent, and all four
have citable directives. That is the recall gap this pass closes (§6.1).

**Build-matrix diff.** Every file cited below is **byte-identical** between the target
`4f3be1b5777` and the build-matrix ref `a1d580430c8`:
`Objects/typeobject.c`, `Include/object.h`, `Include/slots_generated.h`,
`Include/internal/pycore_slots.h`, `Include/internal/pycore_slots_generated.h`,
`Python/slots.c`, `Python/slots.toml`, `Python/slots_generated.c`,
`Lib/test/test_capi/test_slots.py`, `Modules/_testlimitedcapi/slots.c`,
`Doc/c-api/type.rst`, `Doc/c-api/typeobj.rst`, `Doc/c-api/slots.rst`, `Doc/deprecations/`.
The single exception is `Modules/posixmodule.c` (8 insertions / 2 deletions: `RWF_NOSIGNAL`
plus a Windows `os.cpu_count()` change). The line I cite there moved `:17075` → `:17078` and is
otherwise unchanged, so the §2.3 finding is live at both refs.

---

## 1. The one scanner finding — CONFIRMED, not re-litigated

`deprecated-api PyEval_GetGlobals` @ `Objects/typeobject.c:4485`, in `type_new_set_module`.

**Confirmed as pass 1 recorded it.** The API is genuinely soft-deprecated (3.13, PEP 667,
`Doc/c-api/reflection.rst`), it carries no `Py_DEPRECATED` marker, no removal is scheduled, and
the call as written is correct — the borrowed `globals` is consumed by `PyDict_GetItemRef` on
an exact dict with no intervening user code. **POLICY**, unchanged.

Two notes and nothing more:

- **`:4485` is pass-1 territory.** `type_new_set_module` (`:4474-4497`) is inside the `type_new`
  helper cluster, not any of the eight pass-2 regions. Pass 2 adds no new information about it,
  and I did not re-derive pass 1's `Python/ceval.c` comparison.
- **Pass 1's toolkit FIX has landed.** The sample envelope for this run
  (`scanners/scan_deprecated_apis.sample.json`) now carries `"drop_in": false` plus the full
  borrowed-vs-strong caveat and `findings_needing_a_caveat: 1`. Pass 1's item 2 is closed.
  Pass 1's item 5 stands: `PyEval_GetLocals.removed_in` still needs re-checking each release
  against gh-125170.

---

## 2. The inverted question: the compatibility surface this file *defines*

### 2.1 `PyType_FromSpec` / `WithBases` / `FromModuleAndSpec` / `FromMetaclass` — soft-deprecated **in this very release**, 0 % migrated — **POLICY**

**The deprecation.** Four `.. soft-deprecated:: next` directives, all reading *"Prefer
`PyType_FromSlots` in new code"*:

| API | directive | stable ABI since |
|---|---|---|
| `PyType_FromMetaclass` | `Doc/c-api/type.rst:827` | — (3.12, not in `stable_abi.dat`) |
| `PyType_FromModuleAndSpec` | `Doc/c-api/type.rst:858` | — |
| `PyType_FromSpecWithBases` | `Doc/c-api/type.rst:884` | **3.3** (`Doc/data/stable_abi.dat:785`) |
| `PyType_FromSpec` | `Doc/c-api/type.rst:909` | **3.2** (`Doc/data/stable_abi.dat:784`) |

`next` = the release being cut from this tree = **3.16**. `PyType_Spec` itself is now
described as *"Structure defining a type's behavior, used for soft-deprecated functions"*
(`Doc/c-api/type.rst:928`). The replacement, `PyType_FromSlots`, is stable ABI since **3.15**
(`Doc/data/stable_abi.dat:783`).

**Where this file implements it.** All five entry points are thin wrappers over one function:

```
Objects/typeobject.c:5802  PyType_FromSlots          -> type_from_slots_or_spec(slots, NULL, ...)
Objects/typeobject.c:5808  PyType_FromMetaclass      -> type_from_slots_or_spec(NULL, spec, ...)
Objects/typeobject.c:5816  PyType_FromModuleAndSpec  -> type_from_slots_or_spec(NULL, spec, ...)
Objects/typeobject.c:5822  PyType_FromSpecWithBases  -> type_from_slots_or_spec(NULL, spec, ...)
Objects/typeobject.c:5828  PyType_FromSpec           -> type_from_slots_or_spec(NULL, spec, ...)
```

`type_from_slots_or_spec` (`:5248`) branches on `spec != NULL` at `:5284-5303`, choosing
`_PySlotIterator_InitLegacy` for the old path and `_PySlotIterator_Init` for the new one. The
`NO_SPEC` macro (`:5305-5313`, pass 1's finding M6) is the enforcement boundary: nine slot IDs —
`Py_tp_name`, `Py_tp_metaclass`, `Py_tp_module`, `Py_tp_basicsize`, `Py_tp_extra_basicsize`,
`Py_tp_itemsize`, `Py_tp_flags` and others — raise `SystemError` if reached through a `PyType_Spec`.
That is the shim, and it is well built: one function, one iterator abstraction, no duplicated
slot-handling logic.

**Is the deprecation enforced?** No, and it structurally cannot become enforced. No
`Py_DEPRECATED` marker on any of the four declarations (`Include/object.h:338, 340, 346, 359`).
No runtime warning. `Doc/c-api/slots.rst:38` states the position outright: *"The earlier API is
soft deprecated; there are no plans to remove it."* And `PyType_FromSpec` / `PyType_FromSpecWithBases`
are in the **stable ABI since 3.2 / 3.3**, so they can never be removed at all. The compat arm of
`type_from_slots_or_spec` is permanent code.

**Is there dead compatibility code for unsupported versions?** No. Measured, not assumed:
`Objects/typeobject.c` contains **zero** `#if PY_VERSION_HEX`, `#if PY_MAJOR_VERSION` or
`Py_LIMITED_API` preprocessor guards. There is no version-conditional code in this file to be
dead. All compatibility here is data-driven (slot tables, flag bits), not preprocessor-driven —
which is why §3 is where the version bridges actually live.

**Migration status: 0 of 173.**

| directory | `FromSpec` | `WithBases` | `FromModuleAndSpec` | `FromMetaclass` | total |
|---|---:|---:|---:|---:|---:|
| `Modules/` | 45 | 11 | 93 | 20 | **169** |
| `Objects/` | 1 | 1 | 0 | 0 | **2** |
| `Python/` | 1 | 0 | 1 | 0 | **2** |
| **total** | 47 | 12 | 94 | 20 | **173** |

Plus **259** `PyType_Spec` objects tree-wide. The two `Objects/` sites are
`Objects/structseq.c:780` and `Objects/typevarobject.c:2407`; the two `Python/` sites are
`Python/Python-ast.c:6116` and one `FromModuleAndSpec`.

`PyType_FromSlots` call sites in the whole tree, excluding its own definition:

```
Modules/_testlimitedcapi/slots.c:76
Modules/_testlimitedcapi/slots.c:308
Modules/_testlimitedcapi/slots.c:648
```

That is the entire adoption. Three lines, all in the test-support module written to exercise the
API.

**The POLICY question, stated precisely.** CPython soft-deprecated an API it uses 173 times, in
the same release in which it did so, with no migration of its own code and no plan recorded
anywhere. The toolkit's own calibration says *"CPython itself should lead by example"* — here it
demonstrably does not. Three sub-questions, each with a different owner:

1. *Is a 173-site internal migration intended at all?* If yes it needs a tracking issue; if no,
   `Doc/c-api/slots.rst:38` should say so, because "prefer X in new code" reads as a migration
   commitment that is not being honoured internally.
2. *Should the soft deprecation be recorded in `Doc/deprecations/soft-deprecations.rst`?* That
   file currently lists **only** `re.match`. None of the four C-API soft deprecations appear in
   it, nor does `Py_tp_base` (§2.2) or `Py_TPFLAGS_HAVE_VERSION_TAG` (§2.3). Anyone auditing
   soft deprecations by reading the canonical index misses every C-API one.
3. *Is `PyType_FromMetaclass` really in the same bucket?* It is the only one of the four that is
   **not** stable-ABI and the only one added recently (3.12). Soft-deprecating a four-year-old
   API in favour of one added in 3.15 is a much shorter round trip than the 3.2-era pair, and
   is the one entry a maintainer might want to reconsider.

Owner: the C API Working Group / the author of the `PySlot` API.

### 2.2 `Py_tp_base` → `Py_tp_bases` — soft-deprecated 3.15 — **ACCEPTABLE**

`Doc/c-api/type.rst:700`: `.. soft-deprecated:: 3.15`, *"When not targetting older Python
versions, pefer `Py_tp_bases`"* (typo `pefer` is upstream's).

Implemented in this file at `Objects/typeobject.c:5331-5338` — first pass — and skipped in the
second pass at `:5673-5677`:

```c
case Py_tp_bases:
    bases_slot = it.current.sl_ptr;
    break;
case Py_tp_base:
    if (!_PySlotIterator_SawSlot(&it, Py_tp_bases)) {
        bases_slot = it.current.sl_ptr;
    }
    break;
```

The precedence rule the docs state ("if both are specified, `Py_tp_bases` takes priority") holds
in **both** array orders: if `Py_tp_bases` came first, `SawSlot` suppresses the `Py_tp_base`
branch; if it came second, it simply overwrites. Correct as written.

`Py_tp_base` is stable ABI since 3.2 (`Doc/data/stable_abi.dat:1104`) — permanent, cannot be
removed. In-tree uses: **3**, all setting `NULL` at compile time and filling at runtime —
`Modules/_testmultiphase.c:373`, `Modules/xxlimited_35.c:194` and `:215`. `xxlimited_35` is by
name the 3.5-era limited-API demo module and `_testmultiphase` is test support; both exist
precisely to exercise the older surface. **ACCEPTABLE** — no migration is owed here.

### 2.3 The legacy `tp_flags` bits — three earned negatives and one live consumer outside the slice

| flag | status | uses in `typeobject.c` | uses tree-wide | verdict |
|---|---|---:|---:|---|
| `Py_TPFLAGS_HAVE_FINALIZE` | deprecated **3.8**, pending removal *future* | 0 | **3** | CONSIDER (out of slice) |
| `Py_TPFLAGS_HAVE_VERSION_TAG` | soft-deprecated **3.13** | 0 | 0 | ACCEPTABLE |
| `Py_TPFLAGS_VALID_VERSION_TAG` | "Unused. Legacy flag" | 0 | 0 | POLICY (docs gap) |
| `Py_TPFLAGS_HAVE_STACKLESS_EXTENSION` | always 0 in a shipped build | 4 (via `Py_TPFLAGS_DEFAULT`) | — | ACCEPTABLE, do not "clean up" |

**`Py_TPFLAGS_HAVE_FINALIZE`** — `Include/object.h:577`, deprecated at
`Doc/c-api/typeobj.rst:1380` (*"This flag isn't necessary anymore"*), listed in
`Doc/deprecations/c-api-pending-removal-in-future.rst:7`. It is **not** in the stable ABI
(`Doc/data/stable_abi.dat` lists only `BASETYPE`, `DEFAULT`, `HAVE_GC`, `HAVE_VECTORCALL`,
`ITEMS_AT_END`, `METHOD_DESCRIPTOR` at `:999-1004`), so unlike everything else in this section it
genuinely *can* be removed. Zero uses in `typeobject.c` — an earned negative for the slice. But
**three live uses tree-wide**, and one is production code:

```
Modules/posixmodule.c:17075   ScandirIteratorType_spec .flags  <-- production
Modules/_testmultiphase.c:309                                       test support
Modules/_testcapi/heaptype.c:891                                    test support
```

The two test-support sites are plausibly deliberate (they assert the flag is still accepted).
`Modules/posixmodule.c:17075` is not: it is `os.ScandirIterator`'s spec, and the flag has been a
no-op since 3.8. Setting it is harmless — bit 0 is otherwise unassigned; `_Py_TPFLAGS_STATIC_BUILTIN`
is bit 1 — so this is cosmetic, one deleted line. **CONSIDER**, and it is `Modules/`, i.e. a
different slice; recorded here for the slice that owns `posixmodule.c` and not claimed as an
`Objects/` finding.

*The scanner would have caught it.* `Py_TPFLAGS_HAVE_FINALIZE` is in the vocabulary with correct
metadata (`tier: soft`, `deprecated_in: 3.8`, `removed_in: ""`, `drop_in: false`, caveat present).
This is not a recall gap — it is a **scope** artifact: the run was scoped to one `Objects/` file
and the hit is in `Modules/`.

**`Py_TPFLAGS_HAVE_VERSION_TAG`** — `Include/object.h:578`, soft-deprecated at
`Doc/c-api/typeobj.rst:1519`, *"This macro does nothing."* **Zero** occurrences anywhere in the
tree outside its own definition and the docs. Earned negative; the bit must stay reserved because
old stable-ABI extensions set it (`Include/object.h:567-576` says so explicitly).

**`Py_TPFLAGS_VALID_VERSION_TAG`** — `Include/object.h:537`, commented `/* Unused. Legacy flag */`.
`Doc/c-api/typeobj.rst:1507-1510` carries a `.. warning::` — *"This flag is present in header
files, but is not be used. It will be removed in a future version of CPython"* (the `is not be
used` typo is upstream's). Zero uses. **But it has no `.. deprecated::` directive and no entry in
any `Doc/deprecations/` file**, so nothing tracks a flag whose own documentation promises its
removal. That is a small, concrete **POLICY** gap: either give it a `.. deprecated::` directive and
a `c-api-pending-removal-in-future.rst` line so it appears in the generated deprecation index, or
downgrade the warning to a plain note. Owner: docs / C API WG.

**`Py_TPFLAGS_HAVE_STACKLESS_EXTENSION`** — `Include/object.h:527-531`. `STACKLESS` is never
defined anywhere in CPython (only the two `#ifdef` tests at `Include/object.h:527` and
`Include/methodobject.h:119`), so it expands to `0`, which makes

```c
#define Py_TPFLAGS_DEFAULT  ( \
                 Py_TPFLAGS_HAVE_STACKLESS_EXTENSION | \
                0)
```

literally `(0 | 0)` in every build CPython ships. `typeobject.c` writes
`Py_TPFLAGS_DEFAULT | ...` at `:7310`, `:7644`, `:11314` and `:13046`, where the first term is
always zero. This is **not** a dead branch and **not** a cleanup target: `Py_TPFLAGS_DEFAULT` is
stable ABI since 3.2 (`Doc/data/stable_abi.dat:1000`) and the reservation is for a downstream
fork. Flagged **ACCEPTABLE** explicitly so a future reader does not "simplify" it.

### 2.4 `tp_getattr` / `tp_setattr` — deprecated in prose, permanent in the ABI, freshly re-exported by the new API — **POLICY**

`Doc/c-api/typeobj.rst:876`: *"This field is deprecated."* (`tp_getattr`), `:895` the same for
`tp_setattr`. Note carefully what this is **not**: there is no `.. deprecated::` directive, no
`Py_DEPRECATED`, and no `Doc/deprecations/` entry. Per the toolkit's own exclusion rule this is
the `PyDict_GetItem` shape — prose, not a deprecation — and I am **not** proposing them as
vocabulary entries (§6.1). They are in this report because the file maintains machinery for them.

What `Objects/typeobject.c` still carries for these two fields:

- `type_new_...` base check, `:4738-4741` — a base is treated as having attribute hooks if
  *either* the legacy or the modern slot is set.
- `inherit_slots`, `:8933-8940` — paired inheritance: `tp_getattr` is inherited only when both
  `tp_getattr` and `tp_getattro` are unset on the subtype, matching the documented "Group"
  inheritance rule at `Doc/c-api/typeobj.rst:882`.
- the four zeroing slotdefs, `:11586-11589` — see §3.2.

Live users in CPython's own code: **none**. There is not one `(getattrfunc)` or `(setattrfunc)`
cast in `Modules/`, `Objects/` or `Python/`. The only in-tree consumers are two demo/test
modules using the *slot ID* form: `Modules/_testmultiphase.c:119` and
`Modules/xxlimited_35.c:129`, both `Py_tp_setattr`.

`Py_tp_getattr` and `Py_tp_setattr` are stable ABI since 3.2 (`Doc/data/stable_abi.dat:1118`,
`:1135`), so the machinery above is permanent regardless of what the prose says.

**The POLICY item is what the new API did with them.** `PyType_FromSlots` (3.15) re-exports both
as first-class slot IDs — `Include/slots_generated.h:65` `#define Py_tp_getattr 57`, `:76`
`#define Py_tp_setattr 68` — and `Include/internal/pycore_slots_generated.h:539-540` / `:572-573`
write them into the new type:

```c
case Py_tp_getattr:
    ht->ht_type.tp_getattr = (getattrfunc)slot.sl_func;
```

`Python/slots.toml` carries no deprecation marking for either. So a brand-new API, introduced in
2025 and frozen into the stable ABI, offers unmarked first-class access to two fields the
reference documentation calls deprecated. The decision — *carry every legacy slot forward
unmarked for parity, or mark the deprecated ones in `slots.toml` so the generated tables can
surface them* — is a real one and belongs to the `PySlot` API owner. `slots.toml` already has the
mechanism: a `duplicates`/`nulls`-style `deprecated` classification per slot (§3.3), so marking
these two costs a data edit, not new machinery.

### 2.5 Earned negatives — things a from-memory sweep would have wrongly reported

Each of these was checked mechanically, not recalled:

- **`tp_print`.** Zero occurrences in `Include/` and zero in `Objects/typeobject.c`. The slot was
  removed and its offset reused for `tp_vectorcall_offset`; `git log -L` on the slotdef block
  bottoms out at `04dbf3b5ec6` *"Kill all uses and definitions of tp_print under Objects/"*.
  There are **no remnants**.
- **The old buffer protocol.** Zero occurrences of `bf_getreadbuffer`, `bf_getwritebuffer`,
  `bf_getcharbuffer` or `bf_getsegcount` in `Include/` or `Objects/`. `inherit_slots` copies only
  `bf_getbuffer` / `bf_releasebuffer`. Nothing to migrate.
- **Version-guarded dead code.** Zero `PY_VERSION_HEX` / `#if PY_` / `Py_LIMITED_API` guards in
  `Objects/typeobject.c`. The question "is there compat code for a version no longer supported?"
  has a mechanical negative answer for the preprocessor form.
- **`Py_TPFLAGS_HAVE_*` with dead branches.** Of the four `HAVE_` flags, two are live features
  (`HAVE_GC`, `HAVE_VECTORCALL`, both used non-trivially in this file), one is unreferenced
  (`HAVE_VERSION_TAG`), and one is a zero-valued reservation (`HAVE_STACKLESS_EXTENSION`).
  **No branch in this file is dead on a `HAVE_` flag.** The prompt's hypothesis — a feature flag
  now unconditionally true whose `else` arm is unreachable — does not occur here.

---

## 3. Slot compatibility shims

### 3.1 `_Py_SLOT_COMPAT_VALUE` — the slot-ID renumbering bridge is **LIVE**, not dead — **ACCEPTABLE**

`Include/slots_generated.h:6-10`:

```c
#if !defined(Py_LIMITED_API) || Py_LIMITED_API+0 >= _Py_PACK_VERSION(3, 15)
#define _Py_SLOT_COMPAT_VALUE(OLD, NEW) NEW
#else
#define _Py_SLOT_COMPAT_VALUE(OLD, NEW) OLD
#endif
```

Eight slots are dual-numbered (`:92-99`):

| slot | old ID | new ID |
|---|---:|---:|
| `Py_mod_create` / `Py_bf_getbuffer` | 1 | 84 / 88 |
| `Py_mod_exec` / `Py_bf_releasebuffer` | 2 | 85 / 89 |
| `Py_mod_multiple_interpreters` / `Py_mp_ass_subscript` | 3 | 86 / 90 |
| `Py_mod_gil` / `Py_mp_length` | 4 | 87 / 91 |

The cause is visible in `Python/slots.toml` entries `[1]`–`[4]`, which are `kind = 'compat'` with
`equivalents = {type='Py_bf_getbuffer', mod='Py_mod_create'}` and so on: IDs 1–4 meant *different
things* in `PyType_Slot` versus `PyModuleDef_Slot`, and the unified `PySlot` numbering had to
disambiguate them. The old IDs survive as `compat` entries that the iterator resolves by the
array's kind.

**This is exactly the shape §3 of the brief asks about — and it is not dead.** It fires for any
extension compiled against `Py_LIMITED_API < 3.15`, and the abi3 floor is 3.2. It will be live
for as long as abi3 is. Recorded so that a future reader does not mistake "CPython's own build
always takes the NEW arm" for "the OLD arm is unreachable": inside `Py_BUILD_CORE` it is indeed
always NEW, which makes the OLD arm invisible to every in-tree test and every compiler warning.

### 3.2 The four zeroing slotdefs for `tp_getattr` / `tp_setattr` — **ACCEPTABLE**, and permanent

`Objects/typeobject.c:11586-11589`, the first four rows of the 94-row `slotdefs[]` table:

```c
TPSLOT(__getattribute__, tp_getattr, NULL, NULL, ""),
TPSLOT(__getattr__,      tp_getattr, NULL, NULL, ""),
TPSLOT(__setattr__,      tp_setattr, NULL, NULL, ""),
TPSLOT(__delattr__,      tp_setattr, NULL, NULL, ""),
```

They are the **only four entries in the table with both `FUNCTION` and `WRAPPER` NULL**, and they
have no explanatory comment. What they do:

- `slotptr` (`:11795`) resolves them to `&type->tp_getattr` / `&type->tp_setattr` — direct
  `PyTypeObject` fields, never in a sub-struct — so the `ptr == NULL` early-out in
  `update_one_slot` (`:11928-11936`) does **not** fire and these rows are always processed.
- Each of the four names also appears later in the table bound to the *modern* slot:
  `:11600` `TPSLOT(__getattribute__, tp_getattro, _Py_slot_tp_getattr_hook, ...)`,
  `:11603` `TPSLOT(__getattr__, tp_getattro, ...)`, and the `tp_setattro` pair.
- So `update_one_slot` hits the case documented in its own header comment at `:11879-11884`:
  *"if the special method is a wrapper_descriptor with the correct name but the type has
  precisely one slot set for that name and that slot is not the one that we are updating, then
  NULL is put in the slot."*

The net effect: **defining `__getattribute__` / `__getattr__` / `__setattr__` / `__delattr__` in
Python clears the legacy char\*-based slot**, so a heap subclass of a legacy static type that
still has `tp_getattr` cannot end up dispatching through it. These four rows exist *solely*
because `tp_getattr`/`tp_setattr` exist.

They predate the file's modern history — `git log -L 11586,11589:Objects/typeobject.c` walks back
through the py3k merges to the pre-2007 era. And because `Py_tp_getattr`/`Py_tp_setattr` are
stable ABI (§2.4), the rows can never be removed. **ACCEPTABLE.** The only actionable item is a
two-line comment: four unexplained NULL/NULL rows at the head of the table that encode a
non-obvious interaction with a rule 300 lines away is the most opaque thing in an otherwise
well-built table (pass 1's M5 rated the slotdef builders the best-constructed macros in the file).

### 3.3 The deprecation that only fires for the API nobody uses — **POLICY** (the sharpest item in this report)

**The data.** `Python/slots.toml` has 111 slot entries. Their problem-handling classification:

| key | `allow` | `deprecated` | `reject` | (unset → default) |
|---|---:|---:|---:|---:|
| `duplicates` | 2 | **81** | 0 | 28 |
| `nulls` | 5 | **81** | 2 | 23 |

The file's own header comment (`Python/slots.toml:19-20`) defines the class:
*"'deprecated': issue a deprecation warning. Don't use for new slots. (typically, the problem was
disallowed in docs, but allowed in practice)"* — i.e. this is explicitly a transitional
classification.

**The gate.** `Python/slots.c:341-364` (NULL) and `:367-401` (duplicates). The `REJECT` arm raises
`SystemError`. The `DEPRECATED` arm raises `DeprecationWarning` — but only inside:

```c
if (it->states[0].slot_struct_kind == _PySlot_KIND_SLOT) {     /* :350 and :383 */
    PyErr_WarnFormat(PyExc_DeprecationWarning, ... );
}
else {
    MSG("unwanted NULL in legacy struct");                      /* :362 and :398 */
}
```

`_PySlot_KIND_SLOT` is the *new* `PySlot` array. `_PySlotIterator_InitLegacy`
(`Python/slots.c:59-63`) calls `init_with_kind(it, slots, kind, kind)` with the legacy kind, so a
`PyType_Spec` array is `_PySlot_KIND_TYPE` and a `PyModuleDef` array is `_PySlot_KIND_MOD` —
never `_PySlot_KIND_SLOT`. `Objects/typeobject.c:5294` is the call that puts every spec caller on
that path. And `MSG` is compiled out: `Python/slots.c:11-18` is `#if 0` / `#define MSG(...)`.

**Reproduced, not inferred.** On `~/projects/python_build_matrix/builds/debug-gil-nojit/python`
(the slot machinery is byte-identical between the build ref and the target ref), driving
`_testlimitedcapi.type_from_null_slot` (new API) against `_testlimitedcapi.type_from_null_spec_slot`
(legacy API) over eight `nulls = 'deprecated'` slot IDs:

```
 slot | PyType_FromSlots (new)  | PyType_Spec (legacy)
   10 | WARN   ok               | silent ok
   20 | WARN   ok               | silent ok
   30 | WARN   ok               | silent ok
   40 | WARN   ok               | silent ok
   55 | WARN   ok               | silent ok
   60 | WARN   ok               | silent ok
   70 | WARN   ok               | silent ok
   80 | WARN   ok               | silent ok
new-API paths that warned:     8/8
legacy-spec paths that warned: 0/8
```

**It is deliberate and locked in by a test — with a hole.**
`Lib/test/test_capi/test_slots.py:157-173`:

```python
else:
    ctx = self.assertWarnsRegex(DeprecationWarning, "NULL")
    ctx_old = contextlib.nullcontext()          # <-- :168
with ctx:
    _testlimitedcapi.type_from_null_slot(slot_number)
if slot_number < 95:
    with ctx_old:                                # <-- asserts NOTHING
        _testlimitedcapi.type_from_null_spec_slot(slot_number)
```

The legacy call is executed under `contextlib.nullcontext()` — it neither asserts a warning nor
asserts the absence of one. The exemption is exercised but **not pinned**: if someone made the
legacy path start warning, or start raising, this test would still pass. `test_repeat_warning`
(`:175`) and `test_repeat_error` (`:180`) cover duplicates for the new API only; there is no
legacy-duplicate coverage at all.

**It is undocumented.** `Doc/c-api/slots.rst:85` states flatly that pointer slots *"may not be
NULL"*, and `:30-31` that duplicate IDs *"may not occur in a single slots array"*. Neither
mentions that the implementation downgrades both to a `DeprecationWarning` for one API and to
silence for the other. There is no `.. deprecated::` directive for either rule, no
`Doc/deprecations/` entry, and no removal version. An extension author who trips
`"NULL value in slot Py_nb_add is deprecated"` has nothing to look up.

**The POLICY question.** *Is the legacy-struct exemption permanent or transitional?*

- **If permanent** — which is what `Doc/c-api/slots.rst:38` ("no plans to remove") implies —
  then the `deprecated` classification on 81 slots is decorative for 100 % of real-world callers,
  and the docs should say that NULL/duplicate slots remain accepted without warning through
  `PyType_Spec` indefinitely.
- **If transitional** — which is what `Python/slots.toml:19-20` implies ("Don't use for new
  slots") — then it needs a removal version, a `Doc/deprecations/` entry, and a decision about
  whether legacy callers ever start warning. Note the sequencing trap: warnings can only bite
  legacy callers *after* a migration path exists, and per §2.1 the migration has not started.

Either answer is fine. What is not fine is that the two authoritative documents currently imply
opposite ones. **Owner: the `PySlot` API author / C API WG.** Cost of resolving: a docs paragraph
plus, if transitional, one `Doc/deprecations/c-api-pending-removal-in-*.rst` line.

Secondary, cheap: replace `contextlib.nullcontext()` at `test_slots.py:168` with an explicit
`assertNoWarns`-style check so the exemption is pinned rather than merely exercised.

---

## 4. Incomplete migrations

The lane's highest-value shape is a migration that landed on some call sites and not others.

**Found: one, and it is total rather than partial.** §2.1 — `PyType_FromSpec` family →
`PyType_FromSlots`, 0 of 173 internal call sites converted, replacement available since 3.15,
deprecation landed 3.16. This is the `PyModule_AddObject` → `PyModule_AddObjectRef` shape at
maximum amplitude: not "landed on some sites", but "landed on none". It is the single most
actionable item in this report and it is a POLICY decision, not a bug.

**Found: one enforcement asymmetry.** §3.3 — the NULL/duplicate slot deprecation is implemented,
tested and shipped for the new API and silently exempted for the old one. The *mechanism* is
complete; its *reach* is zero.

**Checked and not found:**

- `Py_tp_base` → `Py_tp_bases` (§2.2): 3 remaining uses, all in demo/test modules that exist to
  exercise the old surface. Not an incomplete migration.
- Legacy `tp_flags` bits (§2.3): `HAVE_VERSION_TAG` and `VALID_VERSION_TAG` are at zero uses —
  those migrations are *complete*. `HAVE_FINALIZE` has 3 remaining, 1 production.
- `tp_print` and the old buffer protocol (§2.5): complete, no remnants.
- Preprocessor-guarded compat blocks: none exist in this file.

**Cross-reference with the history agent — one candidate, and it is NOT mine.**
`preflight/history_context.md` §4.3 identifies `e7eaed56149a` (2026-05-23, gh-148450) as a fix
that landed on `_PyType_SetFlagsRecursive` (`Objects/typeobject.c:6506`) and not on its sibling
`_PyType_SetFlags` (`:6453`), 53 lines away, called from the same module (`Modules/_abc.c:601`
vs `:541`). I confirm the asymmetry is real by reading both function bodies — the recursive one
calls `_PyType_Modified_Unlocked` before stopping the world, the non-recursive one does not
invalidate the version tag at all.

**But this is not a deprecation-lane finding.** It is a free-threading / cache-invalidation
fix-propagation gap, and the history document already routes it to **ft-race-scanner** and
**lock-discipline-checker**. I am recording the confirmation and handing it off rather than
claiming it, because deciding whether it is exploitable requires knowing whether a type can hold
a cached version tag when `_abc.c:541` runs — a question for those agents, not this one.

---

## 5. Classification summary

| # | Finding | Site | Class |
|---|---|---|---|
| 1 | `PyEval_GetGlobals` — correct as written; CPython calling its own soft-deprecated API | `Objects/typeobject.c:4485` | **POLICY** (confirms pass 1) |
| 2 | `PyType_FromSpec` family soft-deprecated in 3.16; **0 of 173** internal call sites migrated; two of the four can never be removed (stable ABI) | `Objects/typeobject.c:5808-5831`; `Doc/c-api/type.rst:827/858/884/909` | **POLICY** |
| 3 | NULL-slot / duplicate-slot deprecation (81 of 111 slots) fires **only** for `PyType_FromSlots`; every legacy spec caller is silently exempt. Reproduced 8/8 vs 0/8. Undocumented; the two authoritative docs imply opposite intents | `Python/slots.c:350`, `:383`; `Python/slots.toml:19-20`; `Doc/c-api/slots.rst:30-38`, `:85` | **POLICY** |
| 4 | `Py_tp_getattr` / `Py_tp_setattr` re-exported unmarked by the 3.15 `PySlot` API although the reference docs call the fields deprecated | `Include/slots_generated.h:65`, `:76`; `Doc/c-api/typeobj.rst:876`, `:895` | **POLICY** |
| 5 | `Py_TPFLAGS_VALID_VERSION_TAG` — docs promise removal, but no `.. deprecated::` directive and no `Doc/deprecations/` entry, so nothing tracks it | `Include/object.h:537`; `Doc/c-api/typeobj.rst:1507-1510` | **POLICY** (docs) |
| 6 | `Py_TPFLAGS_HAVE_FINALIZE` set on a production type spec; no-op since 3.8, pending removal, **not** stable ABI so genuinely removable | `Modules/posixmodule.c:17075` | **CONSIDER** (out of slice) |
| 7 | `test_slots.py:168` uses `contextlib.nullcontext()` for the legacy path — the exemption is exercised but not pinned; no legacy-duplicate coverage at all | `Lib/test/test_capi/test_slots.py:157-182` | **CONSIDER** |
| 8 | The four NULL/NULL `tp_getattr`/`tp_setattr` slotdefs are correct but unexplained; they encode an interaction with a rule 300 lines away | `Objects/typeobject.c:11586-11589` | **ACCEPTABLE** (comment would help) |
| 9 | `_Py_SLOT_COMPAT_VALUE` slot-ID bridge — **live**, needed for `Py_LIMITED_API < 3.15`, invisible to in-tree builds | `Include/slots_generated.h:6-10`, `:92-99` | **ACCEPTABLE** |
| 10 | `Py_tp_base` (soft-deprecated 3.15) — 3 uses, all demo/test | `Modules/_testmultiphase.c:373`, `Modules/xxlimited_35.c:194`/`:215` | **ACCEPTABLE** |
| 11 | `Py_TPFLAGS_DEFAULT` is literally `(0\|0)` in every shipped build; stable ABI, do not "simplify" | `Include/object.h:527-531`, `:560-562` | **ACCEPTABLE** |
| 12 | `_PyType_SetFlags` vs `_PyType_SetFlagsRecursive` version-tag asymmetry — confirmed, **handed off** to ft-race / lock-discipline | `Objects/typeobject.c:6453` vs `:6506` | **hand-off, not this lane** |
| — | `tp_print`, old buffer protocol, `Py_TPFLAGS_HAVE_VERSION_TAG`, preprocessor version guards | — | **earned negatives** |

**Zero FIX findings in `Objects/typeobject.c`.** Consistent with pass 1, and expected: this file
is the *provider* of the compatibility surface, not a consumer of it. The value of running this
lane here was inverting the question, exactly as the brief directed.

---

## 6. Toolkit assessment

### 6.1 Vocabulary — four proposed additions, each with a citable directive

All four have a real `.. soft-deprecated::` / `.. deprecated::` reST directive, which
`data/deprecated_c_apis.json`'s own `_meta.method` names as an accepted source. **None of them is
the `PyDict_GetItem` shape** the `deliberately_excluded` list warns about — I checked each for a
directive rather than prose, and deliberately did **not** propose `tp_getattr`/`tp_setattr`
(§2.4), which carry prose only and would be exactly that false positive.

```jsonc
{ "name": "PyType_FromSpec", "tier": "soft", "deprecated_in": "3.16",
  "removed_in": "", "replacement": "PyType_FromSlots",
  "drop_in": false,
  "caveat": "NOT a rename. PyType_FromSlots takes a PySlot[] array (uint16 sl_id + tagged union), not a PyType_Spec; name/basicsize/itemsize/flags move from struct fields into Py_tp_name / Py_tp_basicsize / Py_tp_itemsize / Py_tp_flags slots, and those slot IDs are REJECTED with SystemError if passed through a spec (Objects/typeobject.c:5305-5313, the NO_SPEC macro). Migration is a rewrite of the type definition, not an edit. Note also that the new API enforces deprecations the old one exempts: NULL and duplicate slot values raise DeprecationWarning under PyType_FromSlots and are silent under PyType_Spec (Python/slots.c:350, :383), so a migrated call site may start warning on data that was previously accepted.",
  "compat_shim_files": ["Modules/_testlimitedcapi/slots.c", "Modules/xxlimited_35.c", "Modules/_testmultiphase.c"],
  "notes": "Doc/c-api/type.rst:909 `.. soft-deprecated:: next` (= 3.16). STABLE ABI since 3.2 (Doc/data/stable_abi.dat:784) -- can never be removed; Doc/c-api/slots.rst:38 says so outright. 47 live internal call sites at 4f3be1b5, 173 across the whole family, 259 PyType_Spec objects tree-wide, and ZERO internal PyType_FromSlots callers outside test support." }

{ "name": "PyType_FromSpecWithBases", "tier": "soft", "deprecated_in": "3.16",
  "removed_in": "", "replacement": "PyType_FromSlots",
  "drop_in": false,
  "caveat": "Same rewrite as PyType_FromSpec; the `bases` argument becomes a Py_tp_bases slot, which needs a live tuple and therefore a stack-allocated PySlot array plus Py_slot_subslots for the static part (Doc/c-api/type.rst:433-452).",
  "compat_shim_files": [],
  "notes": "Doc/c-api/type.rst:884. STABLE ABI since 3.3 (Doc/data/stable_abi.dat:785). 12 live internal call sites." }

{ "name": "PyType_FromModuleAndSpec", "tier": "soft", "deprecated_in": "3.16",
  "removed_in": "", "replacement": "PyType_FromSlots",
  "drop_in": false,
  "caveat": "Same rewrite; the `module` argument becomes a Py_tp_module slot and `bases` a Py_tp_bases slot. Both require live objects, so the slot array cannot stay fully static.",
  "compat_shim_files": [],
  "notes": "Doc/c-api/type.rst:858. Not in the stable ABI. 94 live internal call sites -- the single most-used member of the family." }

{ "name": "PyType_FromMetaclass", "tier": "soft", "deprecated_in": "3.16",
  "removed_in": "", "replacement": "PyType_FromSlots",
  "drop_in": false,
  "caveat": "Same rewrite; `metaclass` becomes a Py_tp_metaclass slot. Of the four, this is the only one that is NOT stable ABI and the only one added recently (3.12), so it is the one whose soft deprecation a maintainer might reasonably contest.",
  "compat_shim_files": [],
  "notes": "Doc/c-api/type.rst:827. Added 3.12, soft-deprecated 3.16. 20 live internal call sites." }
```

Two smaller ones, both currently at **zero** occurrences — worth adding as *canaries* rather than
as expected hits, so a regression is caught:

```jsonc
{ "name": "Py_TPFLAGS_HAVE_VERSION_TAG", "tier": "soft", "deprecated_in": "3.13",
  "removed_in": "", "replacement": "",
  "drop_in": false,
  "caveat": "The macro does nothing; delete the flag from the tp_flags expression. The BIT must stay reserved (Include/object.h:567-576: old stable-ABI extensions set it), so this is a call-site cleanup only, never a header change.",
  "compat_shim_files": [],
  "notes": "Doc/c-api/typeobj.rst:1519 `.. soft-deprecated:: 3.13`. Include/object.h:578. ZERO occurrences tree-wide at 4f3be1b5 -- add as a regression canary, not for expected hits." }

{ "name": "Py_tp_base", "tier": "soft", "deprecated_in": "3.15",
  "removed_in": "", "replacement": "Py_tp_bases",
  "drop_in": false,
  "caveat": "NOT a rename: Py_tp_bases takes a TUPLE of bases, Py_tp_base a single type (Doc/c-api/type.rst:696-698 -- 3.15 also made Py_tp_base itself accept a tuple, so the two now overlap). If both appear, Py_tp_bases wins regardless of array order (Objects/typeobject.c:5331-5338). STABLE ABI since 3.2 (Doc/data/stable_abi.dat:1104) -- permanent.",
  "compat_shim_files": ["Modules/_testmultiphase.c", "Modules/xxlimited_35.c"],
  "notes": "Doc/c-api/type.rst:700 `.. soft-deprecated:: 3.15`. 3 live uses at 4f3be1b5, all in demo/test modules." }
```

**Deliberately NOT proposed**, and the reasons belong in `_meta.deliberately_excluded`:

- **`PyTypeObject.tp_getattr` / `tp_setattr`** — `Doc/c-api/typeobj.rst:876`/`:895` say *"This
  field is deprecated"* in **prose**, with no `.. deprecated::` directive, no `Py_DEPRECATED`, and
  no `Doc/deprecations/` entry. This is the same class as `PyDict_GetItem`. They are also
  struct-field names, so a word-boundary matcher would hit `inherit_slots`' own inheritance logic
  and the four slotdef rows in `typeobject.c` — the definition site, i.e. the exact false-positive
  class the vocabulary was rebuilt to eliminate.
- **`Py_TPFLAGS_VALID_VERSION_TAG`** — `Doc/c-api/typeobj.rst:1507-1510` is a `.. warning::`, not a
  directive, and there is no `Doc/deprecations/` entry (that gap is finding #5). Zero occurrences,
  so adding it buys nothing until it is properly deprecated upstream.
- **`Py_TPFLAGS_HAVE_STACKLESS_EXTENSION`** — not deprecated at all; a zero-valued reservation for
  a downstream fork.

### 6.2 Envelope — surface the real denominator for a line-based rule

`scan_deprecated_apis.py:480` hard-codes `functions_analyzed=0`. The RUN_CONTEXT rule
("a zero next to a zero denominator is silence, not safety") applies to the report's *reader*, and
here the zero is a constant, not a canary failure. Proposal: for line-based rules emit
`functions_analyzed: null` and add the rule's actual denominator to `denominators`:

```jsonc
"denominators": {
  "files_analyzed": 1,
  "functions_analyzed": null,          // rule is line-based; not a measurement
  "lines_scanned": 13068,
  "apis_in_vocabulary": 93,
  "api_file_pairs_checked": 93,        // the real denominator
  "findings": 1
}
```

`apis_in_vocabulary` and `apis_with_hits` are already in `summary`; promoting them into
`denominators` and dropping the misleading `functions_analyzed: 0` makes the 1/93 ratio legible
without a footnote.

### 6.3 A rule this lane cannot see, and what it would cost

Everything in §2 and §3 was found by **reading**, not by the scanner, and the reason is
structural: `scan_deprecated_apis` matches *API-name tokens in C source*. It cannot see

- a `.. soft-deprecated::` directive attached to a function this file **defines**;
- a deprecation declared in **data** (`Python/slots.toml`) and enforced in a **different file**
  (`Python/slots.c`);
- a migration whose evidence is a **count ratio** (0 `PyType_FromSlots` vs 173 spec calls) rather
  than any single line.

The cheapest genuine improvement is not a new scanner but a **vocabulary build step**: parse
`Doc/c-api/*.rst` for every `.. deprecated::` / `.. soft-deprecated::` directive and diff the
resulting set against `data/deprecated_c_apis.json`. On this tree that diff would have surfaced
all six §6.1 candidates mechanically, and it is the same mechanism `_meta.method` already claims
to use — just automated, so it does not drift between releases. A second cheap check: join that
set against `Doc/data/stable_abi.dat`, because *stable ABI + deprecated* is a distinct and
important state (permanent compat code, `removed_in` structurally must stay empty) that the
schema currently cannot express. Four of the six candidates above are in it.

Finally, one **corpus-level metric** worth adding to the migration lane, since it is what actually
answered the brief's question 4: for any entry with a `replacement`, report
`count(replacement) / (count(api) + count(replacement))` tree-wide. For `PyType_FromSpec` that is
`0 / 173` and it is the whole finding.
