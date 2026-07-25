# Toolkit assessment ledger — informed-explore, Objects/ sample

Running ledger of defects/tuning leads found *about the toolkit* during this run.
Status: `FIXED` (done + verified this session) · `CONFIRMED` (reproduced, not yet fixed) ·
`OPEN` (reported by an agent, needs my verification).

## Verified directly by the orchestrator

### TK-1 — `analyze_history.py` dies on any long history window · **FIXED**
`_run_git` / `_run_git_streaming` passed `text=True` with no `errors=`, so strict UTF-8 decoding
aborted the whole analysis on the first non-UTF-8 commit message.

Reproduced: `analyze_history.py Objects/ --days 13000 --max-commits 20000` →
`{"error": "'utf-8' codec can't decode byte 0xf3 in position 403", "type": "UnicodeDecodeError"}`, exit 1.
Tripped by exactly one commit in 35 years (`42bb126f0aa`, the 2015 `listsort.txt` UTF-8 conversion).

Fixed by adding `errors="replace"` to both subprocess sites. After the fix the full
9,203-commit history of `Objects/` analyses in **11.0 s**, `commit_cap_applied: false`.
243 tests still green; no new lint findings.

**Cross-toolkit propagation lead (not yet done):** all four siblings carry the identical defect —
`cext-review-toolkit`, `rust-ext-review-toolkit`, `rustpy-review-toolkit`, `code-review-toolkit`
each have 2 `text=True` subprocess sites with **no** `errors=`. They have not hit it only because
their targets have shorter/cleaner histories. This is a textbook fix-propagation case and should be
swept across the family.

### TK-2 — informed briefing leaked raw Python dict reprs and hid sibling sites · **FIXED**
`_first_site()` did `str(sites[0])` on a findings-repo `meta.json`, whose `sites` entries are
**dicts**. Every agent in an informed run read:

    | CPY-0001 | recursion | `{'path': 'Objects/tupleobject.c', 'line': 0, 'function': 'tuple_hash', 'note': '...'}` | ...

Worse, `[0]` truncated multi-site findings to one site — so **CPY-0001's second site,
`Objects/unionobject.c (union_hash)`, was invisible to every agent**, which is exactly the sibling
an informed run is supposed to propagate.

Fixed with a `_format_site()` renderer (`path:line (function)`, `line 0` omitted) applied to all
sites up to `_MAX_SITES=4` with a `(+N more)` overflow marker. Now renders:

    | CPY-0001 | recursion | `Objects/tupleobject.c (tuple_hash); Objects/unionobject.c (union_hash)` | ...

243 tests green; no new lint findings.

### TK-3 — unknown CLI flags are silently ignored · **CONFIRMED**
`analyze_history.py Objects/ --months 420` ran happily with the **default 90-day window** and
reported nothing amiss (the flag is `--days`). The hand-rolled flag loop drops unrecognised `--`
args. A silently-ignored window flag produces a confident, wrong temporal analysis.
Fix: collect unknown `--` args and either error out or emit a `notes[]` warning in the envelope.
Note `scan_common.parse_common_args()` has the same swallow-unknown-flags shape.

### TK-4 — the 2000-commit cap truncates silently in practice · **CONFIRMED**
Default `--max-commits 2000`. On `Objects/` with a 35-year window that discards 7,203 of 9,203
commits. The envelope *does* set `commit_cap_applied: true`, but nothing in the agent-facing path
surfaces it, and the analysis reads as complete. Since the uncapped run takes 11 s (TK-1), the cap
is far too tight for CPython. Fix: raise the default substantially and promote
`commit_cap_applied` into `notes[]` so agents see it.

### TK-5 — a C-source toolkit parses Python files with `ast.parse` · **CONFIRMED**
`analyze_history.py` discovers `(".c", ".h", ".py")` and runs `ast.parse` on `.py` files
(inherited from `code-review-toolkit`). Pointed at a CPython checkout it walks `Lib/`, including
deliberately-malformed test data, emitting `SyntaxWarning: invalid decimal literal` etc. from
`Lib/test/test_import/data/syntax_warnings.py` into the toolkit's own stderr. In this toolkit the
target is C; `.py` should be dropped from discovery.

### TK-6 — `known-issues` overstates `line_drifted` · **CONFIRMED**
Whole-tree run: 26 entries → **11 present / 11 line_drifted / 4 absent / 0 file_missing /
0 no_scanner**. `no_scanner: 0` (the v0.7 goal) holds.

