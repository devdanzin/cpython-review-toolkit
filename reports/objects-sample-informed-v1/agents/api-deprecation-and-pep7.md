# api-deprecation-tracker + pep7-style-checker — Objects/ sample (informed)

Two low-yield-but-unassessed scanners covered in one pass. Target: CPython main
`4f3be1b5777` (3.16.0a0), 14-file `Objects/` sample. Both aspects produce **clean
negatives**; the value of this report is the precision measurement and the tuning
proposals, not the findings.

## Scanner volume

| aspect | raw candidates (sample) | confirmed | dismissed | precision |
|---|---|---|---|---|
| **api-deprecation** (agent's 12-pattern list) | **0** | 0 | 0 | n/a — no candidates |
| api-deprecation, widened to all of `Objects/` | 13 | **0** | 13 | **0%** |
| api-deprecation, my extended 3.12–3.14 list (sample) | 6 | **0** | 6 | **0%** |
| **pep7-style** (sample) | **280** | **0 actionable** | 280 | **0%** |
| pep7-style, all of `Objects/` | 5,736 | 0 actionable | 5,736 | **0%** |
| `_PyObject_GC_UNTRACK` macro-vs-function sweep (sample) | 18 macro sites | **1** (already recorded) | 17 | 5.6% |

---

## Findings

### FIX

**None net-new.** The one genuine macro-vs-function safety defect in the sample —
`Objects/odictobject.c:1718` `odictiter_dealloc` — was already found, reproduced under
OOM injection, and fully written up by the **uninitialized-dealloc-auditor**
(`agents/uninitialized-dealloc-auditor.md`, finding #1). Per informed-mode rule 1 I
confirm it in one line and do not re-litigate:

> `odictiter_dealloc` opens with `_PyObject_GC_UNTRACK(di)` (the *requires-tracked*
> internal macro) on an object `odictiter_new` can `Py_DECREF` before ever calling
> `_PyObject_GC_TRACK` — confirmed reproducing as
> `Assertion "_PyObject_GC_IS_TRACKED(...)" failed` at `set_nomemory(K=1)`.

Its **guarded twin for the macro-vs-function axis specifically** is
`Objects/odictobject.c:1424` `odict_dealloc` — the *same file*, same author, using the
tolerant function `PyObject_GC_UnTrack`. That in-file asymmetry is the cleanest
statement of the modernization gap.

### CONSIDER

**`Objects/odictobject.c` — arbitrary `_PyObject_GC_UNTRACK` / `PyObject_GC_UnTrack`
split across the whole sample (18 vs 11).** Not a bug today at 17 of the 18 sites, but
a latent trap: the unchecked macro silently imposes an invariant on *every constructor
of that type* ("no free path before `_PyObject_GC_TRACK`"), enforced only by a
debug-build assertion. The full split, with the four files where the choice flips
within a single file:

| file | macro `_PyObject_GC_UNTRACK` | function `PyObject_GC_UnTrack` |
|---|---|---|
| `tupleobject.c` | `tupleiter_dealloc`:1107 | `tuple_dealloc`:255 |
| `genericaliasobject.c` | `ga_dealloc`:32 | `ga_iter_dealloc`:957 |
| `odictobject.c` | `odictiter_dealloc`:1715 **(the bug)** | `odict_dealloc`:1424 |
| `descrobject.c` | `descr_dealloc`:23, `mappingproxy_dealloc`:1188, `property_dealloc`:1647 | `wrapper_dealloc`:1324 |
| `funcobject.c` | `func_dealloc`:1128, `cm_dealloc`:1415, `sm_dealloc`:1680 | — |
| `iterobject.c` | `iter_dealloc`:36, `calliter_dealloc`:205, `anextawaitable_dealloc`:321 | — |
| `unionobject.c` | `unionobject_dealloc`:20 | — |
| `cellobject.c` | `cell_dealloc`:77 | — |
| `lazyimportobject.c` | `lazy_import_dealloc`:77 | — |
| `weakrefobject.c` | — | `weakref_dealloc`:141, `proxy_dealloc`:703 |
| `templateobject.c` | — | `templateiter_dealloc`:42, `template_dealloc`:186 |
| `structseq.c` / `capsule.c` / `interpolationobject.c` | — | 1 each |

I verified the constructor of **all 14 other macro-using types** in the sample and every
one is safe: allocation is followed only by infallible field initialisation
(`Py_NewRef` / `Py_XNewRef` / scalar stores) before `_PyObject_GC_TRACK`, or the type
uses the zeroing+tracking `PyType_GenericAlloc` (`descr`, `property`, `classmethod`,
`staticmethod`, `ga`). Checked windows:
`iterobject.c:26→31`, `:196→201`, `:533→540`; `cellobject.c:15→20`;
`lazyimportobject.c:29→50`; `unionobject.c:582→593`; `tupleobject.c:1262→1268`;
`descrobject.c:1285→1288`; `funcobject.c:123→151`, `:202→236`.

The single site in the sample that **documents** the invariant is
`Objects/funcobject.c:206`:

```c
    /* Note: No failures from this point on, since func_dealloc() does not
       expect a partially-created object. */
```

That comment is the guarded twin at the *documentation* level, and it exists exactly
once. Nowhere else does the constructor tell the next maintainer that reordering a
fallible call above `_PyObject_GC_TRACK` turns a clean `MemoryError` into a fatal abort.

**Classification: CONSIDER, not FIX** — no reachable defect at the other 17 sites.
Semantics note (correcting the framing this agent was dispatched with):
`PyObject_GC_UnTrack` is **untracked-tolerant, not NULL-safe**. `Python/gc.c:1948`
guards with `if (_PyObject_GC_IS_TRACKED(op))`, and `_PyObject_GC_IS_TRACKED`
(`Include/internal/pycore_gc.h:73`) dereferences `op` unconditionally — a NULL argument
still segfaults in both variants. The distinction that saves `odictiter_dealloc` is
tracked-vs-untracked, which is precisely the case that arises here because
`gc_alloc()` zeroes the 2-word GC pre-header.

### POLICY / ACCEPTABLE

- **ACCEPTABLE** — All 13 `Objects/`-wide hits for the agent's 12 deprecated-API
  patterns are the *definitions* of the deprecated APIs themselves (which must remain
  for ABI compatibility) or comment prose. Detail in "Classes bounded" below.
- **ACCEPTABLE** — `Objects/stringlib/*.h` (11 files) have no header guard *by design*:
  the stringlib template pattern relies on multiple inclusion with different
  `STRINGLIB_*` macros set (`ucs1lib.h`/`ucs2lib.h`/`ucs4lib.h`). Adding the guard the
  scanner recommends would break the build.
- **ACCEPTABLE** — `Objects/clinic/*.c.h` (34 files) are Argument Clinic output,
  `#include`d exactly once; `Objects/unicodetype_db.h` is a generated data table.
- **POLICY** — Whether `line-too-long` should be reported at all on unmodified tree
  code. CPython's own machine-checkable contract (`.editorconfig`) specifies
  `trim_trailing_whitespace`, `insert_final_newline`, `indent_style = space`,
  `indent_size = 4` — and **deliberately sets no `max_line_length`**. See below.

---

## New siblings of known shapes

**None.** The macro-vs-function sweep is the natural sibling hunt for this run's
confirmed `dealloc-of-uninitialized-object` finding, and I ran it to completion over
all 18 macro sites in the sample. It yielded **zero** new siblings — the odict case is
the only one. This is a genuinely bounded negative, not an unfinished search.

Two scope escapes worth recording for a future run: `Objects/` as a whole has 69
`_PyObject_GC_UNTRACK` and 27 `PyObject_GC_UnTrack` occurrences, so ~49 macro sites
outside the sample are unswept by this pass. The memory-pattern-analyzer already
flagged `dictobject.c:5658 dictiter_dealloc` (macro) as the structural twin of the
odict bug.

---

## Classes bounded (clean negatives)

### 1. Deprecated C API usage in the sample: zero, with evidence

All 12 patterns from the agent definition, run over the 14 sample files: **0 hits**.
Run over all 123 files of `Objects/`: 13 raw hits, **all dismissed**:

| pattern | `Objects/` hits | what they actually are |
|---|---|---|
| `PyModule_AddObject` | 0 | — |
| `PyUnicode_READY` | 0 | — |
| `PyEval_InitThreads` | 0 | — |
| `Py_TRASHCAN_SAFE_BEGIN/END` | 0 | — |
| `PyUnicode_GET_SIZE` | 0 | — |
| `PyOS_AfterFork` | 0 | — |
| `Py_UNICODE` | 2 | comment prose, `unicodeobject.c:4151,4154` |
| `PyUnicode_AsUnicode` | 1 | **regex FP** — substring of `PyUnicode_AsUnicodeEscapeString` (`:6764`), a live non-deprecated API |
| `PyCFunction_Call` | 1 | its own definition, `call.c:379` |
| `PyObject_As{Char,Read,Write}Buffer` | 4 | their own definitions + 1 comment, `abstract.c:382,396,410` |
| `PyUnicode_GetSize` | 3 | the tombstone definition at `:4158` that raises `"PyUnicode_GetSize has been removed."` |
| `PyEval_Call{Object,Function,Method}` | 6 | their own definitions + comments, `call.c:426,584,656` |

**Verdict: `Objects/` is fully migrated off every API on the agent's list.** That is
the correct and expected answer — these are all pre-3.10 deprecations and CPython
completed those sweeps years ago.

### 2. Modern (3.12–3.14 era) deprecations in the sample: also zero

Because the agent's list scored zero I extended it with the deprecations that are
*actually live* today, and re-swept. Sample results, all dismissed:

- `PyDict_GetItem` ×1, `PyDict_GetItemString` ×1, `PyMapping_HasKey` ×1,
  `PyMapping_HasKeyString` ×1 — **all four are rows in the API-comparison
  documentation comment table at `Objects/odictobject.c:215–245`**, not call sites.
- `PyWeakref_GetObject` ×1 — `Objects/weakrefobject.c:977`, its own definition
  (deprecated 3.13, retained for ABI).
- `PyObject_GetAttrString` ×3 (`templateobject.c:356`, `unionobject.c:452`,
  `capsule.c:257`) — none followed by `PyErr_Clear()`, so none is a
  `PyObject_GetOptionalAttrString` migration candidate. Not deprecated; correct as written.

Zero occurrences anywhere in `Objects/` for `PyErr_Fetch`, `PyErr_GetExcInfo`,
`PyImport_ImportModuleNoBlock`, `PyWeakref_GET_OBJECT`.

### 3. PEP 7 rules that correctly find nothing

`trailing-whitespace` and `tab-indent` — the two rules the agent prompt classifies as
**FIX** severity — fire **0 times across all 123 files of `Objects/`**. I verified this
is true and not a scanner failure, by independent grep:

```
grep -rlP '^ *\t'      --include=*.c --include=*.h Objects/ | grep -v mimalloc   # empty
grep -rlP '[ \t]+$'    --include=*.c --include=*.h Objects/ | grep -v mimalloc   # empty
```

This is exactly what `.editorconfig` enforces, so it is a real and expected clean
negative rather than a dead rule.

---

## Toolkit assessment

### Line-number accuracy — **verdict: check_pep7.py is exact; it does not share the +2 drift**

I re-derived every one of the 280 sample violations from the real file at the reported
line and re-tested the rule predicate:

| rule | line numbers self-consistent | mismatched |
|---|---|---|
| `line-too-long` | 142 | **0** |
| `missing-braces` | 131 | **0** |
| `func-call-space` | 7 | **0** |

**280/280 exact. Zero drift.** The reason is structural, and worth recording so nobody
"fixes" it later:

- The `+2` drift in the other scanners comes from
  `strip_comments_and_strings()` — duplicated verbatim in `scan_null_checks.py:66`,
  `scan_refcounts.py:68`, `scan_error_paths.py:66`, `scan_gil_usage.py:66`, and
  `measure_c_complexity.py:68` — whose first line is
  `re.sub(r'/\*.*?\*/', ' ', source, flags=re.DOTALL)`. Replacing a multi-line block
  comment with a **single space** destroys its newlines, shifting every later line up by
  (comment_lines − 1). A 3-line CPython block comment eats exactly 2 newlines → the
  constant `+2` the other two agents observed.
- `check_pep7.py` **has no such function**. Its `_build_line_mask()` (`:73`) builds a
  per-line boolean over `source.split('\n')`, preserving 1:1 line indexing, and
  `_strip_line_strings_and_comments()` (`:100`) is applied to one line at a time. Line
  identity is never lost.

**Chassis defect (tree-sitter `extract_functions` dropping brace-unbalanced files):
also not applicable.** `check_pep7.py`'s entire import list is
`json, re, sys, pathlib.Path, typing.Generator` — it never imports `tree_sitter_utils`
or even `scan_common`, and does not parse functions at all. It processes every line of
every file. My `_PyObject_GC_UNTRACK` sweep was likewise written as an independent
regex pass for the same reason, so neither half of this report is exposed to the ~39%
line loss in `dictobject.c`.

*(Side note: `check_pep7.py` carries its own copies of `find_cpython_root` and
`discover_c_files` rather than importing `scan_common`'s. Harmless today — they are
behaviourally identical — but it means a future fix to `scan_common.discover_c_files`
will silently not reach this scanner.)*

### Precision — which rules produced FPs, and the FP class for each

**Headline: the PEP 7 checker's precision on `Objects/` is 0%, and this is a broken-rule
problem, not a broken-code problem — exactly as the run brief predicted.**

#### `missing-braces` — 131 in sample, 2,199 in `Objects/`. Actionable: **0**.

Detection is *accurate*: 130 of 131 are genuinely brace-less single-statement blocks
(`if (index < 0)\n    return NULL;`). The rule is nonetheless wrong to report them,
because **PEP 7 explicitly exempts them.** Verbatim from PEP 7 §Code lay-out:

> "braces are required everywhere, even where C permits them to be omitted, **but do
> not add them to code you are not otherwise modifying.** All new C code requires
> braces."

The scanner implements the first clause and ignores the second. On an existing,
unmodified tree the second clause governs every single hit. The rule is only meaningful
against **added lines in a diff**.

One additional true detection FP: `Objects/odictobject.c:532` is the continuation line
of the `#define _odict_FOREACH(od, node)` macro (`:531` ends in `\`), not a control
statement in a function body. The rule does not skip preprocessor continuations.

#### `func-call-space` — 7 in sample, 174 in `Objects/` (excl. mimalloc). Actionable: **0**.

This rule has **no basis in PEP 7 at all.** PEP 7 states a *positive* rule — "one space
between keywords like `if`, `for` and the following left paren; no spaces inside the
paren" — and states no prohibition on a space before a function-call paren. The rule was
inferred from general C convention. Root-cause breakdown of all 174 `Objects/` hits:

| FP class | count | why the "fix" is wrong |
|---|---|---|
| `#define NAME (expr)` object-like macro | **112 (64%)** | The space is **mandatory C**. `#define MAX_INTMAX_CHARS(...)` defines a *function-like* macro — removing the space changes the program. |
| `assert (...)` | 34 (20%) | `assert` is keyword-like; under PEP 7's actual rule the space is arguably *required*, not forbidden. |
| inside a backslash-continued multi-line string literal | 11 (6%) | Scanner bug — see below. |
| `typedef int (*fn)(...)` function-pointer declarator | 5 (3%) | Mandatory syntax. |
| other (`#elif (X == 4)`, more string leakage) | 12 (7%) | — |

The 6% string-literal class is a **genuine masking bug**:
`_strip_line_strings_and_comments()` uses `r'"(?:[^"\\]|\\.)*"'`, which requires the
closing quote *on the same line*. CPython docstrings are written as backslash-continued
multi-line literals (`Objects/rangeobject.c:178-179`,
`Objects/iterobject.c:475`, `Objects/funcobject.c:1585,1852`), so their prose leaks into
the code-pattern matchers. `_build_line_mask` tracks `/* */` state across lines but has
no equivalent state machine for `\`-continued strings.

#### `header-guard` — 46 in `Objects/`. Actionable: **0**.

`discover_c_files()` in `check_pep7.py` has no generated-code or vendored-code
exclusions, so this rule fires on exactly the three categories the agent prompt says to
skip: 34 × `Objects/clinic/*.c.h` (Argument Clinic output, included once), 11 ×
`Objects/stringlib/*.h` (**a guard would break the build** — the template pattern
depends on repeated inclusion), 1 × `Objects/unicodetype_db.h` (generated table). The
prompt says "Generated code gets a pass"; the script has no mechanism to give one.

#### `line-too-long` — 142 in sample, 3,192 in `Objects/`. Rule-correct: 100%. Actionable: **~0**.

Every hit is literally >79 characters, so unlike the rules above the detection is sound
and PEP 7 does state the 79-char limit. But it has no review value on unmodified tree
code, and 27% of the sample hits fall in categories the agent prompt itself calls
ACCEPTABLE:

| category | count | share |
|---|---|---|
| code: declaration / call | 66 | 46% |
| code: other | 36 | 25% |
| `PyMethodDef` / `PyGetSetDef` table row | 19 | 13% |
| docstring / string literal | 14 | 10% |
| comment prose | 5 | 4% |
| slot-table alignment comment | 1 | 1% |
| preprocessor `#include` line | 1 | 1% |

Length histogram: 94 of 142 are 80–89 chars; only 4 exceed 109. The decisive datapoint:
**CPython's `.editorconfig` sets `trim_trailing_whitespace`, `insert_final_newline`,
`indent_style = space`, `indent_size = 4` and pointedly no `max_line_length`.** The
tree's own enforced contract covers precisely the two rules that score zero and
deliberately omits this one.

#### `mimalloc` contamination

`Objects/mimalloc/**` (vendored third-party) accounts for ~900 of the 5,736 `Objects/`
violations and 5 of the top-10 most-violating files. The agent prompt says "Third-party
code gets a pass"; the script has no vendored-path exclusion.

### Recall gaps — what I found by reading that the scanners did not surface

**1. The deprecation agent's pattern list is a 2021-era snapshot.** All 12 entries are
pre-3.10 deprecations, which is precisely why it scores 0 on a 3.16 tree — CPython
finished those sweeps years ago. Everything deprecated in the 3.12–3.14 window is
absent: `PyDict_GetItem`/`GetItemString` → `PyDict_GetItemRef`/`GetItemStringRef`
(3.13), `PyObject_HasAttr`/`PyMapping_HasKey` → the `WithError` variants (3.13),
`PyWeakref_GetObject` → `PyWeakref_GetRef` (3.13), `PyErr_Fetch`/`PyErr_Restore` →
`PyErr_GetRaisedException`/`SetRaisedException` (soft-deprecated 3.12),
`PyObject_GetAttrString` + `PyErr_Clear` → `PyObject_GetOptionalAttrString` (3.13). I
swept all of these by hand; `Objects/` is clean on them too, but the *scanner* would not
have known.

**2. The macro-vs-function distinction is entirely absent from the agent definition.**
`_PyObject_GC_UNTRACK` vs `PyObject_GC_UnTrack` is a modernization choice with a real
safety consequence — it converted an OOM `MemoryError` into a fatal abort at
`odictobject.c:1718` — and it appears nowhere in the 12-pattern list. This class was
found by two *other* agents this run (uninit-dealloc, memory-pattern), never by the
deprecation tracker, even though it is squarely a "deprecated-form-vs-preferred-form"
finding. The same shape exists for `_PyObject_GC_TRACK` vs `PyObject_GC_Track`
(`genericaliasobject.c:1024` uses the function form; 99 vs 7 across `Objects/`).

**3. Substring matching produces false deprecation hits.** `PyUnicode_AsUnicode`
matched `PyUnicode_AsUnicodeEscapeString`, a live API. A bare-Grep agent with no
word-boundary discipline will mis-report this on a codebase where it *does* appear.

**4. Definition-vs-call-site is not distinguished.** 13 of 13 `Objects/`-wide hits were
the deprecated function's own definition. Any agent following this prompt literally
reports "PyCFunction_Call is still used in Objects/call.c" — which is true and
meaningless. The deprecated API's implementation must exist.

### Prompt issues

- **`api-deprecation-tracker` says "No Script — Qualitative Analysis".** This is the
  only agent in the toolkit with no script, and consequently the only one with no
  regression test, no data file, and no way to be updated except by editing prose in the
  agent definition. Its pattern list has silently rotted for ~4 CPython releases.
- **`pep7-style-checker`'s classification guide contradicts its own script.** It lists
  ACCEPTABLE for "generated code" and third-party code and says "Generated code gets a
  pass" — but `check_pep7.py` has no exclusion mechanism, so the agent is told to filter
  by hand what the script should never have emitted. On `Objects/` that is ~950
  violations the agent must discard manually.
- **Neither prompt asks for a precision measurement**, which is the only genuinely
  useful output either scanner produced on this scope. The RUN_CONTEXT template supplied
  that requirement; the agent definitions do not.
- **The dispatch framing described `PyObject_GC_UnTrack` as "NULL-safe".** It is
  untracked-tolerant; a NULL argument still faults. Worth correcting wherever this gets
  codified so the doc line is accurate.

### Concrete tuning proposals (ranked by value)

**1. Convert `api-deprecation-tracker` to a data-driven scanner.**
Add `data/deprecated_c_apis.json` (the cext toolkit already ships a `deprecated_apis.json`
— vendor and extend it) with per-entry `{name, deprecated_in, removed_in, replacement,
severity, note}`, and a `scan_deprecated_apis.py` that (a) matches on `\bNAME\s*\(`
word boundaries, (b) **skips the definition site** — a hit whose line matches
`^NAME\(` at column 0, or that sits inside the file that owns the symbol — and
(c) skips comment/string regions. Seed with the current 12 plus the 3.12–3.14 set listed
under Recall gap 1. This converts a rotting prose list into a refreshable data file on
the existing annual CPython-release sync cadence, and gives the check a regression test.

**2. Add a `gc-untrack-macro-form` finding type.**
New rule: a `tp_dealloc` whose body contains `_PyObject_GC_UNTRACK` where the
corresponding type's constructor has any `Py_DECREF`/`Py_XDECREF`/`goto error` between
its non-zeroing allocation and `_PyObject_GC_TRACK` → **FIX**. Same shape but a clean
constructor → **CONSIDER** (latent invariant). This is the rule that would have caught
`odictobject.c:1718` from the deprecation side, and it composes with the
memory-pattern-analyzer's proposed per-function (not per-file) macro gate. Data-file
entry:

```json
{"name": "_PyObject_GC_UNTRACK", "kind": "macro-form",
 "preferred": "PyObject_GC_UnTrack",
 "note": "Requires the object to be GC-tracked; asserts in debug, corrupts the GC list in release. The function form tolerates an untracked object. NEITHER tolerates NULL.",
 "severity": "conditional-fix"}
```

**3. Gate `missing-braces` and `line-too-long` behind a `--diff-only` mode; delete `func-call-space`.**
- `missing-braces`: emit only for lines present in a supplied diff, per PEP 7's "do not
  add them to code you are not otherwise modifying". Removes 2,199 `Objects/` findings.
- `line-too-long`: same gate, and de-severity to POLICY — CPython's `.editorconfig`
  declines to enforce it. Removes 3,192.
- `func-call-space`: **remove the rule.** It has no PEP 7 basis, and 64% of its hits are
  `#define NAME (expr)` where the space is mandatory — the suggested fix would silently
  change an object-like macro into a function-like one. If a keyword-spacing check is
  wanted, keep only `keyword-space` (rule 4), which does implement an actual PEP 7 clause.

**4. Add exclusions to `check_pep7.py:discover_c_files`.**
Skip `*/clinic/*`, `*/mimalloc/*`, `*/stringlib/*`, and `*_db.h` /
`*opcode_targets.h`-style generated files; or add a `# generated by` / `/*[clinic input]`
content sniff. Eliminates all 46 `header-guard` FPs and ~900 mimalloc violations in one
change. Additionally, exempt `.h` files that are `#include`d more than once in-tree from
`header-guard` entirely.

**5. Fix `_strip_line_strings_and_comments` to track backslash-continued string state.**
Extend `_build_line_mask` to return a second mask for "inside a `\`-continued string
literal" (a line whose stripped content ends in `\` while an odd number of unescaped `"`
precede it). Removes the 6% string-leakage FP class and hardens the checker against
CPython's docstring style generally.

**6. Doc line for both agent prompts.**
For `api-deprecation-tracker`: *"A hit on the deprecated symbol's own definition is not a
finding — CPython must keep the implementation for ABI compatibility. Report call sites
only, and match on `\bNAME\s*\(` to avoid substring hits like
`PyUnicode_AsUnicode` inside `PyUnicode_AsUnicodeEscapeString`."*
For `pep7-style-checker`: *"CPython's own tree is the reference implementation of PEP 7.
A rule firing at high volume on `Objects/` is a broken rule, not broken code — diagnose
the rule before reporting. Check the finding against `.editorconfig`, which is the
tree's machine-enforced style contract."*
