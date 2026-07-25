# Toolkit defects — obj-mappings slice

## FIXED IN THIS SESSION (each measured before/after; 853 tests green, was 832)

| defect | fix | measurement |
|---|---|---|
| **`scan_ft_races.py:373`** suppressed `iternext_double_decref` whenever `_has_lock(body)` was true *anywhere* in the function | per-drop span test via the file's existing `_critical_section_spans`; whole-function suppression kept only for locks whose region cannot be delimited (`PyMutex_Lock`, `_PyCriticalSection`, SCREAMING_CASE `*LOCK*(`) | **220 → 222** tree-wide. Both new findings are known-live: `setobject.c:1130 setiter_iternext` (TSAN-0054) and `dictobject.c:6158 dictiter_iternext_threadsafe`. **0 FP, 0 losses.** +3 tests |
| **D-27** `recursion_prone_slot_functions` incremented on `slot is not None` | counts `_RECURSION_PRONE_CALLER_SLOTS` membership; publishes a `slot_classification` breakdown (`classified_total` / `from_slot_map` / `from_name_suffix` / `recursion_prone`) | `Objects/` **158 → 33**, `Modules/` 124 → 27, `Python/` 31 → 8. **Detection unchanged** (61/17/15 findings). +3 tests |
| **D-21** `check_pep7` `missing-braces` had no `else` alternative and matched `raw_line` | added `else` / `else if`; anchor moved to the comment-stripped `clean` | slice **185 → 195**; all 10 new verified true positives by reading, incl. `dictobject.c:4717` (the trailing-comment case). Recall 94.4% → ~99.5%. +5 tests |
| **D-19** `measure_c_complexity` counted commas inside function-pointer parameters | `_count_top_level_params` counts at paren depth 0 | `do_lookup` **10 → 5** params, leaves the hotspot list; `set_do_lookup`'s genuine 7 unaffected. +4 tests |
| **D-20** a smaller file silently contributed 0 hotspots in a merged run | `summary.files_without_hotspots` + a note; only populated when something *did* make the cut | merged dict+set run now names `Objects/setobject.c`. +2 tests |
| **`PyErr_FormatUnraisable` missing from `PYTHON_REACHING_APIS`** (`scan_refcounts`) | added, with `_PyErr_WriteUnraisableMsg` | **+1** tree-wide: `typeobject.c:1201 _PyType_Modified_Unlocked` — CPY-0096's own site. 0 FP |
| **`lock_macros.json`** lacked `FT_MUTEX_LOCK_FLAGS` / `FT_MUTEX_UNLOCK` | added to the `PyMutex` pair | made the two `DONT_DETACH` acquires at `dictobject.c:8252`/`:8272` visible — the reason the slice's population read as 7 when it is 9 |
| **`scan_lock_discipline` `_norm_args`** compared the *whole* argument text, so a flags-taking acquire never matched its release | lock identity is the **first** argument (`_first_top_level_arg`) | **found only because the `lock_macros.json` fix exposed it**: 4 false positives appeared in `PyDict_AddWatcher`/`ClearWatcher`, all `goto done` ladders that DO unlock. Back to **0/0/0**, genuine leaks still caught. Pre-existing and broader — it applies to `PyMutex_LockFlags`, hence to `LOCK_KEYS`. +4 tests |

**Two process notes worth keeping.** The `lock_macros.json` addition is correct in isolation but would have *worsened* the scanner if shipped alone — the campaign's rule (measure the delta, don't just make the change) is what caught it. And my own `files_without_hotspots` fix was wrong on first attempt: with no hotspots anywhere it claimed a file had been squeezed out. Its own test caught that.

Still open from the list below: D-18's clinic `_impl` lock association (measured 0 → 3, precision 3/3, but it does **not** recover CPY-0096 or CPY-0107), D-22's `function_watchlist`, D-25's `tp_base` model, D-28's positional slot-table parsing, and the two new rules the campaign wants — `lock_held_across_python` (keyed on lock flavour) and `stale_bound_across_python_call`.

---


Running ledger. Numbering continues from the obj-typeobject pass-2 ledger (D-1..D-17),
so this slice starts at **D-18**.

