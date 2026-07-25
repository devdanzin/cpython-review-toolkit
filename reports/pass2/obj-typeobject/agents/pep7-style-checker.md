# PEP 7 style checker — `Objects/typeobject.c`, pass 2

**Target:** `/home/danzin/projects/cpython/Objects/typeobject.c` @ `4f3be1b5777` (13,068 lines)
**Scanner:** `plugins/cpython-review-toolkit/scripts/check_pep7.py`
**Interpreter:** `/home/danzin/venvs/cpython-review-toolkit/bin/python` (3.12.13)
**Mode:** whole-tree for the always-on rules; diff-gated rules **force-enabled** via
`--enable-rule missing-braces,line-too-long --line-limit 79` so the goto-fail sweep could run.
**Pass-1 overlap:** none. No pass-1 agent covered style; nothing re-litigated here.

```
check_pep7.py Objects/typeobject.c                                  ->   1 finding
check_pep7.py Objects/typeobject.c --enable-rule missing-braces,\
              line-too-long --line-limit 79                         -> 305 findings
```

---

## 2. Bug-adjacent deviations — the part worth a maintainer's time

**Headline: zero.** Every one of the five bug-shapes I was asked to hunt came back
empty after reading the code. This is a genuine clean negative with a real
denominator behind each line, not silence:

| bug shape | candidates raised | confirmed | verdict |
|---|---|---|---|
| goto-fail (braceless body + next line indented as if inside) | 4 | **0** | ACCEPTABLE |
| indentation misrepresenting nesting | 1 | **0** | ACCEPTABLE |
| macro invocation as braceless body, not `do{}while(0)`-wrapped | 1 | **0** | ACCEPTABLE |
| assignment-in-condition without extra parens | 5 | **0** | ACCEPTABLE |
| dangling `else` ambiguity | 3 | **0** | ACCEPTABLE |

### 2.1 goto-fail shape — 0 of 149 braceless control statements

The dangerous shape is

```c
if (cond)
    stmt1;
    stmt2;      /* same indent, NOT in the block */
```

I walked all 149 genuine braceless `if`/`for`/`while` bodies, tracked paren balance
to find the true end of each condition and the true end of each body statement, then
compared the indent of the *following* code line to the body's indent. Four sites
tripped the heuristic; all four are false positives of my own detector, disproven by
reading:

- **L3401** (`mro_implementation`, MRO C3 merge) — body is `goto skip;` on L3402;
  L3403 is the `}` closing the inner `for`. My body-end tracker overran the brace.
- **L3695** (`mro_internal`) — a clean `if`/`else` pair; L3700 `return 1;` follows a
  blank line at function level.
- **L5449** — *not braceless at all.* The `{` is on L5451 (see §5, scanner defect).
- **L12118** (`update_slots`) — body is `return 0;` on L12119; L12121 is a fresh
  declaration after a blank line. No ambiguity.

The file's actual house form is disciplined: a braceless body is essentially always a
one-line `return NULL;` / `goto err;` / `Py_DECREF(x);` immediately under the
condition, and the next statement is separated by a blank line or dedented. In the
pass-2 regions specifically, the loops are the constructs most worth checking, and
both are clean:

```c
7677:    while (compatible_with_tp_base(newbase))
7678:        newbase = newbase->tp_base;
7679:    while (compatible_with_tp_base(oldbase))
7680:        oldbase = oldbase->tp_base;
```

Two adjacent braceless `while` loops in `compatible_for_assignment` — the
`__class__`-assignment safety check. Visually dense, but each body is a single
assignment and the second `while` is unambiguously a new statement. No hazard.
Same for `for (i = 0; i < to_merge_size; i++) remain[i] = 0;` at L3376–3377 in the C3
merge.

### 2.2 Indentation vs nesting — 0

Computed brace depth line-by-line (strings and char literals masked, block comments
masked) and compared to actual leading whitespace across the whole file. Exactly one
line came back under-indented relative to its depth — **L3331**, which is a
continuation line of a multi-line string literal in the `TypeError` message for the
MRO conflict, deliberately outdented to keep the message readable. Not a nesting
misrepresentation. Everything else in 13,068 lines matches its brace depth.

### 2.3 Macro-as-statement — 0

One braceless body is a macro invocation:

```c
2709:        if (dictptr && *dictptr)
2710:            Py_CLEAR(*dictptr);
```

`Py_CLEAR` is `do { ... } while (0)`-wrapped in both arms of its definition
(`Include/refcount.h:483` and `:493`), so the single-statement body is genuinely one
statement. Safe. No other braceless body is a macro call. **Nothing to hand to the
macro-hygiene lane.**

### 2.4 Assignment-in-condition — 5 sites, all correctly parenthesised

Every one uses the conventional extra-paren form that signals intent:

| line | code |
|---|---|
| 2616 | `while ((basetraverse = base->tp_traverse) == subtype_traverse) {` |
| 2689 | `while ((baseclear = base->tp_clear) == subtype_clear) {` |
| 2750 | `while ((basedealloc = base->tp_dealloc) == subtype_dealloc) {` |
| 2838 | `while ((basedealloc = base->tp_dealloc) == subtype_dealloc) {` |
| 2957 | `if ((f = Py_TYPE(res)->tp_descr_get) != NULL) {` |

