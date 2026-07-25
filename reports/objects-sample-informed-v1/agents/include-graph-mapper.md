# include-graph-mapper — Objects/ sample (informed)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (CPython 3.16.0a0)
**Script:** `analyze_includes.py Objects/` (123 files, 879 includes, 217 unique headers)
**Also run:** `analyze_includes.py .` (whole tree — 1145 files, 5268 includes, 1110 unique headers) to get non-scope-local fan-in.

## Scanner volume

raw candidates in sample: n/a (orientation agent, not a findings scanner)
structural findings: 1 CONSIDER + 1 POLICY | toolkit defects found: **6** (2 of them invalidate output sections outright)

---

## Deliverable 1 — API tier per sample file

**Tier verdict is derived by hand.** The script's `api_tiers` block is unusable (see Toolkit assessment, Gap 1): it reported `internal: 0, cpython: 0, public: 173` for `Objects/`. I resolved every include directive against `Include/`, `Include/cpython/`, `Include/internal/` on disk and cross-checked `Misc/stable_abi.toml`.

| sample file | `Include/*.h` | `Include/cpython/*.h` | `Include/internal/*.h` | stable-ABI entries | **tier** |
|---|---|---|---|---|---|
| `tupleobject.c` | `tupleobject.h` | `cpython/tupleobject.h` | `pycore_tuple.h` | 8 | **PUBLIC** |
| `capsule.c` | `pycapsule.h` | — | `pycore_capsule.h` | 13 | **PUBLIC** (no `Py_LIMITED_API` guard anywhere in header — fully stable ABI) |
| `descrobject.c` | `descrobject.h` | `cpython/descrobject.h` | `pycore_descrobject.h` | 7 (PyDescr 4 + PyProperty 1 + PyDictProxy 2) | **PUBLIC** |
| `structseq.c` | `structseq.h` | `cpython/structseq.h` | `pycore_structseq.h` | 7 | **PUBLIC** |
| `weakrefobject.c` | `weakrefobject.h` | `cpython/weakrefobject.h` | `pycore_weakref.h` | 4 | **PUBLIC** |
| `iterobject.c` | `iterobject.h` | — | — | 4 | **PUBLIC** (no `Py_LIMITED_API` guard at all) |
| `genericaliasobject.c` | `genericaliasobject.h` | — | decls live in `pycore_unionobject.h` | 2 (`Py_GenericAlias`) | **PUBLIC** |
| `funcobject.c` | — | `cpython/funcobject.h` | `pycore_function.h` | 0 | **CPYTHON** |
| `odictobject.c` | — | `cpython/odictobject.h` | — | 0 | **CPYTHON** |
| `cellobject.c` | — | `cpython/cellobject.h` | `pycore_cell.h` | 0 | **CPYTHON** |
| `unionobject.c` | — | — | `pycore_unionobject.h` | 0 | **INTERNAL** |
| `templateobject.c` | — | — | `pycore_template.h` | 0 | **INTERNAL** |
| `interpolationobject.c` | — | — | `pycore_interpolation.h` | 0 | **INTERNAL** |
| `lazyimportobject.c` | — | — | `pycore_lazyimportobject.h` | 0 | **INTERNAL** |

### Triage rule this implies — read before using the table

**C-API tier and Python reachability are orthogonal here, and conflating them will mis-rank this sample.**

All four INTERNAL-tier files (`unionobject.c`, `templateobject.c`, `interpolationobject.c`, `lazyimportobject.c`) implement **syntax-level Python types** with no C-API surface at all:

- `unionobject.c` → `tp_name = "typing.Union"` (3.16 merged `types.UnionType` into `typing.Union`), reached by `int | str` **and** `typing.Union[int, str]`
- `templateobject.c` / `interpolationobject.c` → `string.templatelib.Template` / `.Interpolation`, reached by every `t"..."` literal
- `lazyimportobject.c` → the `lazy import x` soft keyword (`Grammar/python.gram:228,233,235`), `sys.set_lazy_imports()`, `builtins.__lazy_import__()`

So: **use tier to rank third-party-breakage / ABI severity; use Python reachability to rank crash severity.** A SIGSEGV in `unionobject.c` (internal tier) outranks a leak in `capsule.c` (13 stable-ABI entries) on crash severity, because `int | str` is one token away from any user.