Every entry must say how it was MEASURED. A proposal without a before/after measurement
does not ship — that rule exists because D-13's flagship proposal added 65 findings and
recovered 0 of 4 known misses, and was rejected on its own numbers.

---

## D-18 — Argument Clinic emits critical sections into a generated header the scanners never read

**Severity: high. This is a false-negative generator for every lock-reasoning rule.**

`@critical_section` in an Argument Clinic block does not put the lock in the `.c` file. The
generated wrapper in `Objects/clinic/<name>.c.h` holds it, and the `_impl` function in the
`.c` file is called from inside that region with no syntactic marker.

Independently verified by counting (not taken from the agent's report):

| file | `Py_BEGIN_CRITICAL_SECTION` | of which `CS2` |
|---|---|---|
| `Objects/clinic/setobject.c.h` | 14 | 4 |
| `Objects/clinic/dictobject.c.h` | 3 | — |
| `Objects/setobject.c` | 31 | — |
| `Objects/dictobject.c` | 48 | — |

The 13 set methods locked only from the generated header:
`set.add`, `set.clear`, `set.copy`, `set.discard`, `set.pop`, `set.remove`,
`set.isdisjoint`, `set.issubset`, `set.issuperset`, `set.symmetric_difference`,
`set.__reduce__`, `set.__sizeof__`, `frozenset.copy`.

Consequence: a scanner reading only the `.c` file concludes `set.add` and friends are
**unlocked**, and concludes `set.issubset` / `issuperset` / `isdisjoint` /
`symmetric_difference` hold a *single* lock when they actually hold a `CS2`. Both
directions are wrong, and the second is the dangerous one — a two-object lock reported as
one-object is exactly the deadlock shape `scan_lock_discipline` exists to find.

### SETTLED — and both earlier explanations, including mine, were wrong

Two hypotheses were advanced and both are **false**. Recording them because the wrong one
would have sent the fix in the wrong direction:

- ~~Mine: the Clinic header is unread, and that explains the zero.~~ The scanner **does**
  read `clinic/*.c.h` — they are ordinary `.h` files in the scan set.
- ~~macro-hygiene's: the rule looks for `PyThread_acquire_lock`, not
  `Py_BEGIN_CRITICAL_SECTION`.~~ Also false. The rule already resolves
  `LOCK_KEYS` / `LOCK_KEYS_IF_SPLIT` / `UNLOCK_KEYS*` to the `python_mutex` family via
  `resolve_local_lock_macros`, and counts 818 `critical_section_functions` tree-wide.

**The real split, re-derived by hand:** the zero is
- **earned** for the shapes the rule models — 99 lock windows read by hand (54 dict, 30 set,
  15 clinic); 5 contain an inner `return`/`goto`; all 5 jump to a label *inside* the section
  immediately above the `END`. Zero leaks. That is a true negative.
- **structural** for what actually bites this code. Both recorded findings here (CPY-0096,
  CPY-0107) are "arbitrary Python under a non-reentrant `_Py_LOCK_DONT_DETACH` mutex" —
  neither a leak nor a nesting. **No rule exists for that shape.** That is the gap worth
  building.

### The real Clinic defect, measured

Not "the header is unread" but: **the wrapper's lock is never associated with the `_impl` it
wraps.** Patch applied → measured → reverted (tree verified clean).

| | before | after |
|---|---|---|
| findings tree-wide | 0 | **3** (precision 3/3) |
| `critical_section_functions` | 818 | **1281** (+57%) |
| clinic-locked impls found | — | 468 across 34 files |

**It does not recover CPY-0096 or CPY-0107** — those need the new lock-across-Python rule,
not this association fix. Both are worth doing; neither substitutes for the other.

**Affects:** `scan_lock_discipline` certainly; `scan_ft_races` and `scan_stw_safety`
plausibly (any rule whose premise is "is this call inside a lock scope?"). The C chassis is
shared, so a fix belongs **upstream in cext-review-toolkit** and syncs forward to `ft` and
`cpython` — do not fork it here.

**Status:** confirmed, unfixed. Assigned to the lock-discipline agent to measure a proposal.

---

## D-19 — `measure_c_complexity` counts function-pointer parameter types as parameters

`do_lookup` has 5 parameters and is reported as having **10** (4 scalars plus the 6 types
inside the inner function-pointer declarator). Reproduced in isolation: a 5-param function
reports 10 and scores 2.0 instead of 1.0.

Consequence: `do_lookup`'s entire hotspot placement is an artifact. Corrected, it scores 1.0
and leaves the list. 2 of the 8 merged hotspots for this slice are noise — the other being
`_PyDict_CheckConsistency`, which does not exist in a release build (reachable only under
`assert(ASSERT_CONSISTENT)`).

**Status:** confirmed by the c-complexity-analyzer with an isolated reproduction. Unfixed.

---

## D-20 — `--top-percent` silently yields zero hotspots for the smaller file in a merged run

Running the complexity scanner over `dictobject.c` + `setobject.c` together gave
`setobject.c` **zero** hotspots — the percentile is computed over the merged population, so
the smaller file is squeezed out entirely without a warning. Silent truncation reads as
"nothing here", which is the same failure mode as a zero denominator.

**Status:** confirmed. Unfixed. Fix should either partition by file or emit a note.

---

## D-21 — `check_pep7`'s `missing-braces` rule has no `else` alternative

Measured against a tree-sitter-c ground truth built independently of the lexical checker:
**185/185 true positives, 0 surviving false positives, 11 false negatives** out of 196 real
brace-less bodies → **94.4% recall**.

The `\)\s*$` anchor limit I flagged in the brief is real but is the **minority** cause —
2 of 11 (18%). The dominant cause is that the regex has no `else` branch: **9 of 11 misses
(82%) are bare `else` or `else if`**.

Of the two anchor misses, `dictobject.c:4717` is a one-token fix — the rule matches
`raw_line` where every sibling rule matches the already-computed `clean`, so a trailing
`/* comment */` after the `)` defeats it.

**Status:** measured, not applied (correctly — the agent left the tree clean).

### D-21b — the PR #30 paren-balance fix barely engaged here

Instrumented: the paren-balance walk fired **3 times in 11,825 lines** and was decisive
**once** (`dictobject.c:5221`, a genuine Allman multi-line-condition FP the old 2-line
lookahead reported and the new rule suppresses). So the fix does generalise to code it was
not tuned on — but this slice is a **weak** positive signal, not a strong one; that shape is
roughly 60× denser in typeobject.c.

The post-closing-paren tail check (`tail.startswith("{")`) fired **0 of 3** and has now gone
two full slices without ever executing. **It needs a unit test, not another slice.**

---

## D-22 — `analyze_history`'s `function_churn` cannot produce bug-fix density

`function_churn` counts commits per function but does not fix-classify them, so it cannot
answer "which function is fixed for crashes most often" — the question the campaign actually
asks. On this slice it ranked `_PyDict_CheckConsistency` first (77 mostly-mechanical commits)
while `delitem_common` — 9 crash fixes since 2023 — never surfaced at all.

Proposal: a `function_watchlist` intersecting `function_churn` with
`recent_fixes[].crash_class` via hunk-funcname attribution. The agent reports a working
implementation at ~120 lines running in under 90s over both files' full history.

**Second gap, same script:** no revert/re-land detection, despite that being the richest vein
in this slice's history pass (CPY-0096 is iteration 4; a 2021 revert introduced a crash that
took 4 years 10 months to find).

