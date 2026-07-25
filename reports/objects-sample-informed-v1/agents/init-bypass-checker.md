# init-bypass-checker — Objects/ sample (informed)

**Headline: the zero was a recall gap, not a true negative.** Hand-reading the shape the
scanner is blind to turned up a live, Python-reachable **SIGSEGV in `Objects/bytearrayobject.c`**
— a regression introduced by gh-139871 — reproduced on the debug+ASan build at **exit code 139**.
It is a left-behind sibling of exactly the gh-144330 shape Phase 1 pointed me at.

## Scanner volume

raw candidates in sample: **0**   |   confirmed: 0   |   dismissed: 0   |   precision: n/a (no output to score)
raw candidates across all 50 files of `Objects/`: **0**
bugs found by reading that the scanner missed: **1 FIX (SIGSEGV, 6 entry points)**

Recall is the whole story for this agent, so the **Toolkit assessment** section below is the main body.

---

## Findings

### FIX

#### [FIX] `bytearray` `__init__`-bypass → NULL `ob_bytes_object` → SIGSEGV (`Objects/bytearrayobject.c:280`)

**Scope note:** `bytearrayobject.c` is *not* one of the 14 sample files, but it *is* one of the
50 files of `Objects/` over which this scanner reported 0. It is therefore a direct refutation of
that zero, and it was found by following the Phase 1 gh-144330 lead tree-wide.

**What breaks.** `PyByteArray_Type` wires `bytearray___init__` as `tp_init`
(`bytearrayobject.c:2938`) and **`PyType_GenericNew` as `tp_new`** (`:2940`) — the exact
pre-gh-144330 classmethod/staticmethod shape. The "buffer is always present" invariant is
established in `tp_init`, not `tp_new`:

```c
/* Objects/bytearrayobject.c:923 — inside bytearray___init___impl */
/* First __init__; set ob_bytes_object so ob_bytes is always non-null. */
if (self->ob_bytes_object == NULL) {
    self->ob_bytes_object = Py_GetConstant(Py_CONSTANT_EMPTY_BYTES);
    bytearray_reinit_from_bytes(self, 0, 0);
    self->ob_exports = 0;
}
```

Skip `tp_init` and `ob_bytes_object` stays NULL. Any method that grows the buffer reaches
`bytearray_resize_lock_held` (`:212`) and hits `:280`:

```c
int ret = _PyBytes_Resize(&obj->ob_bytes_object, alloc);
```

`_PyBytes_Resize` (`Objects/bytesobject.c:3344`) does `v = *pv;` then `:3349`
`if (!PyBytes_Check(v) || newsize < 0)` → `Py_TYPE(NULL)` → **SIGSEGV** at
`Include/object.h:234` in `_Py_TYPE_impl`.

**How a Python-level input reaches it.** Two routes, both pure Python, neither adversarial:

```python
# Route 1 — the direct bypass
b = bytearray.__new__(bytearray)
b.append(1)                      # SIGSEGV

# Route 2 — an ordinary subclass that forgets super().__init__()
class B(bytearray):
    def __init__(self, *a, **k): pass
B().append(1)                    # SIGSEGV
```

**Observed, not predicted.** Both routes on `/home/danzin/projects/cpython/python`
(3.16.0a0, `--with-pydebug --with-address-sanitizer`):
`AddressSanitizer: SEGV on unknown address 0x000000000008`, and with `handle_segv=0`,
**exit code 139 (SIGSEGV)**.

**Entry points (measured — 6 crash, the rest survive):**

| crashes (exit 139) | survives cleanly |
|---|---|
| `append`, `extend`, `+=`, `insert`, `b[0:0] = …`, `resize` | `take_bytes`, `pop`, `remove`, `clear`, `decode`, `copy`, `+`, `*=`, `__reduce__`, `memoryview`, `bytes()`, `repr` |

The survivors are the operations that never grow the buffer. `b.__init__()` before the call
repairs the object — confirming the diagnosis.

**Differential — this is a regression, not longstanding behavior:**

| interpreter | `bytearray.__new__(bytearray).append(1)` |
|---|---|
| 3.12.13 | `bytearray(b'\x01')`, exit 0 |
| 3.14.0a / 3.14.4 | `bytearray(b'\x01')`, exit 0 |
| **main 3.16.0a0** | **SIGSEGV, exit 139** |