---

## Deliverable 2 — Fan-in per sample file

**The script's `fan_in` cannot answer this** (Gap 4): because `Include/Python.h` pulls in every public object header, `tupleobject.h` has whole-tree include fan-in of **1** while `PyTuple_*` is referenced by **277 files**. Include fan-in ranks internal `pycore_*.h` correctly and reports ~0 for exactly the public types with the largest blast radius.

Two metrics, both measured:
- **hdr** = files including the type's `pycore_*.h`, whole-tree, from `analyze_includes.py .`
- **sym** = distinct `.c`/`.h` files under `Objects/ Python/ Modules/ Include/ Parser/ Programs/` referencing the type's C-API symbol prefix, excluding the defining file

| sample file | hdr | **sym** | blast radius |
|---|---|---|---|
| `tupleobject.c` | `pycore_tuple.h` = **67** | **277** | **Tree-wide.** Highest fan-in in the sample by 7×. Consumers: 19 Objects/, 19 Modules/, 15 Python/, + Parser/, Tools/jit, stringlib. A tuple bug propagates everywhere. |
| `funcobject.c` | `pycore_function.h` = 18 | **38** | High — function versioning caches are read by the eval loop / specializer |
| `genericaliasobject.c` | (no own header; decls in `pycore_unionobject.h` = 9) | **33** | High — every `__class_getitem__` in the stdlib C modules |
| `structseq.c` | `pycore_structseq.h` = 7 | **28** | High and *wide*: `posixmodule.c`, `timemodule.c`, `signalmodule.c`, `resource.c`, `pwdmodule.c`, `grpmodule.c`, `_threadmodule.c`, 5× `_remote_debugging/`, `Python/errors.c`, `Python/thread.c` |
| `descrobject.c` | `pycore_descrobject.h` = 7 | **23** | High — every type's `__dict__` (mappingproxy) and every C method wrapper |
| `capsule.c` | `pycore_capsule.h` = 3 | **23** | Moderate; but it is the C-extension ABI handshake (`datetime` CAPI, `_socket`, `ctypes`) |
| `weakrefobject.c` | `pycore_weakref.h` = **40** | **22** | High *internal* coupling (40 headers) — GC, dict, type teardown all reach it |
| `cellobject.c` | `pycore_cell.h` = 8 | 14 | Moderate — closures, frame teardown |
| `lazyimportobject.c` | `pycore_lazyimportobject.h` = 10 | **11** | **Higher than "newest file" suggests** — reaches `Objects/object.c:2580` (static type table), `Objects/moduleobject.c:212,1336` (module `__getattr__`), `Python/ceval.c`, `Python/specialize.c`, `Python/bytecodes.c` |
| `interpolationobject.c` | `pycore_interpolation.h` = 9 | 10 | Low, contained to the t-string family |
| `templateobject.c` | `pycore_template.h` = 8 | 9 | Low, contained to the t-string family |
| `iterobject.c` | (no own internal header) | **7** | Low by symbol count but **structurally central**: entered from `Objects/abstract.c:2821`, `Objects/typeobject.c:11114`, `Python/bltinmodule.c:1913` (`iter()`), `Modules/_sre/sre.c:1188` |
| `unionobject.c` | `pycore_unionobject.h` = 9 | 6 | Low C fan-in, **high Python fan-in** (`X | Y` syntax) |
| `odictobject.c` | (`cpython/odictobject.h` = 1) | **5** | **Lowest.** Self-contained; a bug here reaches `collections.OrderedDict` users only |

**Ranking for downstream triage:** `tupleobject.c` ≫ `funcobject.c` ≈ `genericaliasobject.c` > `structseq.c` ≈ `descrobject.c` ≈ `weakrefobject.c` ≈ `capsule.c` > `cellobject.c` ≈ `lazyimportobject.c` > `interpolationobject.c` ≈ `templateobject.c` ≈ `iterobject.c` ≈ `unionobject.c` > `odictobject.c`.

---

## Deliverable 3 — Type families (sibling-hunt boundaries)

Downstream agents hunt siblings *within* a family. Eight families cover the sample; **two of them have their most important sibling outside the 14-file scope** — flagged explicitly.

