# recursion-guard-auditor — Objects/ sample (informed)

Shape owned: **`unguarded-recursion-in-slot`** (briefing §1).
Target: `/home/danzin/projects/cpython` @ `4f3be1b5777`.
Differentials run on the locally built ASan CPython 3.16.0a0 (`./python`, `583cb03da83`);
line numbers in this report are **HEAD** line numbers, ASan frames are from the built binary.

## Scanner volume

raw candidates in sample: **4**  |  confirmed: **3**  |  dismissed: **1**  |  precision: **75%**
new sites found by reading that the scanner did **not** surface: **6** (5 in-sample, 1 scope escape)
crashes reproduced live (SIGSEGV / ASan stack-overflow, not `RecursionError`): **7**

---

## The one fact that organises every finding below

Of the four element-descent dispatchers, **exactly one has no recursion guard**:

| dispatcher | guard | site |
|---|---|---|
| `PyObject_Repr` | `_Py_EnterRecursiveCallTstate(" while getting the repr of an object")` | `Objects/object.c:780` |
| `PyObject_Str` | `_Py_EnterRecursiveCallTstate(" while getting the str of an object")` | `Objects/object.c:823` |
| `PyObject_RichCompare` | `_Py_EnterRecursiveCallTstate(" in comparison")` | `Objects/object.c:1110` |
| `PyObject_RichCompareBool` | delegates to `PyObject_RichCompare` | `Objects/object.c:1121` |
| **`PyObject_Hash`** | **none** — dispatches straight to `tp_hash` | **`Objects/object.c:1158-1162`** |

```c
Py_hash_t
PyObject_Hash(PyObject *v)
{
    PyTypeObject *tp = Py_TYPE(v);
    if (tp->tp_hash != NULL)
        return (*tp->tp_hash)(v);          /* <-- no Py_EnterRecursiveCall */
```

Consequence, and it is decisive for both precision and recall:
**a `tp_repr` / `tp_str` / `tp_richcompare` descent is guarded by construction; a `tp_hash`
descent is not.** Every confirmed FIX below descends through `PyObject_Hash`; the single
false positive descends through `PyObject_RichCompare`. This is the guarded twin for the
whole class, and it lives one level up from the slot.

---

## Findings

### Confirmations (catalog — one line each, per informed rule 1)

- **CPY-0001 `tuple_hash`** — `Objects/tupleobject.c:372`, unguarded `PyObject_Hash(item[i])` at `:385`. **Still present, reproduced live**: 1M-deep tuple → `AddressSanitizer: stack-overflow`, ~unbounded `tuple_hash` frames at `tupleobject.c:385`. FIX.
- **CPY-0002 `_Py_make_parameters`** — `Objects/genericaliasobject.c:186`, self-call at `:231`. **Still present, reproduced live**: `list[<300k-deep tuple containing a TypeVar>].__parameters__` → stack-overflow, unbounded `_Py_make_parameters` frames at `:231`. FIX. **Its recorded reachability is wrong — see next section.**

### CPY-0002 reachability must be widened — CONFIRMED, both call sites

The catalog records CPY-0002 as reachable via `list[...]`. Phase 1 flagged the wider path;
I confirmed **both** union entry points statically and dynamically.

Static call path (all at HEAD):

| site | code |
|---|---|
| `Objects/genericaliasobject.c:186` | `_Py_make_parameters` (self-recursive at `:231`) |
| `Objects/genericaliasobject.c:406` | `_Py_subs_parameters` (**also self-recursive**, at `:482` — `// Recursively substitute params in lists/tuples.`) |
| `Include/internal/pycore_unionobject.h:18-19` | both declared `extern` |
| `Objects/unionobject.c:332` | `union_init_parameters` → `alias->parameters = _Py_make_parameters(alias->args);` |
| `Objects/unionobject.c:349` | `union_getitem` → `_Py_subs_parameters(self, alias->args, alias->parameters, item)` |