Introduced by **gh-139871 / GH-140128** (`732224e1139`, "Add `bytearray.take_bytes([n])`"),
which replaced the raw `char *ob_bytes` buffer with an `ob_bytes_object` `PyBytes` holder and
put the non-NULL invariant in `__init__`. The old raw-pointer code tolerated a NULL buffer
because `PyByteArray_Resize` simply `PyMem_Malloc`'d a fresh one.

**Guarded twin (two of them, at two different layers):**

1. **The construction-layer twin is gh-144330 itself.** `classmethod`/`staticmethod` had this
   exact shape and were fixed by moving initialization into a real `tp_new` (`cm_new`/`sm_new`)
   and dropping `PyType_GenericNew`. Applying the same fix to `PyByteArray_Type` closes all six
   entry points at once — strictly better than adding six NULL guards.
2. **The API-layer twin is `_PyBytes_Resize`'s own siblings.** Every other `PyObject **`-taking
   resize/concat API in the tree NULL-checks `*pv`; `_PyBytes_Resize` is the only one that does not:

   | API | guard |
   |---|---|
   | `_PyTuple_Resize` (`Objects/tupleobject.c:1044`) | `if (v == NULL \|\| !Py_IS_TYPE(v, &PyTuple_Type) …)` ✅ |
   | `PyBytes_Concat` (`Objects/bytesobject.c:3278`) | `if (*pv == NULL) return;` ✅ |
   | `PyBytes_ConcatAndDel` (`:3390`) | `if (*pv == NULL) {` ✅ |
   | **`_PyBytes_Resize` (`:3349`)** | **`if (!PyBytes_Check(v) …)` — derefs NULL first** ❌ |

**Classification: FIX.** Pure-Python-reachable segfault, reproduced, regression against two
released versions. Recommended fix: give `PyByteArray_Type` a real `tp_new` that sets
`ob_bytes_object` (the gh-144330 pattern); optionally add `v == NULL ||` to `_PyBytes_Resize`
as defense-in-depth to match `_PyTuple_Resize`.

### CONSIDER

None. Every other candidate resolved cleanly to ACCEPTABLE (below).

### POLICY / ACCEPTABLE

- `property` (`descrobject.c:2092/2094`, `property_init` + `PyType_GenericNew`) — **has the shape,
  is fully defended.** `prop_get`/`prop_set`/`prop_del` are NULL-checked at every read
  (`:1667`, `:1682`, `:1717-1727`), `property_copy` uses `?:` ternaries (`:1786-1799`), and
  `_PyObject_IsAbstract(NULL)` returns 0 by contract (`object.c:1256`). Probed: no crash.
- `OrderedDict` (`odictobject.c:1619`, `odict_init` + `tp_new = 0`) — `odict_init` calls only
  `odict_update()`; it assigns **no struct fields**, so there is nothing to leave NULL. Probed.
- `super` (`typeobject.c:13063`) — has the shape; `super_getattro`/`super_repr` guard
  `su->obj_type`. Probed: no crash.
- `list` (`listobject.c:3964`) — `list___init__` sets no pointer field that a later read derefs;
  `list.__new__(list)` is a valid empty list.
- `cell_contents` (`cellobject.c:168`) — deletable getset (`cell_set_contents` accepts NULL), but
  **every** read goes through `PyCell_GetRef()`, which returns NULL explicitly and forces the
  caller to handle it (`:150` ValueError "Cell is empty", `:106` richcompare, `:120` repr,
  `bytecodes.c:2420`). `del c.cell_contents` then repr/eq/lt/hash/closure-invoke → all clean;
  invoking the closure gives a proper `NameError`. **This is the model implementation of the class.**
- `func.__doc__` / `func.__module__` (`funcobject.c:619,621`) — deletable `_Py_T_OBJECT` members
  (invisible to the scanner, see R2 below). Safe: `_Py_T_OBJECT` reads NULL as `None`, and the
  only C read is guarded (`Python/ceval.h:271 if (f->func_module)`).
- `property.__name__`, `classmethod`/`staticmethod` `__annotations__`/`__annotate__`,
  `function.__dict__` — NULL-accepting or delete-rejecting getsets; all reads guarded. Probed.
- `weakref.__callback__`, all `interpolationobject` and `templateobject` members, all
  `structseq` members — `Py_READONLY`, so not deletable. structseq unset optional fields read
  as `None`; structseq types are not subclassable.