### F1. Typing-parameter walk — `genericaliasobject.c` + `unionobject.c`
The strongest coupling in the sample, and it directly widens CPY-0002.
`_Py_make_parameters` / `_Py_subs_parameters` are **defined in `genericaliasobject.c`** (`:186`, `:406`) and **declared in `Include/internal/pycore_unionobject.h:18-19`**, then called from `unionobject.c:332` and `unionobject.c:349`.

> **Downstream consequence:** CPY-0002 (`_Py_make_parameters` self-recursion, no `Py_EnterRecursiveCall`) is reachable through **`typing.Union[...]` subscripting as well as `list[...]`**. The recursion agent should test both entry points; a fix in `genericaliasobject.c` covers both call sites but the *reachability* argument needs both named.

Third member **outside the sample**: `Objects/typevarobject.c` — both files include `pycore_typevarobject.h`.
Internal recursion sites: `genericaliasobject.c:231` (make_parameters), `:482` (subs_parameters).

### F2. Container hash / richcompare — `tupleobject.c`, `unionobject.c`, `genericaliasobject.c`, `weakrefobject.c`, `descrobject.c`
Slot implementations present: `tuple_hash` / `tuple_richcompare`; `union_hash` / `union_richcompare`; `ga_hash` / `ga_richcompare`; `weakref_hash` / `weakref_richcompare`; `mappingproxy_hash` / `mappingproxy_richcompare`, `wrapper_hash` / `wrapper_richcompare`; `cell_richcompare`.

**Guarded-twin map (measured, `Py_EnterRecursiveCall` / `Py_ReprEnter` counts per file):**

| file | `Py_EnterRecursiveCall` | `Py_ReprEnter` | asymmetry |
|---|---|---|---|
| `tupleobject.c` | 0 | 1 (`:298`/`:346`/`:351`) | **repr guarded, hash not** — this *is* CPY-0001 |
| `odictobject.c` | 0 | 1 (`:1448`/`:1464`) | repr guarded, richcompare not |
| `descrobject.c` | 2 (`_Py_EnterRecursiveCallTstate` `:300`, 7 Leave sites) | 0 | guard is on the descriptor **call** path only, not on `mappingproxy_repr` / `wrapper_hash` |
| `genericaliasobject.c`, `unionobject.c`, `templateobject.c`, `funcobject.c`, `weakrefobject.c`, `structseq.c`, `iterobject.c`, `capsule.c`, `interpolationobject.c`, `cellobject.c`, `lazyimportobject.c` | 0 | 0 | no guard anywhere |

> **Sibling outside the sample:** `Objects/tupleobject.c:367` carries the comment *"If you update this code, update also frozendict_pair_hash() which copied"*. That sibling is at **`Objects/dictobject.c:8415`** (used at `:8462`) — **not in the 14-file scope.** The gh-154318 fix-propagation hunt cannot be completed inside the sample; flag this as a scope escape rather than a clean negative.

### F3. Iterator family — `iterobject.c` + 4 per-type `tp_iternext` slots
`iterobject.c` holds the three generic iterators (`PySeqIter_Type:151`, `PyCallIter_Type:277`, `_PyAnextAwaitable_Type:498`). Per-type `tp_iternext` implementations elsewhere in the sample: `ga_iternext` (genericaliasobject.c), `templateiter_next` (templateobject.c — **this is gh-151815**), `proxy_iternext` (weakrefobject.c), `odictiter_iternext` (odictobject.c). **5 of 14 sample files.**
Generic-iterator entry points: `Objects/abstract.c:2821`, `Objects/typeobject.c:11114`, `Python/bltinmodule.c:1913`, `Modules/_sre/sre.c:1188`.

### F4. t-string family — `templateobject.c` ↔ `interpolationobject.c`
`templateobject.c` includes `pycore_interpolation.h` and calls `_PyInterpolation_CheckExact()` at `:113`; builds a `PyTuple_New(interpolationslen)` at `:137` (→ pulls in F2/tuple). Both internal-tier, both new in 3.14, both least-reviewed. gh-151815 (`template_iter:225`, uninit-dealloc) lives here — the guarded-twin search should cover `templateiter_dealloc:75` / `template_dealloc:393` / `interpolation_dealloc:154`.