No bare `if (x = y)`, and no site where `=` was plausibly meant to be `==` — all five
compare the assigned value immediately, so a typo'd `==` would not even compile into
the same shape.

### 2.5 Dangling `else` — 0

Three candidates, all disproven: L7489 (`else if` binds to the only preceding `if`),
L8195 (the `else` at L8202 belongs to the outer `if (!PyDict_Check(obj))` at L8199),
L10076 (the `else` at L10082 belongs to `if (res == -1 && PyErr_Occurred())` at
L10080). The nested-`if` block at L7545–7552 in `object_richcompare` is braceless
three levels deep but each `else` binds to its adjacent `if` and the indentation
states the binding correctly.

---

## 1. Aggregate counts

| rule | count | severity | PEP 7 basis |
|---|---|---|---|
| `keyword-space` | **1** | FIX | "one space between keywords like `if`, `for` and the following left paren" |
| `tab-indent` | 0 | FIX | "Use 4-space indents and no tabs at all." |
| `trailing-whitespace` | 0 | FIX | "No line should end in whitespace." |
| `header-guard` | 0 (n/a) | CONSIDER | not a PEP 7 rule; `.c` file, rule does not apply |
| `missing-braces` | 153 raw / **149 genuine** | ACCEPTABLE | diff-gated; PEP 7 says *do not* add them to untouched code |
| `line-too-long` | 151 (41 in pass-2 regions) | POLICY | soft rule; CPython sets no `max_line_length` |
| brace style / operator spacing | 0 | — | no deviation found |

Zero tabs and zero trailing whitespace is expected — CPython's `.editorconfig` sets
`trim_trailing_whitespace = true` and `indent_style = space` for `*.c`, so these are
mechanically enforced and the zeros confirm the toolchain works rather than telling
us anything about this file.

**Line length distribution** (raw count 162 over 79 cols; the checker reports 151
because 11 sit inside block comments and are correctly exempted):

| cols | lines |
|---|---|
| 80–89 | 122 |
| 90–99 | 23 |
| 100–109 | 14 |
| 110–119 | 3 (max 118) |

**POLICY, not FIX.** CPython's `.editorconfig` deliberately carries no
`max_line_length` for C, and PEP 7's own introduction says "rules are there to be
broken… to be consistent with surrounding code that also breaks it." 162 over-length
lines in a 13,068-line file is 1.2%, and the long tail is dominated by wide
`PyErr_Format` strings and slot-table initialisers where wrapping hurts readability.
Nothing here should be reformatted as part of this review.

### Missing-braces enumeration

149 genuine sites. **All ACCEPTABLE** under PEP 7's explicit carve-out: *"All new C
code requires braces… but do not add them to code you are not otherwise modifying."*
This is the file's house style for one-line early-exit bodies and reformatting it
would produce a 149-hunk diff with zero behavioural change and real review cost.
Listed for completeness, and because a *future* edit landing inside one of these is
the moment braces should be added.

**In the pass-2 regions (25):**

| region | lines |
|---|---|
| MRO-C3 (3217–3702) | 3252, 3376, 3401, 3405, 3436, 3610, 3695 |
| getattro/setattro (6529–6848) | 6586, 6773 |
| `__class__` assign (7482–7846) | 7489, 7499, 7515, 7545, 7548, 7596, 7598, 7609, 7677, 7679 |
| pickle (7848–8406) | 7900, 7909, 8195, 8236, 8338, 8342 |
| watchers (971–1481), lookup cache (6140–6452), managed static (228–522) | *none* |

Worth noting: the lookup cache, the watcher/version-tag machinery, and the managed
static-type region — three of the pass-2 regions, and among the most concurrency-
sensitive code in the file — contain **zero** braceless control statements. That code
is uniformly braced, which is the correct signal for recently-hardened regions.

**Outside the pass-2 regions (124):**

```
  826   833   837   840   858   906   951  1553  1563  1587  1644  1727
 1746  1918  2081  2474  2479  2598  2619  2654  2690  2709  2713  2738
 2839  2895  3728  3974  4079  7093  7147  7168  7431  7435  8517  8526
 8703  8749  8751  8756  8851  8892  8901  8915  8924  9696  9819  9862
 9865  9876  9879  9890  9902  9914  9967  9979  9983  9994 10020 10037
10040 10043 10056 10060 10063 10076 10080 10093 10096 10108 10112 10190
10192 10195 10207 10210 10213 10224 10227 10245 10258 10285 10288 10300
10302 10304 10321 10324 10336 10340 10407 10455 10626 10630 10733 10753
10775 10935 10980 11056 11142 11147 11149 11173 11183 11379 11383 11826
11959 11972 12114 12118 12465 12468 12478 12487 12492 12572 12594 12608
12790 12798 12903 12905
```

The dense run in 9862–10340 is the `wrap_*` slot-wrapper family — near-identical
6-line functions where `if (!check_num_args(args, 1)) return NULL;` is the idiom
repeated dozens of times. Uniform, self-consistent, and the least interesting code in
the file.