But `line_drifted` is assigned whenever findings exist *somewhere in the file* while neither the
line window nor the function matched. For an entry with a **known function name**
(`gh-149146 tuple_dealloc`), "the scanner found things in this file but nothing in `tuple_dealloc`"
is not drift — it is absence *in that function*, which is a materially different signal (drift =
"still there, moved"; absence = "possibly fixed"). Proposal: when `function != "0"` and the
function is present in the file but carries no finding, report a distinct
`absent_in_function` status. Would reclassify roughly 7 of the 11 current `line_drifted` rows.

### TK-7 — new agents are invisible to an already-running session · **OPEN (operational)**
Only the 11 v0.4 agents are registered as dispatchable subagent types in this session; the 12
agents added in v0.5–v0.7 (`recursion-guard-auditor`, `pyerr-clear-auditor`,
`uninitialized-dealloc-auditor`, `git-history-context`, `ft-race-scanner`, `stw-safety-checker`,
`lock-discipline-checker`, `init-bypass-checker`, `parity-checker`, `tsan-report-analyzer`,
`tsan-stress-generator`, `oom-reproducer`) are not, because the plugin was loaded before they
shipped. This run dispatched them by hand (a generic agent instructed to read the agent's own
`.md` definition first), which works but is not the intended path. Worth a line in the README:
after upgrading the plugin, `/reload-plugins` before running `explore` / `informed-explore`.

## Reported by agents, independently verified by the orchestrator

### TK-8 — the tree-sitter chassis silently drops function bodies · **CONFIRMED (highest impact)**
Reported by `pyerr-clear-auditor`; I verified and quantified it independently.

CPython's brace-unbalanced macros defeat `tree-sitter-c`. `Py_BEGIN_ALLOW_THREADS` expands to
`{ PyThreadState *_save; ...` — an opening brace with no textual closer — and
`Py_BEGIN_CRITICAL_SECTION(...)` does the same. The parser emits `ERROR` nodes and
`extract_functions()` silently returns fewer functions. Confirmed ERROR sites:
`Objects/object.c:650` (`Py_BEGIN_ALLOW_THREADS`), `Objects/dictobject.c:650`
(`#ifdef Py_GIL_DISABLED` splitting a construct).

Measured recall loss across all 50 files of `Objects/`, counting only tokens that can *exclusively*
appear inside a function body (so "unattributed" is unambiguously a defect, not a type-slot table):

| in-body-only token | total | unattributed | recall loss |
|---|---|---|---|
| `Py_BEGIN_CRITICAL_SECTION` | 187 | 19 | **10%** |
| `Py_DECREF` | 1477 | 88 | 6% |
| `goto` | 1081 | 62 | 6% |
| `PyErr_Clear` | 83 | 4 | 5% |
| `PyObject_GC_New` | 59 | 3 | 5% |

**Every one of the 23 tree-sitter scanners loses ~5–6% of its population, rising to 10% for
critical-section-bearing code** — exactly what the v0.6 FT detectors target, and a plausible
contributor to `scan_lock_discipline` returning just 1 finding across all of `Objects/`.
Worst-attributed files: `dictobject.c` 50.2% of lines, `odictobject.c` 51.6%, `bytesobject.c` 56.7%.

**Root cause refined (two later agents corrected my first diagnosis — record the correction).**
The brace-unbalanced sync macros are *a* trigger but **not the dominant one**. Measured across
`Objects/`, the two biggest ERROR-node causes are **Argument Clinic `*_METHODDEF` macros
interleaved with brace initializers inside `PyMethodDef[]` tables** and **`_Py_COMP_DIAG_*`**.
This matters for the fix: a macro-allowlist / pre-substitution table keyed on
`Py_BEGIN_ALLOW_THREADS` + `Py_BEGIN_CRITICAL_SECTION` **would miss both of the largest blind
spots**. Do not implement that as the primary fix.

**A second, worse failure mode: silent function *merging*, not just dropping.** When the parse
desynchronizes, `extract_functions()` can emit one record spanning many real functions. Verified:

| file | record | span | effect |
|---|---|---|---|
| `object.c` | `_PyObject_GetAttrId` | lines **1267–3521 (2,254 lines)** | absorbs ~91 real functions |