**Status:** proposed with a working prototype, not applied. Needs the standard before/after
measurement before it ships.

---

## D-23 — complexity does not predict where this slice's bugs are (calibration result, not a defect to fix)

Recorded here because it bears on how the campaign should spend reading effort.

0 of 6 recorded findings appear in the 8-function hotspot list; 4 of 6 sit at the exact score
floor, including `insert_split_key` (CPY-0096, a reproduced hang) at 1.0. The agent correctly
refused to sell 0/6 as evidence — expected hits 0.16, P(0 | no association) = 0.845 — and ran
a 200k-resample permutation test instead:

| metric | population mean | findings mean | p |
|---|---|---|---|
| composite `score` | 1.066 | 1.150 | 0.210 — **not significant** |
| `cyclomatic` | 4.20 | 8.00 | 0.044 |
| `line_count` | 16.73 | 43.17 | **0.009** |

**The score's raw inputs discriminate; the composite built from them does not.** Cause:
89.8% of the slice clears no threshold (median function ~11 lines against a 50-line cutoff),
collapsing 403 functions into 15 buckets.

Two of the six findings are invisible **in principle**: CPY-0107 spans two functions joined
by a function pointer (no per-function metric can contain it), and CPY-0019's defect is a
*missing* recursion guard (complexity has no term for an absence).

