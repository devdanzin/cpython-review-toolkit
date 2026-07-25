# api-deprecation-tracker — Modules/ sample (informed)

Toolkit v0.8.0 · target `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0), full clone.
All four files cited below (`Modules/_json.c`, `Modules/_pickle.c`, `Modules/_elementtree.c`,
`Objects/unicode_writer.c`) are **byte-identical** between the build-matrix commit `a1d580430c8`
and HEAD `4f3be1b5777` (`git diff --quiet` clean for each), so the matrix binaries match the source
I read.

## Scanner volume

```
raw: 12 | confirmed: 12 | dismissed: 0 | precision: 100%
```

Rule breakdown:

| rule | raw | confirmed | precision |
|---|---|---|---|
| `deprecated-api` | 12 | 12 | **100%** |
| `gc-untrack-macro-form` | 0 | — | earned zero (denominator 0 — see below) |

Line accuracy: **12/12 exact.** Every reported `line` was checked with `sed -n "${l}p"` against the
real file and the retrieved text matched the finding's `code` field character-for-character. No
off-by-one, no drift.

Every one of the 12 is a genuine **consumer call site**. Zero definition sites, zero comment prose,
zero substring artifacts — i.e. zero instances of the three failure modes that scored the 2021
scanner 0/13 on `Objects/`.

### Vocabulary correctness (the other half of precision)

The 12 findings span four distinct APIs. Each vocabulary entry was re-verified independently against
the tree rather than trusted:

| api | claim | verified at |
|---|---|---|
| `_PyUnicodeWriter_WriteStr` | hard-internal, dep 3.14, removal 3.18 | `Include/cpython/unicodeobject.h:600` (`_Py_DEPRECATED_EXTERNALLY(3.14)`); `Doc/deprecations/c-api-pending-removal-in-3.18.rst` names it with its exact replacement. The no-warning claim is confirmed at `Include/pyport.h:271-273`: `#ifdef Py_BUILD_CORE / #define _Py_DEPRECATED_EXTERNALLY(VERSION_UNUSED)` — empty body. |
| `_PyBytes_Resize` | soft, dep 3.15, no removal | `Doc/whatsnew/3.15.rst:2623` — "`PyBytes_FromStringAndSize(NULL, len)` and `_PyBytes_Resize` are *soft deprecated*, use the `PyBytesWriter` API instead" (gh-129813). `Include/cpython/bytesobject.h:17` is a plain `PyAPI_FUNC`, consistent with the `soft` tier. |
| `PyMem_NEW` | soft, dep 2.0, no removal | `Doc/c-api/memory.rst`, "Deprecated aliases" table — "These are *soft deprecated* aliases…", `PyMem_NEW(type, size)` → `PyMem_New`. |
| `PyMem_RESIZE` | soft, dep 2.0, no removal | same table, → `PyMem_Resize`. |

### Two in-sample negative controls the old scanner would have failed

Worth recording because they are *measured*, not asserted:

- **Substring suppression works.** `_PyErr_ChainExceptions` **is** in the vocabulary (soft tier).
  `Modules/_pickle.c` contains 5 occurrences and `Modules/_zoneinfo.c` 2 occurrences of the string
  `_PyErr_ChainExceptions` — every one of them the *live* successor `_PyErr_ChainExceptions1`
  (`_pickle.c:2143, 2157, 4034, 4056, 7409`; `_zoneinfo.c:8, 281`). A naive substring match yields
  **7 false positives**; the scanner reported **0**. `grep -w` for the exact name returns nothing in
  the sample. This is precisely the `PyUnicode_AsUnicode` ⊂ `PyUnicode_AsUnicodeEscapeString` failure
  that sank the old list, reproduced on a different pair, and now handled.
- **No comment double-count.** `Modules/_json.c:263` and `:407` both carry a comment naming a writer
  API two lines above a real hit; neither produced a finding, and the real call at `:409` produced
  exactly one.

---

## Findings

### FIX

#### `Modules/_json.c:409` · `write_escaped_unicode` · `_PyUnicodeWriter_WriteStr`

```c
// gh-148241: Avoid PyUnicodeWriter_WriteStr() which calls str(obj)
// on str subclasses
if (_PyUnicodeWriter_WriteStr((_PyUnicodeWriter*)writer, pystr) < 0) {
```

**What breaks.** Removal is scheduled for **3.18**. `_json.c` is otherwise fully migrated to the
public opaque `PyUnicodeWriter` (`PyUnicodeWriter_Create` at `:534`/`:1459`, `_WriteChar`,
`_WriteASCII`, `_WriteSubstring`, `_Finish`, `_Discard` throughout). This single site casts the
opaque public handle **back down** to the private `_PyUnicodeWriter*` to reach the deprecated
private function. When the private function goes, this file stops compiling.