Every finding anywhere in that 2,254-line range is attributed to `_PyObject_GetAttrId` — confidently
and wrongly — in the shipped JSON. One agent observed exactly this in real output: an STW finding at
line 3412 reported against a function starting at 1267. 93 merged records exist across `Objects/`.
Misattribution is worse than omission, because it survives review as plausible.

Also confirmed at file scale: `extract_functions()` on `dictobject.c` **stops at line 5239 of
8597** — one contiguous 3,358-line blind spot hiding 18 of its 47 critical sections and 100% of the
frozendict family. That is why the `frozendict_pair_hash` sibling (F11) was never surfaced by any
scanner despite an upstream comment pointing straight at it.

**Vendoring status checked:** `tree_sitter_utils.py` is **byte-identical to cext-review-toolkit's**
(the vendoring model is intact — do not fork it here), and all copies already carry the
`preproc_ifdef` gap-fix from the multidict work. This is a **new, distinct** gap: no copy handles
`ERROR` nodes or the ALLOW_THREADS/CRITICAL_SECTION brace-imbalance. Per the family rule the fix
belongs **upstream in cext-review-toolkit**, then syncs forward to all siblings.

Primary fix (cheap, and the important half): add a `parse_errors` count to the report envelope so a
zero result is **auditable** rather than silently wrong. Secondary: a regex fallback for
ERROR-node regions, or pre-substituting the known brace-unbalanced CPython macros before parsing.

### TK-9 — `scan_error_paths.py`'s owned rule has never run · **CONFIRMED**
Reported by `error-path-analyzer`; I read the code and confirm the off-by-one.
At `scan_error_paths.py:158-165`, `sig_start` is first *decremented* onto the return-type line:

```python
sig_start = i - 1
if sig_start > 0 and re.match(r'^[\w\s\*]+$', lines[sig_start - 1].strip()):
    sig_start -= 1              # sig_start now points AT "static bool"
ret_type = lines[sig_start - 1].strip()   # ...then reads the line ABOVE it
```

so `ret_type` is read from the line *above* the return type — usually the previous function's
closing `}` or a blank line. The agent measured the consequence: **82% of functions get an empty
`return_type`, and only 22 of 2861 are classified `PyObject`-returning against ~1645 real ones.**
`return_null_no_exception` — the rule this agent *owns*, and one of the six catalogued bug shapes —
has therefore been evaluating ~1% of its population since it was written. The agent's patch
(`lines[sig_start]`) takes the classification 22 → 1045.

**Do not ship that patch alone**: it takes `Objects/` findings from 5 → 734. It must land together
with a re-scoped, gated replacement rule. Left unfixed for now — this needs the deliberate
design pass described in the synthesis, not a quick edit mid-run.

### TK-10 — `scan_pyerr_clear.py` misses the `_PyErr_Clear(tstate)` spelling · **CONFIRMED**
The scanner matches only the literal `PyErr_Clear`. Verified: `Objects/` has 83 bare `PyErr_Clear`
and `Python/` has **70 `_PyErr_Clear`** — disjoint sets, none of the latter matched. Includes a
live in-scope miss at `Python/pystate.c:836` (`interpreter_clear`, unguarded, no save/restore).
One-line fix. The same private-alias blind spot likely affects other scanners' token tables
(`_PyErr_SetString`, `_PyObject_GC_TRACK`, …) and deserves a sweep.

### TK-11 — catalog entry `OOM-0023` is simply wrong · **CONFIRMED**
`Objects/typeobject.c` `subtype_dealloc` contains **zero** `PyErr_` calls of any kind across its
167 lines, and `git log -S` shows no commit ever removed one. The row is not a fixed bug and not a
scanner miss — it is mis-catalogued. This is why `known-issues` reports it `absent`, and it is the
worked example in the `pyerr-clear-auditor` prompt, so it actively misdirected this run.
Remove from `data/cpython_known_bugs.tsv:30`, `data/cpython_bug_shapes.json`,
`commands/known-issues.md`, and the agent prompt.

### TK-12 — the FP taxonomy contained a factual error that suppresses true positives · **FIXED**
The most damaging defect found in this run, because it makes the toolkit argue *against* real bugs.

`data/cpython_non_bugs.md` ("Guarded by the dispatcher") stated that
`PyObject_Repr`/**`PyObject_Hash`**/`PyObject_RichCompare` "already wrap `Py_EnterRecursiveCall`",
so a leaf slot reached through them is safe. The same claim was in
`agents/recursion-guard-auditor.md:39`.

