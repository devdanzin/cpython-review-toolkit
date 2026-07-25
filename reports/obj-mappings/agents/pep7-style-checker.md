# PEP 7 style check — `obj-mappings` slice

**Agent:** pep7-style-checker
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (verified `git rev-parse`)
**Scope:** exactly `Objects/dictobject.c` (8,597 lines) + `Objects/setobject.c` (3,228 lines)
**Interpreter:** `~/venvs/cpython-review-toolkit/bin/python`
**Scanner change committed:** none.

---

## 1. Denominator line

**11,825 lines checked** (dict 8,597 + set 3,228), 2 files, 0 skipped.

Two runs were performed, because the two rule tiers answer different questions:

| run | invocation | `diff_scope` | active rules |
|---|---|---|---|
| A (whole-tree tier) | `check_pep7.py <file>` | `whole-tree` | tab-indent, trailing-whitespace, keyword-space, header-guard |
| B (diff-gated tier forced on) | `check_pep7.py Objects --changed-files Objects/dictobject.c,Objects/setobject.c --line-limit 79` | `2 changed file(s)` | + missing-braces, line-too-long — `skipped_rules: []` |

**I drove the diff-gated rules over the whole of both files, and I am saying so explicitly.**
Two independent ways, which agree exactly:

1. `--changed-files` sets `diff_scope = {f: set()}`; `analyze()` then does
   `changed_lines = diff_scope[rel] or None`, and the empty set falls to `None`, so
   `in_diff_scope()` returns `True` for every line. `--changed-files` is therefore already
   whole-file scope, not touched-line scope.
2. I also called `check_file(source, rules=ALL_RULES, line_limit=79,
   changed_lines=set(range(1, n+1)))` directly, as instructed. Identical counts (185 / 154).

Without this, on an unchanged file both diff-gated rules report **zero** — a structural zero,
not a clean result.

### Per-rule population (the real denominators)

| rule | candidate population | fired | rate |
|---|---|---|---|
| `keyword-space` | 1,133 `if`/`for`/`while`/`switch` + paren sites (dict 806, set 327) | 3 | 0.26 % |
| `missing-braces` | 186 lines matching `^\s+(?:if\|for\|while)\s*\(.*\)\s*$` (dict 88, set 98) | 185 | 1 correctly suppressed |
| `missing-braces` (ground truth) | **196** brace-less control bodies via tree-sitter-c (dict 95, set 101) | 185 | recall 94.4 % |
| `line-too-long` (79) | 158 raw lines > 79 cols (dict 142, set 16) | 154 | 4 masked as inside block comments — correct |
| `tab-indent` | 11,825 lines | 0 | structural: `.editorconfig` sets `indent_style = space` |
| `trailing-whitespace` | 11,825 lines | 0 | structural: `.editorconfig` sets `trim_trailing_whitespace = true` |
| `header-guard` | **0** | 0 | **structurally zero** — both files are `.c`; the rule returns early on any non-`.h` path. Not evidence of anything. |

---

## 2. The style pass

### Summary

| Rule | dict | set | total | Severity | PEP 7 basis |
|---|---|---|---|---|---|
| `keyword-space` | 2 | 1 | **3** | FIX | "one space between keywords like `if`, `for` and the following left paren" |
| `tab-indent` | 0 | 0 | 0 | FIX | "Use 4-space indents and no tabs at all." |
| `trailing-whitespace` | 0 | 0 | 0 | FIX | "No line should end in whitespace." |
| `header-guard` | n/a | n/a | 0 | CONSIDER | not a PEP 7 rule; and structurally inapplicable to `.c` |
| `missing-braces` | 87 | 98 | **185** | CONSIDER | braces required, "but do not add them to code you are not otherwise modifying" |
| `line-too-long` | 139 | 15 | **154** | POLICY | soft rule; see below |

### FIX — `keyword-space`, 3 sites, all genuine

| site | text |
|---|---|
| `Objects/dictobject.c:6555` | `switch(op) {` |
| `Objects/dictobject.c:7751` | `if((tp->tp_flags & Py_TPFLAGS_MANAGED_DICT) == 0) {` in `PyObject_VisitManagedDict` |
| `Objects/setobject.c:2496` | `if(!PyAnySet_Check(w))` in the richcompare slot |