**Why the compiler will not tell anyone.** The marker is `_Py_DEPRECATED_EXTERNALLY(3.14)`
(`Include/cpython/unicodeobject.h:600`), which `Include/pyport.h:271-273` expands to **nothing**
under `Py_BUILD_CORE`. This call site produces no `-Wdeprecated-declarations` output in any CPython
build. It will not appear in a compiler log, a CI warning diff, or a `-Werror` gate. The scanner is
the only thing that sees it.

**This is NOT a mechanical rename — and that is the interesting part.** The obvious fix
(`PyUnicodeWriter_WriteStr`) is the fix the comment explicitly warns against. From
`Objects/unicode_writer.c:361-378`:

```c
int
PyUnicodeWriter_WriteStr(PyUnicodeWriter *writer, PyObject *obj)
{
    PyTypeObject *type = Py_TYPE(obj);
    if (type == &PyUnicode_Type) {
        return _PyUnicodeWriter_WriteStr((_PyUnicodeWriter*)writer, obj);
    }
    ...
    PyObject *str = PyObject_Str(obj);
```

The `type == &PyUnicode_Type` test is **exact**, so a `str` *subclass* falls through to
`PyObject_Str(obj)` — running a user `__str__`. gh-148241 was that bug; the current code is the
revert. The vocabulary's `replacement` field says `PyUnicodeWriter_WriteStr`, which for this call
site is wrong.

**The real drop-in, verified.** `PyUnicodeWriter_WriteSubstring(writer, pystr, 0,
PyUnicode_GET_LENGTH(pystr))`:

- `Objects/unicode_writer.c:440-457` type-checks with `PyUnicode_Check(str)` — **accepts subclasses**
  — validates the range, then calls `_PyUnicodeWriter_WriteSubstring` directly. No `PyObject_Str`
  anywhere on the path.
- `Objects/unicode_writer.c:412-414`: `if (start == 0 && end == PyUnicode_GET_LENGTH(str)) return
  _PyUnicodeWriter_WriteStr(writer, str);` — for the full range it tail-calls the exact function
  `_json.c` wants today.

So the substitution is behaviour-identical, uses only public API, and costs three integer
comparisons. `PyUnicodeWriter_WriteSubstring` is already used in this same file at `:542`.

**Guarded twin (in-file).** `write_escaped_ascii` at `Modules/_json.c:243-277` faces the identical
"write this exact string, do not call `str()`" problem at `:263-266` and solves it with the public
`PyUnicodeWriter_WriteASCII(writer, input, input_chars)` — which takes raw `char*` and so cannot
invoke `str()` by construction. The ASCII path was migrated; the unicode path was not, because no
one noticed the substring trick.

**How Python input reaches it.** `json.dumps(x)` where `x` is a `str` subclass, or a `dict` with
such a key. Reached on every `dumps` of any non-ASCII-escaping string.

**Class: FIX** — scheduled removal, compiler-invisible, and the naive migration reintroduces a
previously-fixed bug.

### CONSIDER

#### `_PyBytes_Resize` — 5 sites, soft-deprecated 3.15, no removal date

| site | function context |
|---|---|
| `Modules/_pickle.c:1026` | `_Pickler_GetString` — final trim of the output buffer |
| `Modules/_pickle.c:1103` | `_Pickler_OpcodeBoundary` / buffer grow |
| `Modules/_pickle.c:1417` | frame-header write path |
| `Modules/_pickle.c:5813` | unpickler read path |
| `Modules/_elementtree.c:2832` | `treebuilder` data accumulation |

Replacement is the `PyBytesWriter` API — a **different API shape**, not a rename: create/grow/finish
instead of allocate-then-resize-in-place. Migration is per-buffer restructuring.

**Guarded twin, in the same file.** `Modules/_pickle.c:2783-2836` has *already* migrated one buffer
to the new API: `PyBytesWriter_Create(alloc)` → `PyBytesWriter_GetData` →
`PyBytesWriter_GrowAndUpdatePointer(writer, 10-1, p)` → `PyBytesWriter_FinishWithPointer(writer, p)`
→ `PyBytesWriter_Discard(writer)` on the error path. So the file's own author has demonstrated the
target idiom; the remaining four sites are unfinished migration, not an unexplored design question.
That materially lowers the cost estimate and is the single most useful thing to tell a maintainer
about these five.

**Class: CONSIDER.** No removal date, so nothing breaks; but the in-file twin makes it cheap.