**`PyObject_Hash` has no recursion guard.** Verified against main @ 3.16.0a0:

| dispatcher | `Objects/object.c` | wraps `_Py_EnterRecursiveCallTstate`? |
|---|---|---|
| `PyObject_Repr` | :759 | yes |
| `PyObject_Str` | :800 | yes |
| `PyObject_RichCompare` | :1099 | yes |
| **`PyObject_Hash`** | **:1158** | **NO** |

An agent that trusted the taxonomy would dismiss the *entire* confirmed recursion class —
`tuple_hash` (CPY-0001/gh-154318), `union_hash`, `ga_hash`, `frozendict_pair_hash` — as
"dispatcher-guarded". That asymmetry between the four dispatchers is precisely what makes the class
real, and it is now stated explicitly in both files with the line references above.

### TK-13 — `OOM-0023` removed from the catalog · **FIXED**
Per TK-11. Removed from `data/cpython_known_bugs.tsv` (with a tombstone comment recording *why*, so
it is not re-added), from `cpython_bug_shapes.json`'s `confirmed_examples`, and from the worked
example in `commands/known-issues.md`. Re-ran `known-issues`: 26 → 25 entries, `absent` 4 → 3.
The `pyerr-clear-in-destructor` shape also gained a `scope_note` recording that its zero is correct
for the destructor class but that the wider gh-146102 class is uncovered.

### TK-14 — three scanner rules are dead code · **REPORTED, verified by reading**
Independently found by three different agents in their own scanners:
- **`scan_refcounts.py`** — the crown-jewel `borrowed-ref-across-call` rule *does not exist*.
  `BORROWED_REF_APIS` feeds `_ANY_API_CALL_RE`, which is never used; `INCREF_APIS`, the `increffed`
  set and a `pass`-bodied block are all dead. Precision on the sample: **0/19**.
- **`scan_null_checks.py`** — the `deref-before-check` rule builds state and appends nothing, which
  is why `high_confidence` is 0 everywhere. The agent prompt tells the agent to prioritise that
  permanently-empty set. The docstring's `PyArg_Parse*` claim is also unimplemented.
  Precision: **0/21**.
- **`scan_error_paths.py`** — see TK-9. Precision: **0/29**.

Aggregate: **0 of 69 candidates** from the three biggest scanners were real. Every genuine finding
in this run came from an agent *reading code*, guided by the briefing's shapes. That is the single
most important calibration result here: the briefing/guarded-twin methodology is carrying the run,
and these three scanners are currently contributing noise rather than signal.

### TK-15 — line numbers are wrong across scanners · **REPORTED by two agents independently**
A constant `+2` drift, compounded by `strip_comments_and_strings()` collapsing block comments and
eating newlines (observed offsets −3…+16). Measured: **111 of 113 `Objects/` findings** from
`scan_null_checks` have wrong lines; `_Py_subs_parameters` reported at 452, true line 460.
Two-line fix plus a mechanical regression test. This silently degrades every report the toolkit
produces and should be fixed before anything else in the tuning pass.

### TK-16 — `scan_init_bypass.py`'s zero on `Objects/` is an artifact, not a clean bill
`Objects/` declares types with the **positional** static `PyTypeObject` form (`X, /* tp_init */`) —
42 occurrences versus 2 designated. The scanner matches only `.tp_init =` and `{Py_tp_init, X}`, so
it sees 2 of 44. Compounding it, `_collect_nullable_fields` runs on `strip_comments(source)`, which
*deletes the `/* tp_init */` comment that is the positional marker* — so adding a regex alone still
yields zero. Proof: run the shipped scanner against pre-gh-144330 `funcobject.c`, which provably had
the bug, and it reports `nullable_fields: 0`. The v0.7 calibration looked good only because
`Modules/_sqlite` uses `PyType_Spec`. Suggested standing canary: check `total_nullable_fields`, not
`total_findings`, before ever reporting a clean negative.

### TK-17 — smaller confirmed scanner defects
- **`scan_uninit_dealloc.py`** gate is the wrong predicate: it asks "is there a `= NULL` before the
  free" instead of "is a member written *after* the free and not also before". Measured swap on
  `Objects/`: **7 → 3 findings, all 3 real**, dropping 4 FPs *and* gaining the real `PyList_New`
  free-threaded bug. Should also be dominator-aware rather than flat-text.