Three one-character insertions, zero behavioural risk. `dictobject.c:6555` sits eight lines
below a correctly-spaced `switch (op)`-style block, and `setobject.c:2496` is nine lines above
`switch (op) {` **in the same function** — so both are local inconsistencies, not a file
convention. `setobject.c:2496` is also a `missing-braces` hit (its body is a bare
`Py_RETURN_NOTIMPLEMENTED;`), the only line in the slice flagged by two rules.

### POLICY — `line-too-long`

154 lines exceed 79 columns (max 145 in dict, 109 in set). CPython's `.editorconfig` at this
ref sets `trim_trailing_whitespace`, `insert_final_newline`, `indent_style` and `indent_size`
but **deliberately no `max_line_length`** (verified: `grep -c max_line_length` → 0). PEP 7's own
introduction says "rules are there to be broken… to be consistent with surrounding code that
also breaks it." I am not presenting 79 columns as settled policy for this slice. The
distribution is also lopsided — dict has 139 over-length lines to set's 15, so this is a
dictobject.c-specific habit, not a slice-wide one.

The 4-line gap between the raw count (158) and the reported count (154) is the block-comment
mask working correctly; those 4 are prose inside `/* … */`.

---

## 3. Calibration of the `missing-braces` fix — the main deliverable

Last session the rule's fixed 2-line lookahead was replaced with a paren-balance walk to the
true end of the condition, plus a check of the text after the closing paren. It was measured on
`typeobject.c` (153 → 149, exactly the 4 known false positives). **This slice is its first test
on different code.**

Ground truth was built independently with **tree-sitter-c** (available in the venv): every
`if_statement` / `for_statement` / `while_statement` / `do_statement` / `else_clause` whose body
node is not a `compound_statement`. This is a genuinely independent oracle — `check_pep7.py`
imports only the standard library and is purely lexical, so an AST parser shares no failure mode
with it. Analysis only; the scanner was not modified.

### Headline

| metric | dict | set | total |
|---|---|---|---|
| scanner `missing-braces` hits | 87 | 98 | **185** |
| tree-sitter brace-less control bodies | 95 | 101 | **196** |
| **false positives** | **0** | **0** | **0 / 185 (precision 100 %)** |
| false negatives | 8 | 3 | **11 / 196 (recall 94.4 %)** |

**All 185 hits are true positives.** Every one lands exactly on a line where tree-sitter agrees
a control statement has a non-compound body. I spot-read the risk-tagged 43 of them by hand
(section 4) and found no disagreement.

An orthogonal check confirms the oracle rather than rubber-stamping it: tree-sitter reports
**zero** same-line brace-less bodies (`if (x) return y;`) in either file, and
`grep -cE '^\s+(if|while|for)\s*\(.*\)\s*[A-Za-z_].*;'` independently returns 0 for both. So
every brace-less body in this slice is the multi-line kind — which is exactly the dangerous kind.

### 3a. Did the fix actually engage here? — mostly no, and that matters

This is the part that would have been missed by reporting "0 FP" alone. The fix's machinery only
matters when the regex matches a line whose condition does **not** end there (`depth > 0`).
Instrumenting that branch:

| | dict | set |
|---|---|---|
| regex matches with a multi-line condition (paren-walk engaged) | **1** | **2** |
| suppressed by the post-closing-paren tail check (`tail.startswith("{")`) | **0** | **0** |
| new-rule hits | 87 | 98 |
| old-rule (2-line lookahead) hits | 88 | 98 |

**The paren-balance walk engaged 3 times in 11,825 lines, and was decisive exactly once.**

- **`dictobject.c:5221` — a true false positive, eliminated.** Same shape as the four found on
  `typeobject.c`:
  ```c
  if (PyAnyDict_CheckExact(other)
      && GET_USED((PyDictObject *)other) == 0)
  {
      return Py_NewRef(self);
  }
  ```
  The old 2-line lookahead saw line 5222 (`&& GET_USED(...)`), which neither starts nor ends
  with `{`, and reported a brace-less body on correctly-braced code. The walk advances the scan
  start past the real closing paren, so the `{` on 5223 is found. **The fix generalizes: it
  removed a real FP on code it was not tuned against.**
- **`setobject.c:149` and `setobject.c:282` — true positives, preserved.** Both are three-line
  `PyUnicode_CheckExact(...) && ... && unicode_eq(...)` conditions with a genuinely brace-less
  body. The old rule also flagged these, but *for the wrong reason* — it mistook the condition's
  continuation line for the body. The new rule reaches the same verdict by correct reasoning.
  Net count unchanged, correctness improved.

Two consequences worth recording:

1. **The 100 % precision result is only ~1/185 attributable to the fix.** 184 of the 185 hits go
   through the single-line-condition path, which the fix did not touch. Reporting "0 FP, the fix
   works" without this decomposition would overstate the evidence considerably. The honest claim
   is narrower and still positive: *the fix was exercised 3 times on unfamiliar code, was
   decisive once, and was correct all 3 times — no regression, one genuine FP removed.*
2. **The post-closing-paren tail check is still untested.** `tail.startswith("{")` fired 0/3 in
   this slice. It handles `…)  {` where the brace trails the closing paren on the same
   continuation line; this slice's Allman sub-convention always puts the brace on its own line,
   so that branch has now been carried through two slices without ever executing. It is not
   *wrong*; it is *unverified*. Do not count it as validated.

### 3b. Surviving false positives

**None.** 0 of 185.

### 3c. False negatives — 11, and the anchor is *not* the main cause

The brief predicted the `\)\s*$` anchor would be the recall limit. It bites, but it is the
minority cause. Breaking the 11 down:

| # | class | sites | is it the `\)\s*$` anchor? |
|---|---|---|---|
| **8** | **bare `else`** | dict 3844, 4067, 4686, 4750, 6939; set 802, 1394, 1930 | **No** — keyword-set gap |
| 1 | `else if (…)` | dict 3819 | **No** — keyword-set gap |
| 1 | trailing comment after `)` | dict 4717 | **Yes** |
| 1 | multi-line `for` header ending `;` | dict 607 | **Yes** |

**The dominant recall gap is `else`, not the anchor — 9 of 11 (82 %).** The regex is
`^\s+(?:if|for|while)\s*\(.*\)\s*$`: it has no `else` alternative, and in `else if (…)` the
`else` sits between `^\s+` and `if`, so that fails too. Examples:

```c
/* dictobject.c:3843-3848 — dict_ass_sub */
    if (w == NULL)
        return PyDict_DelItem(mp, v);
    else                                  /* <-- not flagged */
        return PyDict_SetItem(mp, v, w);
```
```c
/* dictobject.c:3818-3820 */
            else if (PyErr_Occurred())    /* <-- not flagged */
                return NULL;
```

The `if` half of each pair *is* flagged, so the site is not invisible to a reviewer — but the
count is understated and the `else` arm, which is where the divergent branch lives, is the one
silently dropped.

**Quantifying the anchor limit specifically: 2 of 11 (18 %).**

- `dictobject.c:4717` — `if (cmp <= 0)  /* error or not equal */` followed by `return cmp;`.
  The rule already computes a comment-stripped `clean` for `keyword-space`, but matches
  `_CONTROL_NO_BRACE` against **`raw_line`**, so the trailing comment defeats `\)\s*$`. This is a
  one-token fix (`_CONTROL_NO_BRACE.match(clean)`) with no other behavioural effect — `clean`
  preserves column positions for `/* … */` (replaced by a space) and truncates at `//`.
- `dictobject.c:607` — a three-line `for` header whose first line ends in `;`, body `;`.
  Low value: bracing an intentional empty loop body is noise. I would not chase this.

Both are genuine anchor misses, so the brief's hypothesis is confirmed — just smaller than the
`else` gap.

---

## 4. Escalation: brace-less conditionals adjacent to locks, refcounts, and `goto`

Per the task framing: in this code an added line under an unbraced `if` silently falls outside
the conditional. I tagged each of the 185 true positives by what appears in its body and in the
first executable line *after* the body. **43 of 185 (23 %) are risk-adjacent** — 34 with a
`goto` body, 6 followed by a refcount op, 4 followed by a lock/atomic op, 2 followed by
`PyErr_Clear()`.

**Classification for everything in this section: CONSIDER, not FIX.** I read each site and found
**no present-tense bug** — the code is correct as written. The escalation is about edit hazard
in load-bearing re-entrancy code, and I am stating that plainly rather than dressing a style nit
as a defect.

### 4.1 `_Py_dict_lookup_threadsafe` — `dictobject.c:1627/1631/1635/1666` (strongest)

The lock-free free-threaded read path. Within this one function the *same predicate with the
same body* appears both braced and unbraced:

```c
1635:                if (value == NULL)
1636:                    goto read_failed;          /* UNBRACED */
1637:
1638:                if (values != _Py_atomic_load_ptr(&mp->ma_values)) {
1639:                    Py_DECREF(value);
1640:                    goto read_failed;
1641:                }
...
1645:                if (value == NULL) {
1646:                    goto read_failed;           /* BRACED — same predicate, same body */
1647:                }
...
1666:            if (value == NULL)
1667:                goto read_failed;               /* UNBRACED again */
```