### POLICY

#### `PyMem_NEW` / `PyMem_RESIZE` — 5 sites, all `Modules/_pickle.c`

`:504` `PyMem_RESIZE(data, PyObject *, new_allocated)` · `:789` `PyMem_NEW(PyMemoEntry,
self->mt_allocated)` · `:885` `PyMem_NEW(PyMemoEntry, new_size)` · `:1580` `PyMem_RESIZE(memo_new,
PyObject *, new_size)` · `:1672` `PyMem_NEW(PyObject *, new_size)` · `:7011` `PyMem_RESIZE(marks_new,
Py_ssize_t, alloc)`.

Purely mechanical: `PyMem_NEW` → `PyMem_New`, `PyMem_RESIZE` → `PyMem_Resize`. Same macro, same
argument order, same semantics — `Doc/c-api/memory.rst` lists them in a two-column "Deprecated
aliases" table where the only difference is capitalisation. Soft-deprecated since **2.0** with no
removal date; nothing breaks, ever, on current plans.

One caveat worth stating so a maintainer does not treat this as free: `PyMem_New` and `PyMem_Resize`
are plain macros over `PyMem_Malloc` / `PyMem_Realloc` (`Include/pymem.h:63,73`) and do **not** raise
`MemoryError` — same as the deprecated aliases. Renaming changes nothing about the surrounding
error-handling obligation, and must not be mistaken for fixing it.

**Class: POLICY** — a whole-family sweep decision, not a per-site bug. `_pickle.c` is the only file
in the sample using them; a single commit closes it.

### Safety consequence

**None of the 12 carries one.** All twelve are migration debt: four APIs that still work correctly
today, three of them with no removal date at all. The one class in this scanner that *does* carry a
safety consequence — `gc-untrack-macro-form` — fired zero times, and that zero is genuine (below).

---

## Why `_pickle.c` holds 10 of 12

`_pickle.c` is the only file in the 12-file sample that manages **raw, hand-grown buffers** rather
than PyObject containers. It maintains three hand-rolled dynamic arrays — the pickler memo hash
table (`mt_table`, `PyMemoEntry *`), the unpickler object stack and memo (`PyObject **`), and the
mark stack (`Py_ssize_t *`) — plus a bytes output buffer it grows and trims in place. Those two
habits map one-to-one onto exactly the two deprecated families present:

- raw typed array alloc/grow → `PyMem_NEW` / `PyMem_RESIZE` (6 hits),
- grow-a-bytes-object-in-place → `_PyBytes_Resize` (4 hits).

The rest of the sample simply never does either. `itertoolsmodule.c`, `_collectionsmodule.c` and
`_heapqmodule.c` build `PyObject` containers and let those types own their storage; `_json.c`,
`_datetimemodule.c` and `_csv.c` build strings through the modern writer APIs; `_struct.c`,
`_zoneinfo.c`, `_randommodule.c` and `_queuemodule.c` allocate fixed-size structs. So the
concentration is not a hotspot in the risk sense — it is the sample's one file whose *problem domain*
is buffer management, and both deprecated families are buffer-management APIs. A file doing
serialisation into a growable byte stream is the natural home for the two oldest allocation idioms
CPython still ships.

Corroborating this reading: the ratio holds tree-wide. Across all of `Modules/`, the same two
families are 22 (`PyMem_NEW`) + 19 (`_PyBytes_Resize`) + 5 (`PyMem_RESIZE`) + 7 (`PyMem_FREE`) + 3
(`PyMem_DEL`) = 56 of 137 findings.

---

## `gc-untrack-macro-form` sweep — earned zero

**Result: 0 findings, denominator 0.** Not a silent zero.

Swept all 12 files for both spellings:

- `_PyObject_GC_UNTRACK` (the strict macro, asserts the object *is* tracked): **0 occurrences** in
  any of the 12 files.
- `PyObject_GC_UnTrack` (the tolerant public function, re-checks `_PyObject_GC_IS_TRACKED`): **44
  occurrences** — `itertoolsmodule.c` 22, `_pickle.c` 5, `_elementtree.c` 4, `_collectionsmodule.c` 4,
  `_csv.c` 3, `_json.c` 2, `_struct.c` 2, `_zoneinfo.c` 1, `_queuemodule.c` 1.
  `_datetimemodule.c`, `_heapqmodule.c` and `_randommodule.c` have no untrack call at all (checked
  for every `GC_Track`/`GC_UnTrack`/`GC_TRACK`/`GC_UNTRACK` spelling in `_datetimemodule.c`: none).