Dynamic confirmation (the tuple-hash cache is primed bottom-up so union *construction*
survives and the crash isolates cleanly onto the parameter walk):

```python
T = typing.TypeVar("T"); x = T
for _ in range(300_000):
    x = (x,); hash(x)          # prime tuple_hash cache so `int | ...` is O(1)
u = int | list[x]              # type(u) is typing.Union
u.__parameters__               # -> SIGSEGV
```
→ `AddressSanitizer: stack-overflow`, frames `_Py_make_parameters genericaliasobject.c:231` ×N,
entered from `union_init_parameters`. `u[str]` (via `union_getitem`, `unionobject.c:349`) crashes
identically.

**Verdict: yes, CPY-0002 needs its reachability updated.** It is reachable from
`X | Y` / `typing.Union[...]` attribute access (`.__parameters__`) *and* subscription
(`U[str]`), not only `list[...]`. Practically this promotes it: `typing.Union` is a far
more common surface than a hand-built generic alias, and both `unionobject.c` call sites
are in the **INTERNAL**-tier file the include-graph put at `sym`=6 — i.e. fan-in metrics
badly understate it. A fix at the `_Py_make_parameters` / `_Py_subs_parameters` definitions
covers all four entry points at once; a fix at `ga_parameters` alone would not.

### FIX — new siblings the scanner did not surface

#### [FIX] `ga_hash` descends both alias fields with no guard — `Objects/genericaliasobject.c:611` (`:615`, `:619`)
```c
ga_hash(PyObject *self)
{
    gaobject *alias = (gaobject *)self;
    Py_hash_t h0 = PyObject_Hash(alias->origin);   // :615
    ...
    Py_hash_t h1 = PyObject_Hash(alias->args);     // :619
```
**Guarded twin**: `ga_repr` (`:90`) descends via `_Py_typing_type_repr` → `PyObject_Repr`, which
*is* dispatcher-guarded; `ga_hash` has no equivalent.
**Reachability**: `x = int; for _ in range(300_000): x = list[x]; hash(x)`.
**Confirmed**: SIGSEGV, alternating frames `ga_hash genericaliasobject.c:618` / `tuple_hash tupleobject.c:385`.
**Why the scanner missed it**: fixed-arity descent — no loop, no `*_GET_ITEM`, so the
`element_descent` gate (`scan_recursion_guards.py:152-155`) is never satisfied. Arity is
irrelevant to depth: each field is an arbitrary Python object.

#### [FIX] `weakref_hash` descends into the referent with no guard — `Objects/weakrefobject.c:190` (`:199`), slot at `:205`
```c
weakref_hash_lock_held(PyWeakReference *self)      // :190
{   ...
    self->hash = PyObject_Hash(obj);               // :199  -- obj is the referent
}
weakref_hash(PyObject *op)                         // :205  <- the actual tp_hash
{   ...
    Py_BEGIN_CRITICAL_SECTION(self);
    hash = weakref_hash_lock_held(self);
    Py_END_CRITICAL_SECTION();
```
**Guarded twin**: `weakref_richcompare` (`weakrefobject.c`) descends via `PyObject_RichCompare` → guarded.
Same file, same descent, one is protected and the hash is not — the exact CPY-0001 asymmetry.
**Reachability**: a plain `weakref.ref` cannot be a referent, but a **subclass that declares
`__weakref__` can**, so the chain is unbounded:
```python
class R(weakref.ref):
    __slots__ = ("__weakref__",)
r = R(obj)
for _ in range(300_000): r = R(r)   # keep strong refs
hash(r)                              # -> SIGSEGV
```
**Confirmed**: stack-overflow, frames `weakref_hash_lock_held weakrefobject.c:199` /
`weakref_hash weakrefobject.c:210` repeating.
**Secondary (FT, not my class — hand to ft-race-scanner)**: the whole recursive descent, including
arbitrary user `__hash__`, executes **inside `Py_BEGIN_CRITICAL_SECTION(self)`**, and one
per-object lock is held at every level of the chain.
**Why the scanner missed it**: *two* independent gaps — (a) fixed-arity, no loop; (b) the descent
lives in `weakref_hash_lock_held`, whose name matches no `_SLOT_NAME_SUFFIX` entry and no slot-table
entry, so `_slot_for()` returns `None` and the function is never even considered.