Line 1645 is the braced sibling of 1635 and 1666, ten and thirty-one lines away respectively.
Why this is more than style here: the established idiom for a bail-out in this very function,
three lines below the unbraced form, is **`Py_DECREF(value); goto read_failed;`** (1638-1641,
and again 1649-1652). So "add a `Py_DECREF` before the `goto`" is not a hypothetical future
edit — it is the surrounding code's own pattern. Made under the unbraced `if` at 1635, it
becomes an unconditional `Py_DECREF` of a reference that is live and owned on the not-taken
path, on the hot lock-free read path, in a free-threaded build. That is a refcount underflow /
use-after-free, and it would be invisible in review because the diff would look identical to the
correct braced neighbour.

Following lines are `_Py_atomic_load_uint8_relaxed` (1630) and `_Py_atomic_load_ptr` (1638,
1649) — the atomic re-validation that makes the lock-free protocol sound. The unbraced guards
sit directly between the loads they gate.

Note also the internal inconsistency runs the other way at 1620-1622 and 1645-1647, which *are*
braced for identical one-statement `goto read_failed;` bodies. There is no rule being followed
here; it is drift.

### 4.2 `set_add_entry` — `setobject.c:294` (the re-entrancy staleness check)

```c
286:                table = so->table;
287:                Py_INCREF(startkey);
288:                cmp = PyObject_RichCompareBool(startkey, key, Py_EQ);   /* arbitrary Python */
289:                Py_DECREF(startkey);
290:                if (cmp > 0)
291:                    goto found_active;
292:                if (cmp < 0)
293:                    goto comparison_error;
294:                if (table != so->table || entry->key != startkey)
295:                    goto restart;                                        /* UNBRACED */
296:                mask = so->mask;
```

Line 294-295 is the single most load-bearing unbraced conditional in the slice: it is the
**entire** defense against a user `__eq__` mutating the set during the compare at 288 — the
documented restart loop the slice brief names. Its body is the `goto restart` that re-reads
`mask`, `i`, `freeslot`, `perturb`. Any second staleness action added under it unbraced (e.g.
resetting `freeslot`) would execute on *every* probe iteration instead of only on detected
mutation, silently defeating the linear-probe free-slot tracking.

Its braced sibling with the same `goto restart;` body is seventeen lines below at 311-313. Per
the brief's lesson 3, I am not claiming that sibling's braces *defend* against anything — they
do not; the two sites face the same threat model and the brace difference is pure drift. The
sibling is cited as a style twin only.

### 4.3 `set_compare_entry_lock_held` — `setobject.c:159` (the lock-held twin)

The same staleness check, same shape, also unbraced, in the FT lock-held twin of 4.2:

```c
155:        int cmp = PyObject_RichCompareBool(startkey, key, Py_EQ);
156:        Py_DECREF(startkey);
157:        if (cmp < 0)
158:            return SET_LOOKKEY_ERROR;
159:        if (table != so->table || entry->key != startkey)
160:            return SET_LOOKKEY_CHANGED;                                  /* UNBRACED */
```

Every conditional in this function's re-entrancy region (142, 147, 149, 157, 159, 161) is
unbraced. The shape is systematic across `setobject.c`: the re-entrancy staleness checks are
consistently the unbraced ones.

### 4.4 Lower-value risk-adjacent sites (summarised, not escalated)

`setobject.c:1867` (`return NULL;` immediately preceding `Py_BEGIN_CRITICAL_SECTION(so)`);
`setobject.c:2658` and `2698` (`return NULL;` immediately preceding `PyErr_Clear();`);
`dictobject.c:6476` and `setobject.c:1176` (`return NULL;` preceding a `Py_NewRef` iterator/view
init). These are ordinary allocation guards; I list them for completeness rather than concern.

---

## 5. Classes bounded (clean, with denominator)

| class | denominator | result |
|---|---|---|
| tabs in indentation | 11,825 lines | **0** — evidential and structural (`.editorconfig` `indent_style = space`) |
| trailing whitespace | 11,825 lines | **0** — evidential and structural (`.editorconfig` `trim_trailing_whitespace`) |
| `missing-braces` false positives | 185 hits vs 196 tree-sitter ground-truth bodies | **0** |
| header guards | **0 candidates** | **structural zero** — both files are `.c`; `check_header_guard` returns `[]` on any non-`.h` path. This certifies nothing. Flagged per the brief's denominator lesson. |
| same-line brace-less bodies (`if (x) return y;`) | 11,825 lines, two independent methods | **0** |