The rule requires the strict macro form in a `tp_dealloc`. With zero macro uses in the sample, the
rule cannot fire, and no true positive can be hiding. The catalogued exemplars (`odictobject.c:1718`,
`dictobject.c:5662`) have no analogue here: the `Modules/` types in this sample use the tolerant
function everywhere, which is safe on a pre-track free path by construction.

### Scope escape — the 13 `Modules/` macro uses, all checked

`_PyObject_GC_UNTRACK` appears 13 times in all of `Modules/`, every one in `Modules/_io/`:
`fileio.c:575`, `bytesio.c:1079`, `iobase.c:388`, `bufferedio.c:432` and `:2329`,
`winconsoleio.c:521`, `textio.c:294` and `:1540`, `stringio.c:637` (the other 4 are the
`#include "pycore_object.h" // _PyObject_GC_UNTRACK()` comments).

All are **ACCEPTABLE true negatives.** The `_io` types are not allocated with `PyObject_GC_New`; they
are allocated through `type->tp_alloc` — e.g. `Modules/_io/stringio.c:655-660`:

```c
assert(type != NULL && type->tp_alloc != NULL);
self = (stringio *)type->tp_alloc(type, 0);
...
/* tp_alloc initializes all the fields to zero. So we don't have to
```

`tp_alloc` is `PyType_GenericAlloc`, which GC-tracks the object at allocation for any
`Py_TPFLAGS_HAVE_GC` type. There is therefore **no pre-track window** — the object is tracked from
birth to `tp_dealloc`, which is exactly the case where the strict macro is correct and preferable.
The same allocator comment also rules these out for the sibling `dealloc-of-uninitialized-object`
shape.

Recording this so a later run does not re-hunt `Modules/_io/`: it is a checked negative for this
rule, not an unexamined directory.

**Terminology correction carried forward, as instructed:** `PyObject_GC_UnTrack` is
**untracked-tolerant, not NULL-safe**. `_PyObject_GC_IS_TRACKED` dereferences its argument
unconditionally in both the default and free-threaded builds, so `PyObject_GC_UnTrack(NULL)` still
segfaults. Nothing in this report should be read as claiming a `Py_XDECREF`-style NULL guard.

---

## Recall check — reading, not scanning

**Method.** Extracted all 157 API names cross-referenced in
`Doc/deprecations/c-api-pending-removal-in-{3.14,3.15,3.16,3.18,3.19,3.20,future}.rst` +
`soft-deprecations.rst`, subtracted the 66 vocabulary entries and the 9
`_meta.deliberately_excluded` names, leaving **114 candidates**, and grepped all 114 word-wise
against the 12 sample files. Also swept every `Py_DEPRECATED(...)` / `_Py_DEPRECATED_EXTERNALLY(...)`
marker in `Include/`. Every hit was then hand-verified against the header or doc directive before
being called a gap — the sweep is deliberately over-broad, because the pending-removal pages name
*replacement* APIs as often as deprecated ones.

### Sample result: zero misses

Of the 114 candidates, 22 matched somewhere in the 12 files. **All 22 were replacements or
extraction noise, not deprecated APIs** — `PyErr_GetRaisedException`/`SetRaisedException`,
`PyDict_Pop`, `PyDict_GetItemStringRef`, `PyLong_GetSign`, `PyUnicode_AsUTF8`,
`PyUnicode_InternFromString`, `PyWeakref_GetRef`, `PySlice_Unpack`/`PySlice_AdjustIndices`,
`PyObject_GetAttr`/`PyObject_CallMethod`, `PyImport_ImportModule`, `_PyErr_ChainExceptions1`, the six
public `PyUnicodeWriter_*` functions, `PyComplex_AsCComplex`/`Py_complex` (the replacements for the
deprecated `cval` member, not deprecated themselves), plus two RST-markup artifacts (`writer`,
`immutable`) my extractor picked out of prose.

**So the scanner's 12 is the complete set for this corpus.** I could not find a 13th by reading.
That is a real result and I am not padding it.

### Vocabulary gaps found (all tree-wide, none reachable in this sample)

#### 1. `_PyUnicodeWriter_Prepare` / `_PyUnicodeWriter_PrepareKind` — ~26 invisible call sites

The single most valuable addition. These are the **macro forms** of the two `*Internal` functions the
vocabulary already carries:

```c
Include/cpython/unicodeobject.h:561
#define _PyUnicodeWriter_Prepare(WRITER, LENGTH, MAXCHAR)  ... : _PyUnicodeWriter_PrepareInternal(...)
Include/cpython/unicodeobject.h:581
#define _PyUnicodeWriter_PrepareKind(WRITER, KIND)  ... : _PyUnicodeWriter_PrepareKindInternal(...)
```