### F5. Descriptor / proxy family — `descrobject.c` (self-contained in sample)
7 Python-visible types in one file: `mappingproxy`, `property`, `member_descriptor`, `getset_descriptor`, `method_descriptor`, `wrapper_descriptor`, `classmethod_descriptor`. TSAN-0043 (`descr_get_qualname` lazy init) lives here. The 7 `_Py_LeaveRecursiveCallTstate` sites (`:328`–`:477`) are the *call*-path guards, useful as a same-file guarded twin for anything on the call path — but note they do **not** cover the repr/hash slots.

### F6. Dict-duality family — `odictobject.c` ↔ `Objects/dictobject.c` (**outside sample**)
`odictobject.c` includes `pycore_dict.h` + `pycore_critical_section.h`; `odict_richcompare` delegates to `dict_richcompare`. The linked-list/dict duality means the FT and refcount agents must read `dictobject.c` to judge `odictobject.c` findings — scope escape.

### F7. Callable / closure family — `funcobject.c` ↔ `cellobject.c`
Both CPYTHON tier. `funcobject.c` additionally includes `pycore_code.h`, `pycore_optimizer.h`, `pycore_object_deferred.h` — function versioning caches read by the specializer. FT-relevant.

### F8. Import-machinery family — `lazyimportobject.c` (reaches far outside Objects/)
`PyLazyImport` is referenced from `Objects/object.c`, `Objects/moduleobject.c`, `Modules/_typesmodule.c` (`EXPORT_STATIC_TYPE("LazyImportType", ...)`), `Python/ceval.c`, `Python/specialize.c`, `Python/import.c`, `Python/bytecodes.c`. The resolution point is `Objects/moduleobject.c:1336` → `_PyImport_LoadLazyImportTstate`, i.e. **a lazy-import object is materialized inside a module `__getattr__`** — a re-entrancy surface the file itself does not show.

---

## Deliverable 4 — Python-level reachability

| sample file | Python surface that reaches it |
|---|---|
| `tupleobject.c` | `()`, `tuple()`, every literal, `*args` packing, every multi-return in the C API, `namedtuple` base |
| `genericaliasobject.c` | `list[int]`, `dict[str, T]`, any `__class_getitem__`, `types.GenericAlias`, `iter(list[int])` |
| `unionobject.c` | `int \| str`, `X \| Y`, `typing.Union[...]` (merged in 3.16), `Optional[T]` |
| `templateobject.c` | `t"..."` literals, `string.templatelib.Template`, `Template.__iter__` / `.strings` / `.interpolations` |
| `interpolationobject.c` | `t"{expr}"` → `string.templatelib.Interpolation`, `.value` / `.expression` / `.conversion` / `.format_spec` |
| `descrobject.c` | `type.__dict__` (mappingproxy), `property()`, `@classmethod`/`@staticmethod` descriptors, any C-defined method accessed unbound, `SomeType.some_slot` |
| `odictobject.c` | `collections.OrderedDict` (C impl), `.move_to_end()`, `.popitem()`, `==` against `dict` |
| `funcobject.c` | `def`, `lambda`, `__defaults__` / `__kwdefaults__` / `__code__` / `__annotations__` assignment, `functools.partial` targets, `classmethod`/`staticmethod` objects |
| `weakrefobject.c` | `weakref.ref(o, callback)`, `weakref.proxy(o)`, `WeakValueDictionary` / `WeakSet`, and **callbacks run at arbitrary teardown points** — the re-entrancy surface |
| `structseq.c` | `os.stat()`, `time.localtime()`, `sys.version_info`, `sys.flags`, `sys.float_info`, `resource.getrusage()`, `pwd.getpwnam()`, `os.times()` |
| `iterobject.c` | `iter(seq)`, `iter(callable, sentinel)`, `anext(ait, default)`, any `for` over a non-iterator sequence, `re.finditer` internals |
| `capsule.c` | Not directly constructible from Python. Reached via C extensions publishing a CAPI (`datetime.datetime_CAPI`, `_socket.CAPI`, `ctypes.pythonapi`), and via `repr()` of a capsule object (`capsule_repr:360`) |
| `cellobject.c` | closures (`def outer(): x=1; def inner(): return x`), `fn.__closure__[0].cell_contents` (read **and write**), `types.CellType()` |
| `lazyimportobject.c` | `lazy import x` / `lazy from x import y` soft keyword (`Grammar/python.gram:228,233,235`), `sys.set_lazy_imports()`, `sys.set_lazy_imports_filter()`, `builtins.__lazy_import__()`, `types.LazyImportType`; materialized on module attribute access |