- **`scan_memory_patterns.py`** cannot express the `structseq` shape at all: `_SIZE_ARG_INDEX` has
  no var-object allocator entry, and the multiply lives inside `_PyObject_VAR_SIZE`, so it never
  appears in source. Needs a `varobject_nitems_unguarded` rule (exactly 9 call sites in `Objects/`).
  Its v0.7 GC gate cost **zero** recall (verified) but is unsound in principle — make it type-level,
  not file-level.
- **`scan_ft_races.py`** should add `Py_SETREF/Py_XSETREF(x->f, NULL)` to T3 (caught a live
  segfault), and gate a `lazy_init_partial_guard` type on "≥2 accessors of one field, ≥1 guarded" —
  the guarded twin is the discriminator.
- **`run_oom_sweep.py`** needs a `--setup` phase (arming before freelist-draining setup burns the
  budget) and must not classify ASan's exit-1 as `memory_error`.

### TK-18 — `check_pep7.py` uses a non-standard JSON envelope · **CONFIRMED**
Every other scanner emits `findings: [...]` per the design's §4.2 envelope. `check_pep7.py` emits
`files: [{file, violation_count, violations: [...]}]` with no `findings` key at all, so any generic
consumer that aggregates scanner output silently sees zero. Confirmed by running it. Either
normalize it to the shared envelope or document the exception explicitly.

### TK-19 — `check_pep7.py` is ~0% actionable on CPython's own code · **CONFIRMED, measured**
5,736 violations across `Objects/`. I verified the composition and spot-checked the rules:

| rule | count | verdict |
|---|---|---|
| `line-too-long` | 3192 | **Not CPython policy.** `.editorconfig` sets `trim_trailing_whitespace`, `insert_final_newline`, `indent_style`, `indent_size` — and deliberately **no `max_line_length`**. CPython ships 1,285 lines >79 chars in `Objects/*.c` alone. |
| `missing-braces` | 2199 | Rule implements only half of PEP 7. The text requires braces *but* says **"do not add them to code you are not otherwise modifying."** Correct only in a `--diff-only` mode. |
| `func-call-space` | 235 | **Actively harmful — delete.** Spot-check: `#define MAX_INTMAX_CHARS (5 + ...)` (removing the space converts an *object-like* macro to a *function-like* one, changing semantics), `#elif (SIZEOF_SIZE_T == 4)` (a preprocessor conditional, not a call), `assert (...)`. No PEP 7 basis. |
| `keyword-space` | 64 | plausible but unverified |
| `header-guard` | 46 | Fires on `Objects/clinic/*.c.h` (generated, `#include`d once mid-file) and `stringlib/*.h`, where a guard would **break the build**. |

The two rules the agent prompt calls FIX-severity — `tab-indent` and `trailing-whitespace` — fire
**zero** times, independently confirmed by grep. The scanner's severity model is inverted relative
to its actual output.

**One genuine positive:** `check_pep7.py` imports only stdlib — no `strip_comments_and_strings`, no
tree-sitter — so it is immune to *both* the +2 line drift (TK-15) and the chassis defect (TK-8).
**280/280 sample line numbers exact.** It is the only scanner in the toolkit with verified-correct
line numbers.

### TK-20 — the deprecation tracker's pattern list is a 2021 snapshot · **CONFIRMED**
0 candidates in the sample, 0/13 real across `Objects/` — every hit is the deprecated API's own
*definition site* (`PyCFunction_Call` at `call.c:379`, the `PyUnicode_GetSize` tombstone, the
`PyObject_As*Buffer` trio) or comment prose, plus one substring FP (`PyUnicode_AsUnicode` matching
the live `PyUnicode_AsUnicodeEscapeString` — no word boundary). Six of the twelve patterns are zero
tree-wide. Nothing deprecated in 3.12–3.14 is on the list. Needs definition-site suppression, word
boundaries, and promotion to a data-driven `deprecated_c_apis.json`.

### TK-21 — the complexity hotspot threshold is mis-calibrated by ~3.4 points · **CONFIRMED**
`measure_c_complexity.py` on all of `Objects/`: **3,073 functions scored, `hotspot_count: 3`** at the
shipped `score >= 5.0` threshold. The **maximum score anywhere in `Objects/` is 6.5**. On the
14-file sample the threshold flags **zero** functions in 13,250 lines. An absolute cutoff of 5.0 on
an observed 0–6.5 range is effectively "never fires".