---

## 6. Toolkit feedback

### Precision per rule (this slice)

| rule | precision | note |
|---|---|---|
| `keyword-space` | **3/3 = 100 %** | consistent with the 64/64 measured tree-wide on `Objects/` |
| `missing-braces` | **185/185 = 100 %** | independently verified against tree-sitter-c |
| `line-too-long` | 154/154 mechanically correct | but POLICY, not a defect count |
| `tab-indent`, `trailing-whitespace` | n/a (0 hits) | |

### Recall gaps found by reading (highest-value output)

1. **`else` / `else if` are not in the `missing-braces` keyword set — 9 of 11 misses (82 %).**
   This is the dominant gap and it was *not* the one predicted. Concrete proposal: extend the
   anchor to
   ```python
   _CONTROL_NO_BRACE = re.compile(r"^\s+(?:\}\s*)?(?:else\s+if|else|if|for|while)\b(?:\s*\(.*\)\s*)?$")
   ```
   with the paren-balance walk skipped when there is no `(`. Caveat worth measuring before
   shipping: a bare `else` line is trivially easy to match, so this could raise tree-wide volume
   on a rule PEP 7 explicitly says not to apply retroactively — it should stay diff-gated, and it
   should be measured on `Objects/` before merge. I did **not** make this change.
2. **`missing-braces` matches `raw_line` where every other rule matches `clean`.** One site here
   (`dictobject.c:4717`) is lost purely to a trailing `/* … */`. Changing
   `_CONTROL_NO_BRACE.match(raw_line)` to `.match(clean)` is a one-token fix; `clean` is already
   computed on the same iteration and preserves the line's structure. Lowest-risk recall win
   available. I did **not** make this change.
3. **The post-closing-paren tail check has now gone two slices without executing** (0/3
   engagements here, and the typeobject.c measurement was attributable to the paren walk). It
   needs a unit test with `if (a &&\n b)  {` rather than more field slices, or it will keep
   being carried as unverified code.

### Calibration verdict on the fix

**No regression, one genuine FP removed, correct on all 3 engagements.** But the fix's code path
touched only 3 of 186 candidate sites in 11,825 lines, so this slice is a *weak* positive
signal, not a strong one. The multi-line-Allman-condition shape it targets is dense in
`typeobject.c` (~60 sites) and rare in the mappings files (3). If further validation is wanted,
pick a slice with dense multi-line conditions rather than a large one.

### Suggested guideline (POLICY, for the campaign not for CPython)

`Objects/dictobject.c` and `Objects/setobject.c` have 185 brace-less control bodies between
them. PEP 7 is explicit that these must not be retroactively braced. The actionable form is a
**diff-time** rule: any patch touching a brace-less conditional inside `_Py_dict_lookup*`,
`set_add_entry`, `set_lookkey`, or `set_compare_entry_lock_held` should add the braces as part
of that patch — those are the sites where the body is a `goto`/`return` guarding a lock, an
atomic re-validation, or a refcount transfer.

---

## 7. Noticed outside slice

- `Objects/` tree-wide `keyword-space` was previously measured at 64; the 3 found here are a
  subset of that population, not new. No action for this slice.
- The `missing-braces` `else` recall gap is toolkit-wide, not slice-specific — it will have
  understated every prior slice's count, including the `typeobject.c` 153/149 calibration.

---

## Appendix — reproduction

```bash
V=~/venvs/cpython-review-toolkit/bin/python
S=/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts

# Run A — whole-tree tier
$V $S/check_pep7.py /home/danzin/projects/cpython/Objects/dictobject.c
$V $S/check_pep7.py /home/danzin/projects/cpython/Objects/setobject.c

# Run B — diff-gated tier forced over the whole of both files
$V $S/check_pep7.py /home/danzin/projects/cpython/Objects \
     --changed-files Objects/dictobject.c,Objects/setobject.c --line-limit 79
```

Calibration harnesses (scratchpad, not committed): `calibrate.py` (tree-sitter ground truth,
FP/FN), `escalate.py` (hardened oracle + risk tagging), `exercised.py` (old-vs-new rule diff and
paren-walk engagement counter).