`#if` contributes zero to cyclomatic complexity, so the dual-build branching the campaign
most wants to find is precisely what the metric cannot see: **30 functions with internal
`#ifdef Py_GIL_DISABLED` branching all score 1.0 or 1.1.** An ifdef-density ranking hit
**2/14** against recorded findings versus **0/8** for the complexity hotspots.

**The trap worth remembering:** three of five lock-across-Python sites *are* top-4 by score,
but the discriminator is the **lock kind**, which complexity does not model. The metric
ranked highly the two sites where the pattern is by design (`Py_BEGIN_CRITICAL_SECTION`, which
is detach-capable) and floored the one that is a reproduced hang (`LOCK_KEYS`, which is
`_Py_LOCK_DONT_DETACH`).

---

## D-25 — the init-bypass AGENT PROMPT states a rule that is factually inverted for static types

Not a scanner defect — a defect in `agents/init-bypass-checker.md`, and in the brief I wrote
for this slice, which repeated it. Worth more than a scanner tweak because it corrupts triage
rather than counts.

The rule as written: "`tp_new` must be `0` (inherited) or `PyType_GenericNew` … then
`T.__new__(T)` yields a zeroed struct". For a **static** type whose `tp_base` is `object`,
that is backwards. `Objects/typeobject.c:9428-9437` (`type_ready_set_new`) adds
`Py_TPFLAGS_DISALLOW_INSTANTIATION` **implicitly** when `tp_new == NULL` and
`tp_base == object`. So `tp_new = 0` means the type CANNOT be instantiated — the opposite of
the prompt's premise.

The token `DISALLOW_INSTANTIATION` appears **nowhere** in either slice file, yet all 10
view/iterator types carry the flag. Verified from Python:
`type(iter({})).__flags__ & (1 << 7)` → `True`.

This also corrects the slice brief I wrote: I claimed dict/set iterators are "the same
construction shape" as `bytearray.__new__(bytearray).append(1)` and
`super.__new__(super).__get__(1)`. They are not. Those two crash precisely because they
spell `PyType_GenericNew` — an explicit, non-NULL slot — which suppresses the auto-flag.
dict/set's iterators spell nothing, so they get the flag and are safe.

Hand-applying the prompt's rule to this slice promotes **10 of 14 types** to CONSIDER, all
wrong. Scanner-side cost measured honestly: 43 `new_bypass` findings across
`Objects/` + `Modules/`, **zero** of which are this shape (all are `PyType_GenericNew` or
heap spec types) — so the defect bites triage, not the finding count.

**Fix:** an FP-taxonomy entry (same failure mode as the catalogued `Py_TRASHCAN_BEGIN`
entry), a scanner suppression, and a `tp_base` model, which `scan_init_bypass` currently
lacks entirely.

### D-25b — the structural zero was proven, not assumed

Recording the method because it is the standard the campaign wants. The agent did not assert
the zero; it built a counterfactual (`repro/scanner_zero_is_structural.py`) and showed the
zero survives two independent causes:

1. `_positional_bypassable_inits()` returns `[]` because the tp_init/tp_new **pairing filter**
   rejects `dict_init` (paired with `dict_new`) and `set_init` (paired with `set_new`), so
   `init_names` is empty and **D-8's fragile `_impl` name lookup never executes** — D-8 is
   definitively not the cause here.
2. Patching `tp_new` → `0` in both type tables makes the filter pass; D-8's heuristic then
   runs and *succeeds* (both are literal names, no Clinic rename) — and still yields 0,
   because `dict_init` assigns no struct field at all and `set_init` assigns only the scalar
   `self->hash`.

A 3-case recall canary through the real scanner: correctly reports the `bytearray`/`super`
shape, correctly silent on the dict/set shape, and **falsely reports** a static type with
`tp_new = 0` / `tp_base = 0` — which is D-25 itself, reproduced from the scanner side.

Dynamic confirmation of the negative: 512 probes × 4 builds = **2,048 runs, 0 crashes**
(4 construction routes × 14 types × every callable and 12 operators, each forked), plus a
15-route dict/frozendict layout-confusion probe, 30/30 clean.