More important is what the agent measured about the signal itself. Ground truth was rebuilt from all
eight agents' reports: **25** defect-bearing functions in the sample.
- Ranking **works**: the top 10 by score contain **5** of the 25 (p = 0.00004, 10× enrichment).
- Gating **fails**: **20 of 25 defects sit at the score floor**. A complexity gate discards 80% of
  the real bugs. `descr_get_qualname` — 4 lines, cyclomatic 2, nesting 0, rank 257 — carries a
  confirmed FT race.
- For the recursion class the metric **inverts**: `tuple_repr` 1.6 vs `tuple_hash` 1.0, `ga_repr`
  1.8 vs `ga_hash` 1.0. The guard is itself a branch, so *the guarded twin scores higher than the
  buggy sibling*. Complexity actively points away from this bug class.

Consequences for `hotspots.md`: make the threshold **relative** (top 2%), and stop treating
complexity as a severity input for recursion/FT findings. A proposed new metric,
`manual_cleanup_ladder = returns_with_cleanup × owned_locals` when `goto_count == 0`, scores
`_Py_subs_parameters` at 36 and every clean top-10 function at 0 — worth prototyping, since
**24 of the 25 defect functions have zero gotos**: in CPython a `goto` cleanup ladder is a
*positive* signal, and its absence is the risk marker.

### TK-22 — `measure_c_complexity.py` silently drops 22.4% of functions · **CONFIRMED**
It uses its own regex `find_functions` (so it escapes the tree-sitter chassis defect — on
`dictobject.c` it finds 231 functions where tree-sitter finds 187), but it requires the parameter
list to be on **one line**. Measured: **113 of 505 sample functions (22.4%) dropped**, including 5
of the 25 defect functions and 2 of the top-10 hits. Two scanners, two *different* silent
truncation mechanisms, neither surfaced in the output envelope — which is the general lesson of
TK-8/TK-22: **every extraction path in this toolkit needs a coverage field**.

### TK-23 — `scan_lock_discipline.py` discards half its own data file · **CONFIRMED (one-line fix)**
`scan_lock_discipline.py:86` filters `lock_macros.json` to `type == "critical_section"`, throwing
away the `PyMutex` pair the data file already ships. That blinds it to `weakrefobject.c`'s entire
16-site `LOCK_WEAKREFS` scheme. Combined with the parse cliff, this explains the "1 finding across
118,424 lines" result. Both were hand-checked and are genuinely clean, so the *verdict* was right —
but it was right by luck, not by coverage. The single shipped finding (`dictobject.c:4380`) is an
FP: the `return` is dead code after a `goto slow_exit`, left over from gh-112075.

## Calibration measurements from this run

**Scanner volume, `Objects/` (50 files, 118,424 lines) → 14-file sample (13,250 lines):**

| scanner | Objects/ | sample | runtime |
|---|---|---|---|
| scan_error_paths | 148 | 29 | 0.39 s |
| scan_null_checks | 113 | 21 | 0.40 s |
| scan_refcounts | 98 | 19 | 0.52 s |
| scan_ft_races | 24 | 6 | 1.01 s |
| scan_stw_safety | 13 | 0 | 0.80 s |
| scan_recursion_guards | 9 | 4 | 0.66 s |
| scan_gil_usage | 8 | 0 | 0.20 s |
| scan_uninit_dealloc | 7 | 2 | 0.88 s |
| scan_memory_patterns | 4 | 1 | 1.44 s |
| scan_lock_discipline | 1 | 0 | 0.75 s |
| scan_pyerr_clear | **0** | 0 | 0.58 s |
| scan_init_bypass | **0** | 0 | 0.71 s |

Whole-`Objects/` scan cost across all 12 scanners: **~8.3 s**. Performance is a non-issue; the
budget is entirely in agent triage.

**Recall on the calibration subset: 6/6.** Every catalog entry located in the 5 calibration files
was reported `present` by `known-issues` — `tuple_hash`, `tuple_richcompare` (gh-154318),
`_Py_make_parameters` (gh-154275), `union_hash`, `template_iter` (gh-151815),
`descr_get_qualname` (TSAN-0043).