---

## 4. Deviation from the file's OWN convention — the one real finding

This is the highest-signal item in the lane, and it is a single character.

### FIX — L6471: `if(` in a file that writes `if (` 1,347 times out of 1,348

```c
6464: int
6465: _PyType_Validate(PyTypeObject *ty, _py_validate_type validate, unsigned int *tp_version)
6466: {
6467:     int err;
6468:     BEGIN_TYPE_LOCK();
6469:     err = validate(ty);
6470:     if (!err) {
6471:         if(assign_version_tag(_PyInterpreterState_GET(), ty)) {
                ^^^ missing space
6472:             *tp_version = ty->tp_version_tag;
6473:         }
```

Measured house style across the whole file: **1,347 `if (`/`for (`/`while (`/`switch (`
with the space, exactly 1 without.** A 1-in-1348 deviation is the strongest
local-convention break in the file, and it behaves exactly as the pass-2 brief
predicts — `git blame` puts it at `78a530a57800`, Donghee Na, **2024-11-22**, i.e. one
of the newest lines in a file whose bulk is decades old.

**Classification: FIX** — PEP 7 states the rule unconditionally, it is a
one-character mechanical change, zero behavioural risk, and it is the only violation
of an always-on rule in the entire file. Not a bug; worth a one-line cleanup patch.

I read the enclosing function for substance since the style break flagged it as
recent code: `_PyType_Validate` takes `BEGIN_TYPE_LOCK()`, calls `validate(ty)`,
conditionally assigns the version tag, and releases with `END_TYPE_LOCK()` on the
single exit path. No early return, no leak of the type lock, `err` set on both
branches. **Structurally clean** — the style break did not lead anywhere.

### Sub-convention worth recording

The file consistently uses K&R (`) {` on the control line) — 1,137 sites — but has a
deliberate 60-site Allman sub-convention: when a condition spans multiple lines, the
opening brace goes on its own line so the condition and the body are visually
separable. Example at L5449–5451. This is *intentional and good*, it is what the
scanner mistook for missing braces (§5), and it should not be "corrected."

---

## 5. Toolkit feedback — `missing-braces` precision defect

**Precision on this file: 149/153 = 97.4%.** All four false positives share one shape,
and it is a fixable scanner defect rather than a judgement call.

`check_pep7.py:299-312`:

```python
for j in range(i + 1, min(i + 3, len(lines))):
    next_stripped = lines[j].strip()
    if not next_stripped:
        continue
    if next_stripped.startswith('{') or next_stripped.endswith('{'):
        break
    # Next line is a statement without braces.
    violations.append(...)
```

The lookahead assumes the condition ends on the matched line. When a condition spans
multiple lines **and** the brace is on its own line (the 60-site Allman
sub-convention above), the first continuation line is non-blank and neither starts nor
ends with `{`, so the rule fires immediately — even though the block *is* braced, one
or two lines further down.

False positives, all confirmed braced by reading:

| line | brace actually at | blame |
|---|---|---|
| 1676 | L1679 | Victor Stinner, 2024-03-14 |
| 5449 | L5451 | Petr Viktorin, 2026-05-05 |
| 7655 | L7657 | Serhiy Storchaka, 2025-09-15 |
| 7660 | L7662 | Serhiy Storchaka, 2025-09-15 |

**Proposed fix:** before looking for the brace, advance to the true end of the
condition by tracking paren balance from the control keyword (the same technique this
report used); only then apply the existing next-non-blank-line test. This also removes
the arbitrary 2-line lookahead cap, which would independently mis-fire on any
condition spanning 3+ lines.

Note the FP sites skew heavily recent (2024–2026) — multi-line conditions with Allman
braces are how *new* code in this file is written, so this defect's cost will grow.

Everything else in the checker behaved correctly. Line numbers were exact in all 305
findings I spot-checked against the source; the block-comment exemption on
`line-too-long` (11 lines) is correct; the `keyword-space` hit is a true positive.

---

## Summary

| classification | count | items |
|---|---|---|
| **FIX** | 1 | L6471 `if(` → `if (` — breaks house style 1347:1, added 2024-11-22 |
| **CONSIDER** | 0 | — |
| **POLICY** | 1 | 162 lines > 79 cols; CPython sets no `max_line_length` — recommend no action |
| **ACCEPTABLE** | 149 | braceless single-statement bodies; PEP 7 forbids touching untouched code |

**No bug-adjacent style deviation exists in this file.** The goto-fail sweep, the
nesting-vs-indentation sweep, the macro-as-statement check, the assignment-in-
condition check and the dangling-`else` check all came back empty against real
denominators (149 braceless bodies, 13,068 depth-checked lines, 5 assignment-in-
condition sites). For a 13,068-line file this is the expected and correct result, and
inflating it would waste the maintainer's attention.

The one actionable item is a single missing space, and its real value is not the fix
but the signal: it is the newest line in a decades-old file, found by measuring the
file against itself rather than against PEP 7 in the abstract.