`Doc/deprecations/c-api-pending-removal-in-3.18.rst` names both explicitly:
"`_PyUnicodeWriter_Prepare`: (no replacement)" and "`_PyUnicodeWriter_PrepareKind`: (no replacement)".
Same removal release (3.18) and same family as the vocabulary's top-priority group.

Call sites the scanner cannot currently see (excluding `Objects/unicode_writer.c` itself, which
implements the family): `Objects/unicodeobject.c` ×12 (`:1998, 2256, 2545, 2836, 4456, 5211, 5232,
5361, 5859, 6180, 6497, 6891, 7453, 8611, 8811, 9737`), `Objects/unicode_formatter.c` ×4 (`:1169,
1352, 1506, 1719`), `Objects/longobject.c` ×3 (`:2049, 2219, 2393`), `Objects/unicode_format.c:895`,
and one in `Modules/` — `Modules/cjkcodecs/cjkcodecs.h:166`.

These are *more* invisible than the functions: on the fast path the macro inlines a bounds check and
never references the marked symbol at all, so even an external `-Wdeprecated-declarations` build is
inconsistent about them.

Note the naming asymmetry to handle: the docs call them `_PyUnicodeWriter_Prepare` /
`_PyUnicodeWriter_PrepareKind` while the vocabulary's existing entries are the `*Internal` spellings.
Both must be present — a word-boundary matcher will not match one from the other.

```json
{ "name": "_PyUnicodeWriter_Prepare", "tier": "hard-internal", "deprecated_in": "3.14",
  "removed_in": "3.18", "replacement": "",
  "notes": "Macro form of _PyUnicodeWriter_PrepareInternal, Include/cpython/unicodeobject.h:561. Named for removal in Doc/deprecations/c-api-pending-removal-in-3.18.rst ('no replacement'). Fast path inlines the bounds check and never references the marked symbol, so even external builds warn inconsistently. ~24 call sites in Objects/ + Modules/cjkcodecs/cjkcodecs.h:166." }
{ "name": "_PyUnicodeWriter_PrepareKind", "tier": "hard-internal", "deprecated_in": "3.14",
  "removed_in": "3.18", "replacement": "",
  "notes": "Macro form of _PyUnicodeWriter_PrepareKindInternal, Include/cpython/unicodeobject.h:581. Named for removal in Doc/deprecations/c-api-pending-removal-in-3.18.rst. Call sites: Objects/unicodeobject.c:5211, :7453." }
```

#### 2. The 20 `Py_*Flag` global configuration variables — removal in **3.16**, the release in development

`Doc/deprecations/c-api-pending-removal-in-3.16.rst` has two halves. The vocabulary harvested the
**function** half (`PySys_SetArgv`, `PySys_SetArgvEx`, `Py_SetProgramName`, `Py_SetPythonHome` — all
present with `removed_in: "3.16"`) and missed the **variable** half entirely.

Marker confirmed: `Include/cpython/pydebug.h:9` — `Py_DEPRECATED(3.12) PyAPI_DATA(int)
Py_VerboseFlag;` and the same form for the rest. That is the `hard` tier (the compiler *does* warn),
but only for code that reads them, and CPython's own reads are concentrated in initialization code
that nobody rebuilds with `-Werror`.

The 20: `Py_DebugFlag`, `Py_VerboseFlag`, `Py_QuietFlag`, `Py_InteractiveFlag`, `Py_InspectFlag`,
`Py_OptimizeFlag`, `Py_NoSiteFlag`, `Py_BytesWarningFlag`, `Py_FrozenFlag`,
`Py_IgnoreEnvironmentFlag`, `Py_DontWriteBytecodeFlag`, `Py_NoUserSiteDirectory`,
`Py_UnbufferedStdioFlag`, `Py_HashRandomizationFlag`, `Py_IsolatedFlag`,
`Py_LegacyWindowsFSEncodingFlag`, `Py_LegacyWindowsStdioFlag`, `Py_FileSystemDefaultEncoding`,
`Py_HasFileSystemDefaultEncoding`, `Py_FileSystemDefaultEncodeErrors`, `Py_UTF8Mode`.

**This is the nearest removal date of anything in or out of the vocabulary** — nearer than the 3.18
writer family the agent prompt tells me to lead with. Most reads are the definitions plus the
compat bridge in `Python/initconfig.c` (`:143` `GLOBAL(&Py_VerboseFlag, 0)`, `:618` definition, `:678`
`SET_ITEM_INT`) and `Python/preconfig.c` (`:484-518` `COPY_FLAG(isolated, Py_IsolatedFlag)` etc.) —
those are the API's own backwards-compatibility implementation and should be suppressed as
ACCEPTABLE. But there is at least one genuine consumer read: **`Python/sysmodule.c:4533`**