- `Interpolation` — `tp_new` requires `value`; `Template.__new__(Template)` yields a valid
  `Template(strings=('',), interpolations=())`. Both probed.

---

## New siblings of known shapes

**gh-144330 sibling sweep — the assigned high-value work.**

The fix itself is **complete for its own types**, verified empirically:
- `classmethod.__new__(classmethod)` → `TypeError: classmethod expected 1 argument, got 0`
  (`cm_new` requires the callable).
- `object.__new__(Sub)` for `class Sub(staticmethod)` → `TypeError: object.__new__(S) is not
  safe, use S.__new__()`, blocked by `tp_new_wrapper`'s static-base check in `typeobject.c`.

So the remaining question was the **un-swept siblings**. The shape is precisely
*`tp_init` non-zero **and** `tp_new` ∈ {`0`, `PyType_GenericNew`}* — because `tp_new_wrapper`
blocks `Base.__new__(Derived)` only when the static base's `tp_new` differs. Sweeping all of
`Objects/` for it yields exactly **5 types**:

| file:line | type | tp_init | tp_new | verdict |
|---|---|---|---|---|
| `bytearrayobject.c:2938` | `PyByteArray_Type` | `bytearray___init__` | `PyType_GenericNew` | **FIX — live SIGSEGV** |
| `descrobject.c:2092` | `PyProperty_Type` | `property_init` | `PyType_GenericNew` | ACCEPTABLE (guarded) |
| `listobject.c:3964` | `PyList_Type` | `list___init__` | `PyType_GenericNew` | ACCEPTABLE |
| `odictobject.c:1619` | `PyODict_Type` | `odict_init` | `0` (inherits `dict_new`) | ACCEPTABLE |
| `typeobject.c:13063` | `PySuper_Type` | `super_init` | `PyType_GenericNew` | ACCEPTABLE |

**Yield: 1 of 5 is a live crash.** Phase 1's prediction that "no sweep followed gh-144330" was
correct, and the sweep it asked for found the one that mattered.

**Negative result worth recording:** the sample's own highest-risk candidates (`cellobject.c`
`cell_contents`, `descrobject.c` `property`, `funcobject.c`, `weakrefobject.c`, `structseq.c`,
`capsule.c`, `odictobject.c`, `templateobject.c`, `interpolationobject.c`, `lazyimportobject.c`)
are all clean. The 14-file sample is genuinely free of this class.

---

## Classes bounded (clean negatives)

Empirical bound, not just a reading: a generated prober ran
**32 sample types × 2 constructions (`T.__new__(T)`, subclass overriding `__init__`) ×
26 operations = 1,664 subprocess runs** on the debug+ASan build
(`repro/…/probe2.py`, driver `sweep.sh`). Operations covered `getattr`-all, `callattr`-all,
`repr`, `str`, `hash`, `iter`, `call`, `len`, descriptor `__get__`/`__set__`/`__delete__`,
`==`, `<`, `[]`, `__reduce__`, `copy`, `deepcopy`, `bool`, `next`, `__sizeof__`, `keys`,
`in`, `setattr`, `index`, `format`, `dir`, and a GC pass.

**Result: zero crashes.** Types covered: `tuple`, `tuple_iterator`, `GenericAlias`, `UnionType`,
`Template`, `Interpolation`, `TemplateIter`, `property`, `mappingproxy`, `method_descriptor`,
`classmethod_descriptor`, `getset_descriptor`, `member_descriptor`, `wrapper_descriptor`,
`method-wrapper`, `OrderedDict`, `odict_iterator`/`keys`/`items`/`values`, `function`,
`classmethod`, `staticmethod`, `weakref.ref`/`ProxyType`/`CallableProxyType`, `os.stat_result`,
`sys.version_info`, `time.struct_time`, `list_iterator`, `callable_iterator`, `cell`.

Plus targeted deletable-member/getset reproducers (`del c.cell_contents`, `del f.__doc__`,
`del f.__module__`, `del pr.__name__`, `del cm.__annotations__`, `del f.__dict__`) — all clean.

**So: the 14-file sample is a true negative for this class. `Objects/` as a whole is not.**

---

## Toolkit assessment  ← REQUIRED

### Verdict: recall gap, and a structural one

The v0.7 calibration note — "0 false positives on `Objects/`, achieved by honoring the
`field &&` / `if (field)` / `field ?` truthiness idioms" — describes a property the scanner
never actually exercised. **On `Objects/`, the `new_bypass` rule cannot fire at all.** The zero
was free, not earned.