#### [FIX] `unionbuilder_add_single_unchecked` hashes each union member with no guard — `Objects/unionobject.c:168` (`:170`)
```c
unionbuilder_add_single_unchecked(unionbuilder *ub, PyObject *arg)
{
    Py_hash_t hash = PyObject_Hash(arg);   // :170
```
**Reachability**: `int | <deeply nested generic alias>` crashes at **construction time**, before
any user code touches the result — `x = int; for _ in range(300_000): x = list[x]; int | x`.
**Confirmed**: stack-overflow with `ga_hash`/`tuple_hash` frames, before the union object exists.
**Why the scanner missed it**: fixed-arity + the name maps to no slot.

#### [FIX] `union_hash` — `Objects/unionobject.c:46` (`:57`, `:67`) — scanner TP, confirmed
```c
    return PyObject_Hash(alias->hashable_args);   // :67  (the unbounded one)
```
The scanner flagged this on the `:57` loop over `unhashable_args`; the load-bearing descent is
actually the **unguarded tail at `:67`**, outside that loop. Confirmed: `hash(typing.Union[<deep alias>, str])`
→ SIGSEGV. Honest scoping: `is_unionable` (`unionobject.c`) admits only `None`/type/Sentinel/
GenericAlias/Union/TypeAliasType, and nested unions flatten — so `union_hash` contributes **O(1)
frames per level**, and the unbounded depth comes from the `ga_hash`/`tuple_hash` members it
descends into. It is still the same unguarded-`PyObject_Hash`-in-`tp_hash` shape and takes the
same fix.

#### [FIX] `_Py_subs_parameters` self-recurses with no guard — `Objects/genericaliasobject.c:406` (`:482`)
Second, independent self-recursion in the parameter-walk family (the scanner folded it into
CPY-0002 as a `duplicate_location`, so it has no finding of its own). Reached from
`unionobject.c:349` and from `ga_getitem`. Confirmed via `(int | list[<deep tuple>])[str]`.

### FIX — scope escape (outside the 14-file sample, chased per assignment)

#### [FIX] `frozendict_pair_hash` — `Objects/dictobject.c:8415` (`:8427`), driven by `frozendict_hash` at `:8447`
The copy-paste `tupleobject.c:367` now formally names (commit `244300162d2`, 2026-05-20:
*"If you update this code, update also frozendict_pair_hash() which copied this code."*).
The comment was added; **the guard was added in neither**.
```c
// Compute hash((key, value)).
// Code copied from tuple_hash().
static Py_hash_t
frozendict_pair_hash(Py_hash_t key_hash, PyObject *value)   // :8415
{   ...
    lane = PyObject_Hash(value);                            // :8427  <- unguarded descent
```
`frozendict_hash` (`:8447`) calls it once per entry in a `_PyDict_Next` loop.
**Reachability**: `d = frozendict({1: 2})` then `d = frozendict({1: d})` ×500k, `hash(d)`.
**Confirmed**: SIGSEGV, alternating frames `frozendict_hash` / `frozendict_pair_hash`.
**Fix propagation**: whatever guard lands on `tuple_hash` for CPY-0001 must land here in the
same patch — this is the sibling the sample boundary would have hidden, and it is a *new* type
(frozendict is 3.15/3.16-era), so it is not covered by any existing tracker issue for gh-154318's
original site.

### CONSIDER