```c
PySys_SetArgvEx(argc, argv, Py_IsolatedFlag == 0);
```

— a single line using a deprecated *function* and a deprecated *variable*, both scheduled for removal
in 3.16. Zero occurrences in `Modules/`, so this gap costs the current run nothing; it would matter
immediately on any scope including `Python/`.

```json
{ "name": "Py_VerboseFlag", "tier": "hard", "deprecated_in": "3.12", "removed_in": "3.16",
  "replacement": "PyConfig.verbose / PyConfig_Get(\"verbose\")",
  "notes": "Py_DEPRECATED(3.12) PyAPI_DATA(int) at Include/cpython/pydebug.h:9; Doc/deprecations/c-api-pending-removal-in-3.16.rst. One of 20 global config variables in that doc's second half; the vocabulary harvested only the function half of the same page. Suppress Python/initconfig.c and Python/preconfig.c — those define the variables and bridge them into PyConfig (the API's own compat implementation)." }
```
…and the same shape for the other 19. All share the doc citation and the suppression note.

#### 3. `PyGen_New` / `PyGen_NewWithQualName` / `PyCoro_New` / `PyAsyncGen_New` — regression guard only

`Include/cpython/genobject.h:19, 20, 32, 43` — all four carry `Py_DEPRECATED(3.16)`, and
`Doc/deprecations/c-api-pending-removal-in-3.18.rst` lists all four with "(no replacement)". Note the
deliberate split: **deprecated in 3.16, removed in 3.18** — the marker version and the doc version
disagree only because they mean different things.

Value is guard-only: the *sole* tree-wide occurrence of each is its own definition in
`Objects/genobject.c` (`:1202, 1208, 1573, 1895`). Zero consumer call sites today. Adding them costs
nothing and catches a future regression; it also exercises the scanner's definition-site suppression
on four fresh cases.

```json
{ "name": "PyGen_New", "tier": "hard", "deprecated_in": "3.16", "removed_in": "3.18",
  "replacement": "",
  "notes": "Py_DEPRECATED(3.16) at Include/cpython/genobject.h:19; removal scheduled in Doc/deprecations/c-api-pending-removal-in-3.18.rst ('no replacement'). Zero consumer call sites at 3.16.0a0 -- only its own definition at Objects/genobject.c:1208. Regression guard." }
```
…likewise `PyGen_NewWithQualName` (`genobject.h:20` / `genobject.c:1202`), `PyCoro_New`
(`genobject.h:32` / `genobject.c:1573`), `PyAsyncGen_New` (`genobject.h:43` / `genobject.c:1895`).

#### 4. `PyComplexObject.cval` — propose, but with an explicit precision warning

`Doc/deprecations/c-api-pending-removal-in-3.20.rst`: "The `cval` field in `PyComplexObject`
(gh-128813). Use `PyComplex_AsCComplex` and `PyComplex_FromCComplex`." Confirmed in
`Doc/whatsnew/3.15.rst`. There is no and can be no compiler marker — `Include/cpython/complexobject.h:29`
is a bare `Py_complex cval;`.

I am proposing this **with a caveat, not a recommendation to just add it**: `cval` is a four-character
identifier and a word-boundary matcher over it will hit unrelated locals across the tree. It needs a
member-access rule (`->cval` / `.cval` on a value of type `PyComplexObject *`), which is a different
matcher than the rest of the vocabulary uses. `Modules/_struct.c` uses the *replacement*
(`PyComplex_AsCComplex` at `:792, 808, 1134, 1149, 1460, 1476`) and never touches `cval`, so the
sample gives no signal either way. If the member-access rule is not cheap, leaving this out is
defensible — a low-value entry that regresses precision is exactly what the 0/13 rewrite was meant to
prevent.

```json
{ "name": "PyComplexObject.cval", "tier": "soft", "deprecated_in": "3.15", "removed_in": "3.20",
  "replacement": "PyComplex_AsCComplex / PyComplex_FromCComplex",
  "match": "member-access",
  "notes": "Struct member, Include/cpython/complexobject.h:29 -- no compiler marker is possible. Doc/deprecations/c-api-pending-removal-in-3.20.rst (gh-128813). REQUIRES a ->cval / .cval member-access rule; a bare word match on a 4-char identifier will false-positive. Do not add under the plain name-match rule." }
```