**Reachability-first ranking (crash severity):** `tupleobject.c` (universal) > `unionobject.c` / `genericaliasobject.c` (one operator away, user-controlled nesting depth) > `weakrefobject.c` (user callbacks at teardown) > `cellobject.c` (`cell_contents` is writable) > `descrobject.c` / `funcobject.c` / `odictobject.c` / `structseq.c` / `iterobject.c` > `templateobject.c` / `interpolationobject.c` / `lazyimportobject.c` (new syntax, low deployment) > `capsule.c` (needs a C extension in the loop).

---

## Findings

### CONSIDER

**`Include/internal/pycore_structs.h:55` — genuine include cycle with `pycore_context.h`, and a violated file invariant.**
`pycore_structs.h:55` does `#include "pycore_context.h"`; `pycore_context.h:8` does `#include "pycore_structs.h"`. This is the **only** real cycle in the 1145-file tree (found by my hand-resolved re-analysis; the script reports 0 — see Gap 2).

- *What breaks:* nothing today. Both headers have proper guards, and `_PyContextTokenMissing` is not used inside `pycore_structs.h` below line 55 — the include exists for downstream consumers, not for structs.h itself. So the include-context-first ordering (guard already set → structs.h's line-55 include no-ops) is currently harmless.
- *Latent hazard:* the moment any `pycore_context.h` symbol is used inside `pycore_structs.h`, the build breaks only in TUs that reach `pycore_context.h` first — an order-dependent, hard-to-diagnose failure.
- *Guarded twin:* the file's own stated contract, lines 1–2: *"This files contains various key structs that are widely used **and do not depend on other headers**."* That invariant is false as of `a1aeec61c43` / `6827c5129c5` (gh-131238 core header refactor).
- *Python-level input:* none — build-time only.
- *Classification:* **CONSIDER**. Not a runtime bug, not reachable from Python, so not FIX. Worth reporting because it's a documented invariant that regressed silently and the mid-file include placement (line 55, after struct definitions) is unusual enough to be accidental.

### POLICY / ACCEPTABLE

- **ACCEPTABLE** — `Objects/mimalloc/`, `Modules/_hacl/`, `Modules/expat/` `#include` `.c` files textually (`alloc.c`, `../unix/prim.c`, `Hacl_Hash_Blake2b_Simd256.c`). Vendored third-party build convention, not CPython style. 35 such entries tree-wide.
- **ACCEPTABLE** — `clinic/*.c.h` includes (160 unique tree-wide, in 6 of the 14 sample files). Generated, expected.
- **POLICY** — CPython includes internal headers by bare name (`#include "pycore_object.h"`) rather than by tier-qualified path. Correct given `Include/internal` is on the build include path; noted only because it is the root cause of the toolkit defect below.

---

## Classes bounded (clean negatives)

- **Include cycles in `Objects/`: genuinely zero.** Verified by hand-resolving all 4054 local include directives tree-wide against `Include/`, `Include/cpython/`, `Include/internal/` and the including file's own directory, then running DFS on the resolved graph. Exactly one cycle exists in all of CPython and it is in `Include/internal/`, not `Objects/`. *This is a real negative — but note it is my result, not the script's; the script's `cycles: 0` is a tautology (Gap 2) and must not be cited as evidence.*
- **No API-tier violation in the sample.** All 14 files are core-build TUs and are entitled to `pycore_*.h`. No sample file reaches into another component's private header inappropriately; the one cross-file internal dependency (`unionobject.c` → `_Py_make_parameters` via `pycore_unionobject.h`) is a deliberate shared declaration.
- **Include guards: all sample-relevant headers have them.** Both members of the one cycle are correctly guarded (`Py_INTERNAL_STRUCTS_H`, `Py_INTERNAL_CONTEXT_H`), which is why the cycle is currently benign.
- **Over-inclusion: not a concern in this sample.** Sample fan-out ranges 4–14 (`capsule.c` 4, `funcobject.c` 14) against an `Objects/` max of 78 (`unicodeobject.c`). Nothing in the sample is near the over-inclusion range.

---

## Toolkit assessment

**Bottom line: `analyze_includes.py` gave me the raw include edges and nothing else I could use. Both of my primary deliverables (tier, fan-in) had to be derived by hand, and two of the script's five output sections are not merely imprecise — they are structurally incapable of being correct on CPython.**

### Precision — which rules produced wrong output

**Gap 1 (blocking) — `classify_api_tier` cannot classify CPython's own internal headers.**
`analyze_includes.py:104-111` classifies on the raw directive **text**. CPython `.c` files write `#include "pycore_object.h"` (bare), because `Include/internal` is on the build include path. Nothing matches `internal/` or `cpython/`, so everything falls through to the `return "public"` default.

Measured on `Objects/`: `{"public": 173, "cpython": 0, "internal": 0, "system": 44}` — for a directory where *every single file* includes `pycore_object.h`.
Measured tree-wide: **148 headers literally named `pycore_*.h` are bucketed as "public"**. The 39 headers that *did* land in `internal` are all `internal/Hacl_*.h` from `Modules/_hacl` — a vendored third-party directory that happens to have an `internal/` subdirectory. **Zero of the 39 are CPython API-tier internal headers.** Every number in the `api_tiers` block and the four `*_headers` counts in `summary` is wrong.

**Gap 2 (blocking) — `cycles` is structurally always `[]`.**
`analyze:180` builds `simple_graph[rel].append(header)` — nodes keyed by repo-relative path (`Objects/tupleobject.c`), edges by raw directive text (`pycore_tuple.h`). The two namespaces never meet.
Measured: of **1110** unique edge targets tree-wide, only **5** are also node keys (`pyconfig.h` + 4 `Python/frozen_modules/*.h`), and all 5 are graph leaves. The DFS at `:114-140` therefore never traverses a single header→header edge. `cycles_found: 0` is a tautology, not a result — it would be `0` on a codebase built entirely out of mutually-including headers.
Proof that it hides real signal: my hand-resolved re-analysis found the genuine `pycore_structs.h` ↔ `pycore_context.h` cycle reported above.

**Gap 3 — `public` is a catch-all "local include" bucket, not an API tier.**
Tree-wide it contains **35 non-`.h` entries** (`alloc.c`, `../unix/prim.c`, `_ssl/cert.c`, `_testcapi_feature_macros.inc`), plus 160 `clinic/*.c.h` generated headers, plus `stringlib/*.h`, plus all of `mimalloc/`. Calling this "Public (stable C API)" in the output template is actively misleading.

### Recall gaps — what I needed that the script does not produce

**Gap 4 — include fan-in is the wrong fan-in for a type-level review, and the script offers no alternative.**
Because `Include/Python.h` includes all 10 public object headers of this sample (`Python.h:97-124`), each has whole-tree include fan-in of exactly **1**:

```
tupleobject.h        1        vs   PyTuple_*        referenced by 277 files
weakrefobject.h      1        vs   PyWeakref_*      referenced by  22 files
cpython/odictobject.h 1       vs   PyODict*         referenced by   5 files
```

The script ranks `pycore_tuple.h` (67) correctly but reports ~0 for exactly the *public-tier* types with the largest blast radius. Every fan-in number in this report's Deliverable 2 came from `grep -rlE '<symbol-prefix>'`, not from the script.

**Gap 5 — fan-in is silently scope-local.**
`analyze_includes.py Objects/` reports `pycore_tuple.h: 20`; the whole-tree count is **67**. Nothing in the JSON says the counts are relative to `scan_root`. I only caught the 3.4× understatement because I ran the whole tree as a control. An agent that ran only the prescribed command would have reported 20 as "how many files depend on tuple internals."

**Gap 6 — no reverse index.** `include_graph` is forward-only. "Who depends on me?" — the actual preflight question — requires a full re-scan by the consumer.

### Prompt issues (my agent definition)

1. **The output template presumes the tier field is trustworthy.** It specifies a `| Header | Included By | Tier |` table and an `### API Tiers` table with per-tier counts. Filling those in verbatim from the script would have produced a confidently wrong report claiming CPython's `Objects/` uses zero internal headers. Nothing in the prompt says "sanity-check the tier output."
2. **"Count accurately: report exact numbers from the script, not estimates"** points the wrong way when the script's numbers are wrong. It should be "report exact numbers, and verify each section is measuring what its name claims before quoting it."
3. **The `Python.h includes everything` guideline is right but stops one step short.** It says high fan-in for `Python.h` is expected. It does not state the actionable consequence: *because* `Python.h` is a mega-include, every public header's fan-in collapses to 1, so include fan-in cannot rank public-tier types at all.
4. **No guidance on tier-vs-reachability.** For this sample the two diverge sharply (four Python-syntax-level types are internal-tier). The prompt's rule "a bug in a public-API-exposed type outranks the same bug in an internal one" would have de-ranked `unionobject.c` — reachable via `int | str` — below `capsule.c`, which needs a C extension in the loop. The rule needs the reachability modifier.

### Concrete tuning proposals (ranked by value)

1. **Resolve include directives to on-disk paths before classifying.** Single fix, repairs Gaps 1, 2, and 3 at once. In `analyze()`, resolve each directive against `[<includer's own dir>, Include/, Include/cpython/, Include/internal/, <project_root>]` and key both `simple_graph` nodes and edges by the resolved repo-relative path. Then `classify_api_tier` takes a resolved path and the existing prefix checks work as written.
   *Validated:* I implemented exactly this. Tree-wide result goes from `{public: 704, cpython: 60, internal: 39}` to `{public: 78, cpython: 63, internal: 153, generated: 160, vendored: 72, other-local: 143, unresolved: 83}` — 237 of 4054 local directives (5.8%) stay unresolved, all platform-conditional (`windows.h`-adjacent, `PC/`). And cycle detection immediately surfaces the real `pycore_structs.h` ↔ `pycore_context.h` cycle.

2. **Cheap fallback if (1) is deferred: one line in `classify_api_tier`.**
   ```python
   if header.startswith("pycore_") or header.startswith("internal/"):
       return "internal"
   ```
   Recovers 148 of 153 internal headers with no filesystem access. Does **not** fix cycles (Gap 2) — that needs real resolution.

3. **Add a `symbol_fan_in` section.** For each `Objects/*.c` / `Modules/*.c`, derive the exported-symbol prefix from `PyTypeObject <Name>_Type` / `PyAPI_FUNC` declarations in its matching header, then count distinct referencing files tree-wide. This is the number a preflight agent actually needs, and it is the only way to rank public-tier types.

4. **Emit `fan_in_scope` in `summary`, or resolve fan-in against `project_root` always.** Minimum: `"fan_in_scope": "scan_root"` plus a note. Better: always count fan-in tree-wide even when `scan_root` is narrower, and add `fan_in_within_scope` alongside.

5. **Split the `public` bucket.** New tier strings: `generated` (`*/clinic/*.c.h`), `vendored` (`Modules/_hacl/`, `Objects/mimalloc/`, `Modules/expat/`, `Modules/_decimal/libmpdec/`), `textual-include` (non-`.h` targets). Keeps `public` meaning "stable C API" so the output template's claim is true.

6. **Emit `reverse_graph`** (`header -> [files including it]`). Free once (1) lands.

7. **Doc line for the agent prompt** — add to the Important Guidelines: *"Because `Python.h` is a mega-include, every public `Include/*.h` header has a whole-tree fan-in of 1. Include fan-in ranks internal `pycore_*.h` headers only. To rank a public-tier type by blast radius, count references to its C-API symbol prefix instead."*

8. **Data-file entry** — a `data/cpython_include_tiers.json` mapping `Objects/<file>.c -> {public, cpython, internal headers, stable_abi_count, symbol_prefixes}` for the ~120 files in `Objects/`. Turns the hand-derivation in Deliverables 1 and 2 into a lookup, and gives every downstream agent tier + fan-in for free.