#### [CONSIDER] `mappingproxy_hash` — `Objects/descrobject.c:1204` (`:1207`)
```c
mappingproxy_hash(PyObject *self)
{
    mappingproxyobject *pp = (mappingproxyobject *)self;
    return PyObject_Hash(pp->mapping);     // :1207
}
```
Source-level this is the same unguarded shape, and mappingproxies **do** nest without flattening
(verified: `repr(M(M(M(H()))))` → `mappingproxy(mappingproxy(mappingproxy(...)))`), so the receiver
is genuinely unbounded. **But I could not produce a crash**: at 3,000,000 levels deep
`hash(p)` returns normally. The descent is a bare tail call and clang -O eliminated it into a
loop. **CONSIDER, not FIX**: the safety here is a compiler optimisation, not a guard, and it does
not survive a `-O0` debug build, a different compiler, or any future edit that makes the call
non-tail (e.g. mixing in the proxy's own identity, as `wrapper_hash` does). Reported honestly
rather than promoted — I did not reproduce it and I am not claiming it.

### Dismissed — false positive

#### `tuple_richcompare` — `Objects/tupleobject.c:755` — **guarded by the dispatcher**
Descends via `PyObject_RichCompare` / `PyObject_RichCompareBool`, both of which enter through
`PyObject_RichCompare` (`object.c:1099`), which wraps the descent in
`_Py_EnterRecursiveCallTstate(tstate, " in comparison")` at `object.c:1110`.
This is FP class *"Guarded by the dispatcher"* from `data/cpython_non_bugs.md`, and per the
taxonomy I confirmed the call graph rather than dismissing on the pattern alone: two independent
1,000,000-deep tuples compared with `a == b` raise a clean **`RecursionError`**, no SIGSEGV.

---

## New siblings of known shapes (the fix-propagation yield)

| # | site | shape | in sample? | crash reproduced | scanner surfaced? |
|---|---|---|---|---|---|
| 1 | `Objects/genericaliasobject.c:615,619` `ga_hash` | unguarded `PyObject_Hash`, fixed arity | yes | SIGSEGV | **no** |
| 2 | `Objects/weakrefobject.c:199` `weakref_hash_lock_held` | unguarded `PyObject_Hash`, helper-hidden | yes | SIGSEGV | **no** |
| 3 | `Objects/unionobject.c:170` `unionbuilder_add_single_unchecked` | unguarded `PyObject_Hash` at construction | yes | SIGSEGV | **no** |
| 4 | `Objects/genericaliasobject.c:482` `_Py_subs_parameters` | second self-recursive parameter walk | yes | SIGSEGV | folded as a duplicate |
| 5 | `Objects/unionobject.c:332,349` | **CPY-0002 reachable via `typing.Union`** | yes | SIGSEGV | n/a (reachability) |
| 6 | **`Objects/dictobject.c:8427` `frozendict_pair_hash`** | the formalized `tuple_hash` copy-paste | **scope escape** | SIGSEGV | **no** — see R1 |
| 7 | `Objects/descrobject.c:1207` `mappingproxy_hash` | unguarded, tail-call-elided | yes | not reproduced | **no** |

**Out-of-sample leads, not chased to a verdict** (from the tree-wide sweep in R2, listed so a
later run does not have to re-derive them): `Objects/classobject.c:280` `method_hash`
(chained `types.MethodType` did **not** crash at 300k — unconfirmed, do not report as a bug),
`Objects/codeobject.c:3196` `hash_const`, `Python/hamt.c:394` `hamt_hash`,
`Modules/_sqlite/row.c:231` `pysqlite_row_hash`, `Modules/_sre/sre.c:3301` `pattern_hash`.

---

## Classes bounded (clean negatives, with the bound stated)

Per the taxonomy I owed a *bound*, not a hand-wave, for each exemption.

**Exemption (a) — guarded by the dispatcher. Bound = the interpreter recursion limit.**
Every `tp_repr` / `tp_str` / `tp_richcompare` in the 14-file sample descends *only* through
`PyObject_Repr` (`object.c:780`), `PyObject_Str` (`:823`) or `PyObject_RichCompare` (`:1110`) —
including the indirect spellings `%R` in `PyUnicode_FromFormat` and
`PyUnicodeWriter_WriteRepr` (`Objects/unicode_writer.c:384`, which calls `PyObject_Repr` at `:393`).
Confirmed clean, with the descent route:
`structseq.c:585` `structseq_repr` (→ `PyUnicodeWriter_WriteRepr`) ·
`templateobject.c:396` `template_repr` and `interpolationobject.c:157` `interpolation_repr`
(both `%R`; **t-string nesting `t"{t'{x}'}"` reaches repr only**) ·
`capsule.c:360` · `lazyimportobject.c:149` · `funcobject.c` `func_repr`/`cm_repr`/`sm_repr` ·
`cellobject.c:182` `cell_repr` and `cell_richcompare` (→ `cell_compare_impl` → `PyObject_RichCompare`;
cells are freely cyclic, and the dispatcher is the only thing that saves them) ·
`descrobject.c` `method_repr`/`member_repr`/`getset_repr`/`wrapperdescr_repr`/`mappingproxy_repr`/
`mappingproxy_str`/`mappingproxy_richcompare`/`wrapper_richcompare` ·
`weakrefobject.c` `weakref_richcompare`/`proxy_repr`/`proxy_str`/`proxy_richcompare` ·
`genericaliasobject.c:90` `ga_repr`, `:712` `ga_richcompare` · `unionobject.c` `union_repr`, `union_richcompare` ·
`odictobject.c:1436` `odict_repr` (belt-and-braces: an explicit `Py_ReprEnter` at `:1448` *and* the
dispatcher) and `odict_richcompare` (→ `PyDict_Type.tp_richcompare`; scope escape into
`dictobject.c`, but the same guarded dispatcher).

**Exemption (b) — non-nestable receiver / non-descending slot. Bound stated per entry.**
- `descrobject.c:1478` `wrapper_hash` — `PyObject_GenericHash(wp->self)` + `Py_HashPointer(wp->descr)`. **Bound: 0 recursive descents** (both are identity hashes).
- `templateobject.c`, `interpolationobject.c`, `capsule.c`, `lazyimportobject.c`, `structseq.c`, `funcobject.c` — define **no `tp_hash`**, so they inherit `object`'s pointer hash. **Bound: 0.**
- `odictobject.c` — `tp_hash = 0` at `:1597`, inheriting dict's `PyObject_HashNotImplemented`. **Bound: 0** (an OrderedDict is unhashable).
- `iterobject.c` — all four slots are `0` at `:162-176`, `:288-302`, `:509-523`. **Bound: n/a**, no recursion-prone slot exists.
- `structseq.c` — no own `tp_hash`; a structseq is a tuple subclass, so `hash(os.stat_result((deep,...)))` lands in `tuple_hash`. **Not a new site** — inherited reach of CPY-0001, and fixing `tuple_hash` fixes it.
- The `PyObject_Hash(key)` sites in `odictobject.c:633,1130,1655,1676`, `setobject.c:600,611,622`, `dictobject.c:3053` etc. are **pass-through** hashes of a single caller-supplied key: they add **exactly 1 C frame** and the depth is the argument's own. Not additive per level → **ACCEPTABLE**, and correctly not reported by the scanner.

**Genuine negative for the sample's newest code**: `lazyimportobject.c` (157 lines, 0 bug-fix commits ever) and `capsule.c` are clean for this class with a bound of 0 — no hash slot, repr dispatcher-guarded. Reported as a confident negative, not padding.

---

## Toolkit assessment

### Precision

3 of 4 = **75%**, and the single FP is not a judgment call — it is mechanically decidable.

| candidate | verdict | FP class |
|---|---|---|
| `_Py_make_parameters` `:231` | TP (CPY-0002) | — |
| `tuple_hash` `:385` | TP (CPY-0001) | — |
| `union_hash` `:57` | TP (right function, wrong line — the unbounded descent is `:67`, outside the loop it matched on) | — |
| `tuple_richcompare` `:755` | **FP** | *Guarded by the dispatcher* (`data/cpython_non_bugs.md`) |

The FP is systematic, not incidental: **every** `tp_richcompare`/`tp_repr`/`tp_str`
element-descent finding this rule can ever emit is guarded-by-dispatcher unless the slot is
reached by a non-dispatcher route. The rule's `confidence: medium` for repr/str is aimed at
the right instinct but implements it as a severity haircut instead of a suppression, and it
still rates `tp_richcompare` `high`.

### Recall gaps

**R1 — CHASSIS, and the highest-value finding in this report. A tree-sitter parse ERROR silently truncates whole files for *all 12 scanners*.**

`extract_functions` stops at an `ERROR` node. In `Objects/dictobject.c` the ERROR spans
**lines 5268–8597**: the last function extracted is at `:5239`, and the file is 8,598 lines.
**3,359 lines — 39% of the file — are invisible to every scanner in the toolkit.** Measured:

```
>>> fns = extract_functions(parse_bytes(open('Objects/dictobject.c','rb').read()), src)
187 functions extracted; last at line 5239
'frozendict_pair_hash' in names -> False
'frozendict_hash'      in names -> False
'frozendict_repr'      in names -> False
```

That is precisely why `frozendict_pair_hash` — my assigned scope escape, the formalized
copy-paste twin of CPY-0001, a **reproduced SIGSEGV** — was never surfaced. It is not a
recursion-rule gap at all. The same blind spot silently applies to `scan_refcounts`,
`scan_error_paths`, `scan_null_checks`, `scan_ft_races` and the rest over the entire
frozendict implementation.

Trigger: a `PyMethodDef` table interleaving Argument Clinic `*_METHODDEF` macros with brace
initializers (`Objects/dictobject.c:5251-5271`) — tree-sitter-c cannot parse
`DICT___CONTAINS___METHODDEF {"__getitem__", ...}` (identifier followed by an initializer,
no comma). A 12-line minimal file with only that construct reproduces `root_node.has_error == True`.

Tree-wide impact (Objects/ + Python/ + Modules/ + Parser/, 638,915 C lines):

| file | lines | fns | last fn | lost | % |
|---|---|---|---|---|---|
| `Modules/_cursesmodule.c` | 9164 | 110 | 4163 | 5001 | 55% |
| **`Objects/dictobject.c`** | 8598 | 187 | 5239 | **3359** | **39%** |
| `Modules/_winapi.c` | 3463 | 15 | 826 | 2637 | 76% |
| `Modules/_tkinter.c` | 3928 | 46 | 2070 | 1858 | 47% |
| `Modules/faulthandler.c` | 1553 | **0** | 0 | 1553 | 100% |
| `Modules/_io/winconsoleio.c` | 1266 | **0** | 0 | 1266 | 100% |
| `Modules/mmapmodule.c` | 2440 | 39 | 1254 | 1186 | 49% |
| `Objects/bytesobject.c` | 3926 | 67 | 2784 | 1142 | 29% |
| `Python/optimizer_bytecodes.c` | 2748 | 1 | 1684 | 1064 | 39% |
| … 10 more | | | | | |
| **total** | | | | **23,825 lines (3.73%)** | |

**No scanner reports this.** `files_analyzed: 113` and `functions_analyzed: 4627` in the run
JSON read as full coverage. A clean scan of `dictobject.c` currently means "clean in the first
61% of it".

**R2 — the `element_descent` gate requires a loop or `*_GET_ITEM`, so every fixed-arity descent is dropped.**

```python
# scan_recursion_guards.py:152-155
has_container = bool(_CONTAINER_ACCESS_RE.search(func["body"])) or _has_loop(func["body_node"])
element_descent = bool(element_apis) and has_container
```
Arity has nothing to do with depth: one `PyObject_Hash` on one struct field is enough, because
the field is an arbitrary Python object. Measured tree-wide: **42** functions call `PyObject_Hash`
with no guard in their body; **36 of them have no loop** and are therefore unreachable by this
rule. Four of my six new findings are in that 36 (`ga_hash`, `weakref_hash_lock_held`,
`mappingproxy_hash`, `unionbuilder_add_single_unchecked`), plus `frozendict_pair_hash`,
which R1 hides on top.

**R3 — `*_lock_held` helper delegation makes the slot body empty.**
`_slot_for()` classifies a function by slot-table entry or by name suffix
(`_hash`/`_repr`/`_str`/`_richcompare`). The registered slot `weakref_hash` (`:205`) contains
*no* descent — it is three lines wrapping `weakref_hash_lock_held` (`:190`), whose name matches
nothing, so the function is never even a candidate. CPython's free-threading refactor made
`<slot>_lock_held` a **pervasive** idiom (`odict_richcompare_lock_held`,
`bounded_lru_cache_get_lock_held`, `PyODict_SetItem_LockHeld`, …), so this is now systematic,
not a one-off.

**R4 — no model of the dispatcher guard** (see the table at the top). The scanner treats all six
`_ELEMENT_RECURSION_APIS` as equally dangerous, when in fact five of them are guarded one level
up and only `PyObject_Hash` is not. Modelling this single fact fixes the FP *and* sharpens every
true positive to `high`.

**R5 — indirect repr spellings unmodelled**: `%R` in `PyUnicode_FromFormat` and
`PyUnicodeWriter_WriteRepr` are element descents the scanner cannot see. Low priority *because
of R4* — repr is dispatcher-guarded, so modelling them would only manufacture FPs. Worth
recording so nobody "fixes" it in the wrong direction.

**R6 — tail-call elimination unmodelled.** `mappingproxy_hash` is a textbook unguarded descent
that does not crash, because its single tail call was compiled to a jump. Any tool that reports
it as FIX will be told "cannot reproduce".

### Prompt issues

1. **My agent definition contains a factual error that would suppress the crown jewel.**
   `agents/recursion-guard-auditor.md`, Phase 2:
   > *"A guard on a *caller* (the dispatch wrapper `PyObject_Repr`/`PyObject_Hash` already wrap `Py_EnterRecursiveCall`) can make a leaf slot safe."*

   `PyObject_Hash` does **not** wrap `Py_EnterRecursiveCall` (`object.c:1158`). An agent that
   trusted this line would dismiss `tuple_hash`, `frozendict_pair_hash`, `ga_hash` and
   `weakref_hash` as guarded-by-dispatcher — i.e. dismiss the entire confirmed bug class,
   including the two catalog entries. This must be corrected to name Repr/Str/RichCompare
   only, and to state explicitly that `PyObject_Hash` is the unguarded one.
   The same wording is inherited by `data/cpython_non_bugs.md` ("Guarded by the dispatcher …
   `PyObject_Repr`/`PyObject_Hash`/`PyObject_RichCompare`") and needs the identical fix.
2. The Output Format section hard-codes a worked `tuple_hash` example, which pulls the agent
   toward re-litigating a catalog entry — exactly what informed rule 1 forbids. Swap it for a
   non-catalog example.
3. Phase 3 says "reproduce on a locally-built debug CPython" but does not say **where**. The run
   had a usable ASan build at `/home/danzin/projects/cpython/python` that I found by looking;
   RUN_CONTEXT should carry the interpreter path, and the prompt should tell the agent to prime
   caches (`tuple_hash` memoises in `ob_hash`) when isolating a descent.

### Concrete tuning proposals (ranked by value)

**P1 — Emit a parse-health diagnostic; treat silent truncation as a bug (fixes R1, benefits all 12 scanners).**
In `tree_sitter_utils.extract_functions` (or `scan_common.build_report`), compute
`tree.root_node.has_error` and `nlines - max(end_line)`, and when the trailing gap exceeds a
threshold, append to the report:
```json
"parse_health": [
  {"file": "Objects/dictobject.c", "lines": 8598, "functions": 187,
   "last_function_line": 5239, "unparsed_trailing_lines": 3359,
   "coverage": 0.61, "reason": "tree-sitter ERROR node at 5268"}
]
```
and surface a non-zero `parse_health` count in every agent prompt as "your scan of this file was
partial". Cheap, and it converts an invisible 3.73%-of-the-tree blind spot into a visible one.
Follow-up (larger): pre-substitute `\w+_METHODDEF` tokens with `{0},` before parsing, or re-parse
the post-ERROR byte range as a fresh translation unit and merge the function lists — either
recovers `dictobject.c` fully.

**P2 — Make the `PyObject_Hash` descent arity-independent (fixes R2).**
```python
UNGUARDED_DISPATCH = frozenset({"PyObject_Hash"})   # the only dispatcher without a guard
...
if call_names & UNGUARDED_DISPATCH:
    element_descent = True                          # no has_container requirement
else:
    element_descent = bool(element_apis) and has_container
```
Recovers `ga_hash`, `mappingproxy_hash`, `weakref_hash_lock_held`, `unionbuilder_add_single_unchecked`
and (once P1 lands) `frozendict_pair_hash`. Measured cost tree-wide: 36 additional candidates for
Objects/+Python/+Modules/, most of them one-frame pass-throughs that P3 can down-rank — a very
acceptable recall/precision trade for a SIGSEGV class.

**P3 — Model the dispatcher guard; classify by element op, not by slot (fixes R4 and the only FP).**
Split `_ELEMENT_RECURSION_APIS` into two tables and emit a distinct finding `type`:
```python
_GUARDED_DISPATCH   = {"PyObject_Repr", "PyObject_Str", "PyObject_ASCII",
                       "PyObject_RichCompare", "PyObject_RichCompareBool"}  # object.c:780/823/1110
_UNGUARDED_DISPATCH = {"PyObject_Hash"}                                     # object.c:1158 — no guard
```
- descent via `_UNGUARDED_DISPATCH` → `type: "missing_recursion_guard"`, `confidence: high`;
- descent via `_GUARDED_DISPATCH` only → `type: "recursion_descent_guarded_by_dispatcher"`,
  `confidence: low`, plus `"guarded_by": "Objects/object.c:1110"` — kept for auditability,
  excluded from the FIX count.
Also **add a `pass_through` de-rank**: a `PyObject_Hash` whose argument is a *function parameter*
rather than a field of `self` adds one frame and is not additive per level (the
`odictobject.c:633` / `setobject.c:600` class) → `confidence: low`.

**Also worth doing, in order:**
- **P4** — in `_slot_for()`, follow one level of static helper delegation: if a slot-mapped function's
  body is a single call to a same-file `static` function (esp. `*_lock_held` / `*_LockHeld`),
  analyse the callee's body under the caller's slot. Recovers `weakref_hash` and generalises to the
  whole FT `_lock_held` refactor (R3).
- **P5** — record the confirmed copy-paste pair in a data file so propagation is tracked, e.g.
  `data/cpython_known_issues.json`: `{"shape": "unguarded-recursion-in-slot", "sites":
  ["Objects/tupleobject.c:385 tuple_hash", "Objects/dictobject.c:8427 frozendict_pair_hash"],
  "note": "tupleobject.c:367 comment (244300162d2) names the copy; neither has a guard"}`,
  and add a scanner rule that flags any function whose leading comment matches
  `[Cc]ode copied from (\w+)` when the named source has an open finding.
- **P6** — add `confidence: "compiler_dependent"` (or a `tail_call: true` flag) when the only
  descent is a bare `return PyObject_Hash(...)`, so `mappingproxy_hash`-class sites are reported
  honestly rather than as reproducible crashes (R6).
- **P7** — correct the two prompt/data errors in "Prompt issues" #1 and #2 above.