---

## D-26 — dead guards read as guarded twins and will generate false sibling findings

`dictobject.c` has nine `dv->dv_dict == NULL` guards against three unguarded reads
(`dictview_mapping:6489`, `dictviews_to_set:6657`, `dictitems_xor:6878`). That asymmetry is
exactly the shape this campaign hunts — and here it is **not a bug**.

Lesson 3 applied properly: `git log --all -S "dv_dict = NULL"` returns **nothing**. The field
has never been NULL in 19 years. The guards date to 2007 and defend an abandoned design whose
TODO is still sitting at `dictobject.c:6499`.

The hazard is that a future informed run will read the nine guards as the guarded twin and
report the three unguarded reads as siblings needing the fix. Belongs in
`data/cpython_non_bugs.md` as a named FP class: *a guard with no corresponding store is dead
code, not a twin — check that something can actually produce the guarded value.*

---

## D-27 — `recursion_prone_slot_functions` counts the wrong thing, and it is a DENOMINATOR field

**Severity: high — this is the campaign's own anti-unearned-zero mechanism reporting a lie.**

`scan_recursion_guards` increments `recursion_prone_slot_functions` on `slot is not None`
rather than on membership in `_RECURSION_PRONE_CALLER_SLOTS`. On this slice the envelope
says **17**; the truth is **6**, and 4 after removing two suffix-fallback misattributions.

An agent following lesson 2 — "quote the denominator before calling a zero clean" — and
quoting that field overstates its coverage by **2.8×**. The one field that exists to prevent
unearned zeros is itself unearned. Fix before any further slice quotes it.

### D-28 — positional `PyTypeObject` slot registration is unparsed

`setobject.c` has **119 positional versus 4 designated** slot registrations, and the
scanner's slot map for it has size **zero**. `frozenset_hash` is matched only by a
name-suffix convention, not by structure. This is the informed briefing's own standing trap
#1, still unimplemented. Any rule keyed on "which slot is this function registered as" is
blind on positionally-initialised type tables.

### D-29 — two smaller recursion-scanner gaps, both currently zero-yield

- `Py_ReprEnter` sits in `_GUARD_TOKENS` and suppresses the entire enclosing function.
  Measured impact today: **0** (19 functions carry it tree-wide; none also performs an
  unguarded hash dispatch). A latent model defect, not an alarm.
- Direct slot spellings (`Py_TYPE(x)->tp_hash(x)`) are outside the vocabulary. Tree-wide
  denominator: 5 sites, all currently bounded. Real hole, no current yield.

Recorded because both are the kind of gap that produces a confident zero later.

### D-30 — PR #29's graph-field rule: structural zero here, verdict deferred

Its denominator on this slice is **2** genuine reads (`dictobject.c:7279-7280`, a flat
`__static_attributes__` lookup); the other 6 textual hits are positional slot-table
*comments*, not reads.

It produced **no false positives** on its first exposure to non-`typeobject.c` code, which is
the right outcome — but this slice supplies no evidence *for* it either. **Do not retire it
on this evidence; re-evaluate on a type/class-machinery slice.** Recorded so the next slice
does not read "structural zero" as "does not work".

### D-31 — a reproduction harness reported `ok` at a depth that actually crashes

Recorded as a method warning, not a scanner defect. The recursion agent's first probe of
CPY-0019 at depth 200,000 reported `ok`; the real crash threshold on a 16 MB stack is
**between 200,000 and 300,000**, not the 1,000,000 previously assumed. A single-depth probe
that returns clean is not evidence of a bound — sweep the depth or state the depth tested.

---

## D-24 — `ASSERT_DICT_LOCKED` is not proof that a lock is held

Not a scanner bug — a **reasoning** trap that a scanner or an agent can fall into, recorded
so it enters the FP taxonomy.

`_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` is skipped entirely when `Py_REFCNT(op) == 1`,
and compiles to nothing without `Py_DEBUG` or under the GIL. Every "build a fresh result
container, then insert into it" path satisfies it while holding no lock at all. Any argument
of the form "this must be locked, there is an assert" is invalid.

Belongs in `data/cpython_non_bugs.md`.