**Root cause, in two layers.**

*Layer 1 — the slot-table form.* The scanner recognizes only two `tp_init` spellings:

```python
_TP_INIT_SLOT_RE  = re.compile(r"\bPy_tp_init\s*,\s*(?:&\s*)?(\w+)")      # PyType_Spec
_TP_INIT_DESIG_RE = re.compile(r"\.tp_init\s*=\s*…(\w+)")                 # designated init
```

`Objects/` overwhelmingly uses neither — it uses the **positional static `PyTypeObject`** form
with a trailing slot comment. Measured across `Objects/*.c`:

| form | `tp_init` | `tp_new` |
|---|---|---|
| positional `X,  /* tp_init */` | **42** | **42** |
| designated `.tp_init =` | 2 | 12 |

The scanner sees **2 of 44**. `Modules/` uses the `PyType_Spec` form, which is why the
`_sqlite` calibration worked and why `Modules/` yields 102 nullable fields and 10 findings while
`Objects/` yields 24 and 0.

*Layer 2 — and this is the subtle part — `strip_comments()` destroys the marker.*
`_collect_nullable_fields` runs all its slot regexes over `clean = strip_comments(source)`.
The positional form's *only* identifying token is the `/* tp_init */` comment. Stripping comments
first is correct project policy for the pattern-matching rules (CLAUDE.md gotcha), but here it
erases the very thing the rule needs. **Adding a positional regex alone does not fix this** — I
tried it and still got 0. The regex must run against the raw `source`.

**Proof the rule is dead rather than merely quiet.** I ran the shipped scanner against the
pre-gh-144330 `Objects/funcobject.c` (`git show ffa68529b40^:Objects/funcobject.c`) — a file that
provably contained this exact shape (`cm_init` + `PyType_GenericNew` at `:1624/:1626`):

```
findings: 0   nullable_fields: 0   files_with_nullable_fields: 0
```

Zero *nullable fields*, not zero findings. It could not have flagged the bug under any
guard configuration.

### Recall gaps

**R1 — positional slot tables invisible (`new_bypass` rule dead on `Objects/`, `Python/`).**
Covered above. Highest-value gap; it is what hid the bytearray crash.

**R2 — deletable members with a macro-wrapped offset are invisible.**
`_MEMBER_ENTRY_RE` requires a literal `offsetof(`:

```python
r"offsetof\s*\(\s*\w+\s*,\s*(\w+)\s*\)\s*,\s*"
```

but `funcobject.c` uses `#define OFF(x) offsetof(PyFunctionObject, x)`:

```c
{"__doc__",       _Py_T_OBJECT,     OFF(func_doc), 0},        /* funcobject.c:619 */
{"__module__",    _Py_T_OBJECT,     OFF(func_module), 0},     /* funcobject.c:621 */
```

Missed tree-wide in `Objects/`: `func_doc`, `func_module`, `methodobject.c:m_module`. All three
happen to be safe, so this cost nothing here — but it is silent, and `OFF`-style macros are
common in older CPython files.

**R3 — no getset model at all.** The agent prompt tells me to "read the **setter**: does it
accept `value == NULL`?" — but the scanner never surfaces getsets, so there is nothing to read.
`_collect_nullable_fields` has exactly two branches, deletable `PyMemberDef` and tp_init fields.
gh-152817 (`del cursor.row_factory`) was caught only because that field *also* had a member
entry. A getset-only deletable field is invisible. In the sample this class covers
`cell_contents`, `property.__name__`, `cm/sm.__annotations__`, `cm/sm.__annotate__` — four real
NULL-accepting setters the scanner never considered (all guarded, but by luck of the draw).

**R4 — the lazy-init nullability mechanism is unmodeled.** The *actual historical crash* in this
family, **gh-125017** ("Fix crash on premature access to classmethod/staticmethod annotations",
`f203d1cb52f`), was a NULL **`cm_dict`** — a lazily-created instance dict, set neither by
`tp_init` nor by a member/getset. The fix routed reads through `PyObject_GenericGetDict()`.
TSAN-0043 (`descr_get_qualname` lazy init) is the same shape. Neither the shipped scanner nor
my prototype models "field is NULL until first use", which is a third, distinct source of
legitimate NULL.