### Exclusions that should be added to `_meta.deliberately_excluded`

Each of these is named in a pending-removal doc but has **already been removed from `Include/`** at
3.16.0a0 (verified: zero `Include/` files contain the name), so no call site can exist and adding
them would only invite a future re-add. They are exact siblings of the existing `PyWeakref_GetObject`
exclusion:

- `PyImport_ImportModuleNoBlock` — **and this one is a live trap.** The header declaration is gone,
  but the symbol survives for ABI compatibility at `Python/import.c:3645`, whose own body emits
  "PyImport_ImportModuleNoBlock() is deprecated and scheduled for removal". Worse,
  `Modules/_testlimitedcapi/import.c:116` **re-declares the prototype locally** in order to test it,
  and calls it at `:123`, under a comment reading `/* Test PyImport_ImportModuleNoBlock() (removed in
  3.15) */`. A word-matching scanner that added this name would report `_testlimitedcapi/import.c:123`
  as a live deprecated call site — a textbook false positive in test-support code.
- `PyWeakref_GET_OBJECT` (0 occurrences anywhere), `PyUnicode_AsDecodedObject`,
  `PyUnicode_AsDecodedUnicode`, `PyUnicode_AsEncodedObject`, `PyUnicode_AsEncodedUnicode`,
  `Py_GetPath`, `Py_GetPrefix`, `Py_GetExecPrefix`, `Py_GetProgramName`, `Py_GetProgramFullPath`,
  `Py_GetPythonHome` — all 0 `Include/` files, ≤1 tree occurrence each.

---

## Classes bounded (clean negatives, with evidence)

- **`gc-untrack-macro-form` in the 12-file sample** — 0/0. Zero strict-macro uses; 44 tolerant
  function uses. Evidence above.
- **`gc-untrack-macro-form` in `Modules/_io/`** (scope escape, 13 macro uses = 100% of `Modules/`) —
  all ACCEPTABLE. `tp_alloc`-allocated types are GC-tracked at allocation, so there is no pre-track
  window. Evidence: `Modules/_io/stringio.c:655-660`.
- **Additional deprecated APIs in the 12 files** — none. 114 doc-listed candidates swept word-wise;
  all 22 hits hand-verified as replacements or markup artifacts.
- **`hard`-tier findings in the sample** — zero, correctly. The one tree-wide `hard` finding
  (`PySlice_GetIndicesEx`) is outside the 12 files; `_elementtree.c:1833/1891` already uses the
  replacement pair `PySlice_Unpack` + `PySlice_AdjustIndices`, so that file is migrated.
- **Version-guarded / intentional usage** — none of the 12 sits inside a `#if PY_VERSION_HEX < …`
  block. Checked; CPython's own tree has no reason to carry them and does not.

---

## Toolkit assessment

### Precision per rule

| rule | raw | TP | FP | precision | line accuracy |
|---|---|---|---|---|---|
| `deprecated-api` | 12 | 12 | 0 | **100%** | 12/12 exact |
| `gc-untrack-macro-form` | 0 | — | — | n/a (denominator 0) | n/a |

Combined with the reported 20/20 on the sampled `Objects/` check, the rewrite stands at **32/32** on
two different corpora. The rewrite is validated: data-driven vocabulary + word boundaries +
definition-site suppression fixed all three failure modes of the 2021 list, and I reproduced the
substring failure mode in-sample (`_PyErr_ChainExceptions1` ×7) to confirm the guard actually fires
rather than being untested.

The honest caveat: **12 findings across 4 APIs is a small denominator**, and three of the four are
trivially unambiguous macro/function names. The precision number is real but it is not yet a hard
test of the definition-site suppression logic, which had no opportunity to fire in this sample
(none of the 4 APIs is *defined* in the 12 files). `Objects/` exercised that; this run did not.

### Recall gaps found by reading

Four, ranked by call sites recovered. None affects this sample's result — all are tree-wide:

1. `_PyUnicodeWriter_Prepare` / `_PyUnicodeWriter_PrepareKind` — **~26 call sites** currently
   invisible, in the vocabulary's own top-priority family and removal release.
2. The 20 `Py_*Flag` globals — **removal in 3.16**, the release under development; the nearest
   deadline anywhere. Half of one doc page was harvested and half was not.
3. `PyGen_New` ×4 — 0 sites, regression guard.
4. `PyComplexObject.cval` — needs a new matcher; proposed with a precision warning attached.

Root cause of #1 and #2 is the same and worth naming: the vocabulary was harvested per-*name* from
the pending-removal pages, and both gaps are places where a page lists a second form (macro vs
function, variable vs function) that a name-by-name pass reads as a duplicate of something already
captured.