**R5 — the sink set is narrower than the bug class.** Even with R1 fixed, the bytearray crash
still would not surface: the sink is `_PyBytes_Resize(&obj->ob_bytes_object, alloc)` — an
address-of pass to a `PyObject **` API, matching none of `_INCREF_SINKS` / `_CALL_SINKS` /
`_DEREF_SINKS`. Fixing R1 makes `ob_bytes_object` *visible as nullable* (necessary), but a sink
rule is also required (sufficient).

### Precision

Nothing to report — the scanner produced no output on this scope, so no FP classes were
exercised. Worth stating plainly: **the "0 FPs on `Objects/`" v0.7 calibration claim is not
evidence of precision.** It is evidence of silence. The FP-suppression logic in `_has_null_guard`
(the truthiness idioms) is good work, but it was never reached on this corpus.

One latent precision risk I did not get to exercise: `_has_null_guard` searches the **entire
function body** for a guard on the field. In a long function with an early guarded branch and a
later unguarded read of the same field, the finding is suppressed. That trades recall for
precision silently and is worth a note in the agent prompt.

### Prompt issues

- The agent definition says the scanner "honors `== NULL` / `!field` / `field &&` / `if (field)` /
  `field ?` truthiness guards" and frames Phase 2 as *verifying* those. On a 0-candidate scope
  that framing pulls toward "confirm the negative". The RUN_CONTEXT override ("your job shifts to
  recall") is what redirected me — that instruction should live in the **agent definition**, not
  only in the run context, as a standing rule: *if the scanner returns 0, first prove the rule can
  fire on this corpus at all.*
- "**What this cannot see (be honest about it)**" lists cross-function flow, unmodeled derefs and
  file-scoped `new_bypass`. It does **not** mention that slot-table detection is *form*-specific.
  That is the single most important limitation for anyone pointing this scanner at `Objects/`,
  `Python/`, or any pre-`PyType_Spec` C file.
- The prompt tells me to read getset setters (Phase 1) even though the scanner cannot emit a
  getset finding — a capability the prompt implies but the code lacks.

### Concrete tuning proposals

Ranked by measured value. All numbers are from prototypes I ran end-to-end; the patched scanners
are in the scratchpad (`scan_v2.py` = P1+P2, `scan_v4.py` = P1+P2+P3).

---

**P1 — Parse positional slot tables, on the RAW source. (P0, unblocks the whole rule.)**

In `scan_init_bypass.py`, add alongside the existing slot regexes:

```python
_TP_INIT_POS_RE = re.compile(r"^[ \t]*(\w+)\s*,\s*/\*\s*tp_init\s*\*/", re.M)
_TP_NEW_POS_RE  = re.compile(r"^[ \t]*(\w+)\s*,\s*/\*\s*tp_new\s*\*/",  re.M)
_INHERITED_NEW  = {"PyType_GenericNew"}
```

In `_collect_nullable_fields`, pair each positional `tp_init` with the **next** positional
`tp_new` (same `PyTypeObject` block) and keep it only when that `tp_new` is `0` or
`PyType_GenericNew`. **Run these two regexes over `source`, not `clean`** — `strip_comments()`
removes the slot comments they key on. This per-block pairing also replaces the current
whole-file `_TP_NEW_TOKENS_RE` kill-switch, which the agent doc already flags as over-broad
(it is why `property` stays hidden even in a naive positional patch: `descrobject.c` also
contains `mappingproxy_new`).

Measured:

| scope | shipped v0.7 | with P1+P2 |
|---|---|---|
| `Objects/` nullable fields | 24 (3 files) | **34 (6 files)** |
| `Objects/` findings | 0 | **0** |
| `Modules/_sqlite` findings | 3 | **3** (no regression) |
| `Modules/` findings | 10 | **10** (no regression) |
| pre-gh-144330 `funcobject.c` nullable fields | 0 | **2** (`cm_callable`, `sm_callable`) |

Newly visible on `Objects/`: `property`'s `prop_get`/`prop_set`/`prop_del`/`prop_doc`/`prop_name`/
`getter_doc`, and `bytearray`'s `ob_bytes_object`/`ob_exports`.

**Does this reintroduce the FPs v0.7 eliminated? No — measured 0 new findings on `Objects/` and
byte-identical output on `Modules/`.** The v0.7 truthiness-guard work is what keeps it at zero:
`property`'s six fields all become candidates and all six are correctly suppressed by real
`== NULL` guards. After P1 the zero on `property` is *earned* rather than vacuous.

---

**P2 — Accept macro-wrapped offsets in `_MEMBER_ENTRY_RE`.** (Fixes R2.)

```python
r"(?:offsetof\s*\(\s*\w+\s*,\s*(\w+)\s*\)|[A-Z_]\w*\s*\(\s*(\w+)\s*\))\s*,\s*"
```

(then take `m.group(2) or m.group(3)` as the field). Restricting the macro alternative to an
UPPERCASE name keeps it from swallowing arbitrary expressions. Measured: +3 deletable members on
`Objects/` (`func_doc`, `func_module`, `m_module`), 0 new findings, no change on `Modules/`.

---

**P3 — New sink category `addr_deref` for `PyObject **` APIs that deref `*pv` unguarded.**
(Fixes R5; this is the rule that actually catches the bytearray bug.)

```python
# PyObject**-taking APIs that dereference *pv with NO NULL check. Their siblings
# _PyTuple_Resize ("v == NULL ||") and PyBytes_Concat ("if (*pv == NULL) return")
# DO check, so only the unguarded ones belong here.
_ADDR_DEREF_SINKS = frozenset({"_PyBytes_Resize"})
```

Return `"addr_deref"` from `_sink_kind()`, and in `_check_function` strip a leading `&` from the
first argument before resolving the field. The allowlist is deliberately tiny and evidence-based —
I checked the whole family and `_PyBytes_Resize` is the only unguarded member.

Measured with P1+P2+P3:

| scope | findings | precision |
|---|---|---|
| `Objects/` | **1** — `bytearrayobject.c:280 bytearray_resize_lock_held / ob_bytes_object / _PyBytes_Resize / new_bypass` | **1/1 = 100%** (confirmed SIGSEGV) |
| `Modules/` | 10 (unchanged) | no regression |
| `Python/` | 0 | — |

---

**P4 — Model getset setters as a nullability source.** (Fixes R3.) For each `PyGetSetDef` entry
with a non-NULL setter, resolve the setter body; if it does **not** reject deletion (no
`value == NULL` early-return, no `"cannot delete"` / `"may not be deleted"` message) and it
assigns the field via `Py_XSETREF`/`Py_SETREF`/direct store, record the field with reason
`deletable_getset` and `confidence: "high"`. A first-cut classifier over the 14 sample files
found 7 NULL-accepting setters and correctly separated them from the rejecting ones. This is the
gh-152817 mechanism generalized beyond fields that happen to also carry a `PyMemberDef`.

---

**P5 — Documentation / prompt changes (cheap, high leverage).**

- Add to the agent definition's "What this cannot see" block: *"Slot detection is **form**-specific.
  `PyType_Spec` (`{Py_tp_init, …}`) and designated (`.tp_init = …`) tables are parsed; the
  positional static `PyTypeObject` form (`X, /* tp_init */`) used throughout `Objects/` and
  `Python/` requires P1. If your scope is `Objects/`, a 0 may mean the rule never fired."*
- Add a standing rule: *"If the scanner returns 0 on a scope, first verify the rule **can** fire
  there (check `total_nullable_fields`, not just `total_findings`) before reporting a clean
  negative."* `total_nullable_fields` is already in the envelope and is the perfect canary — it
  read 24 on `Objects/` vs 102 on `Modules/`, and 0 on a file that provably had the bug.
- Add to `data/cpython_non_bugs.md`, **Init bypass** section: *"A field whose every read goes
  through an accessor that returns NULL explicitly (`PyCell_GetRef`, `PyObject_GenericGetDict`)
  is ACCEPTABLE — `cellobject.c` is the model implementation."*
- Add to the bug-shape catalog a new shape **`init-bypass-null-deref`** with guarded twin
  = gh-144330 (`cm_new`/`sm_new` initialize in `tp_new`), hunt = *"every type with `tp_init`
  non-zero and `tp_new` ∈ {0, `PyType_GenericNew`}"*, differential = *"`T.__new__(T).method()`
  and a subclass overriding `__init__`"*, confirmed examples = gh-152954, gh-152817, gh-144330,
  gh-125017, **this bytearray finding**.

---

## Reproducers

Saved to `<run>/repro/init_bypass_bytearray.py` (6 cases; `python init_bypass_bytearray.py new`).
Broad prober and sweep driver used for the clean-negative bound are in the session scratchpad
(`probe2.py`, `sweep.sh`, `ba_sib.py`).