### Prompt problems

1. **The prompt hard-codes the priority family.** It states the highest-yield tier is
   `_Py_DEPRECATED_EXTERNALLY` and that `_PyUnicodeWriter_*`/3.18 is the family to lead with. That was
   correct for `Objects/` and is correct for this sample, but it is a *finding*, not a rule — on any
   scope touching `Python/`, the `Py_*Flag` family at removal-3.16 outranks it. The prompt should say
   "order the report by the vocabulary's own `removed_in`, soonest first" and let the data pick the
   family. As written it would have had me bury the nearest deadline in the recall section.
2. **The report template's "Difficulty" column encodes the wrong axis.** For `_json.c:409` the
   load-bearing fact is not "Medium" — it is that the named replacement is **semantically different**
   and applying it reintroduces gh-148241. A "Difficulty: Medium" cell actively hides that. The
   template already special-cases this once (`PyModule_AddObject … Medium (refcount)`), which shows
   the axis is really *drop-in vs not*.
3. **The prompt says to check whether the replacement "is a drop-in" but the JSON has no field for
   the answer**, so the finding text can't carry it and every agent re-derives it by reading
   `Objects/unicode_writer.c`. That is exactly the kind of per-run rediscovery the informed loop is
   supposed to eliminate.
4. Minor: the prompt's `_PyUnicodeWriter_*` call-site estimate ("~75 in `Objects/` + `Modules/`")
   excludes the two macro forms and is therefore ~26 low. Worth correcting once the entries land, so
   the number stops disagreeing with the scanner.

### Ranked tuning proposals

**1. Add `_PyUnicodeWriter_Prepare` and `_PyUnicodeWriter_PrepareKind`.** (JSON above.) Recovers ~26
call sites in the highest-value tier and removal release; two entries, no new matcher, no precision
risk — the names are long and unambiguous. Also add a regression test asserting the `*Internal` and
macro spellings are matched independently, since that is the exact confusion that caused the gap.

**2. Add the 20 `Py_*Flag` globals with `removed_in: "3.16"`, plus a compat-shim suppression.**
Without the suppression this lands ~60 findings in `Python/initconfig.c` and `Python/preconfig.c`
that are the variables' own definitions and the PyConfig bridge — noise that would look like the
old scanner's definition-site problem returning. With it, the signal is small and sharp:
`Python/sysmodule.c:4533` is a genuine consumer read that pairs a deprecated variable with a
deprecated function at the same removal date. Concretely: suppress a finding whose file defines the
symbol (`^<type> <name> =` at column 0) or whose enclosing file is on a per-entry
`compat_shim_files` list.

**3. Add `drop_in: true|false` + `caveat` to the vocabulary schema, and surface both in `detail`.**
This run's single FIX exists precisely because `replacement` was insufficient: it says
`PyUnicodeWriter_WriteStr`, and following it verbatim reintroduces a fixed bug. Seed it with the two
cases already known — `_PyUnicodeWriter_WriteStr` (`drop_in: false`, caveat: the public function
calls `PyObject_Str` on anything that is not exactly `PyUnicode_Type`; use
`PyUnicodeWriter_WriteSubstring(w, s, 0, PyUnicode_GET_LENGTH(s))`) and `PyModule_AddObject`
(`drop_in: false`, steals only on success). Everything else in the vocabulary is `drop_in: true` and
the field costs nothing. This converts a per-run rediscovery into vocabulary.

**4. Emit a per-file `replacement_already_used` hint.** A one-line grep for the entry's
`replacement` string in the same file, reported in the finding. `_pickle.c` has already migrated one
buffer to `PyBytesWriter_Create` at `:2783` while four `_PyBytes_Resize` calls remain — that single
fact moves those four from "someday, unknown cost" to "finish the migration the file's author
started, idiom demonstrated 700 lines away". Same applies to `_json.c:409` (the whole file is on the
public writer) and would apply to the `PyMem_NEW` family in files that mix cases. Cheap, and it is
the deprecation-tracker's equivalent of the guarded twin.

**5. Add the 11 already-removed names to `_meta.deliberately_excluded`.** Low effort, prevents a
future re-add, and one of them (`PyImport_ImportModuleNoBlock`) would produce a concrete false
positive at `Modules/_testlimitedcapi/import.c:123`, where the test suite re-declares the removed
prototype locally in order to call it.

**6. (defer) `PyComplexObject.cval`.** Needs a member-access matcher. Worth doing when the schema
gains `match: "member-access"`; not worth a bare word-match entry.
