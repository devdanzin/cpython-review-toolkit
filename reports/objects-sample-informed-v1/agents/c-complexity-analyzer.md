# c-complexity-analyzer — Objects/ sample (informed)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0, full clone)
**Script:** `measure_c_complexity.py`, run over the 14-file sample and over all of `Objects/`
**Role in this run:** correlation, not ranking. Nine agents finished before me; this report asks
whether the complexity signal would have pointed at any defect they confirmed.

**Verdict, in one line:** **complexity *ranking* predicts defects here (the top 10 of 505 functions
by score hold 5 of the 25 confirmed defect-bearing functions — p = 0.00004, 10x enrichment), but
the shipped hotspot *threshold* predicts nothing (`score >= 5.0` flags zero functions in the
sample), and 80% of defects sit at the score floor. Complexity may rank; it must never gate.**

---

## Scanner volume

```
functions measured (shipped tool, sample):   392
functions actually present (tree-sitter):    505    <-- 113 missed, 22.4%
hotspots (score >= 5.0) in sample:             0
hotspots (score >= 7.0) in sample:             0
hotspots in all of Objects/:                   3    (top one is a generated table in a .h)
confirmed defect-bearing functions in sample: 25    (from the 8 other agents' reports)
  ... at the score floor (1.0):               20    (80%)
  ... in the top 10 by score:                  5    (p = 0.00004)
  ... invisible to the shipped extractor:      5    (20%)
```

Precision/recall in the detector sense does not apply — this is a measurement tool. The
equivalent numbers are the **threshold sweep** (§1.4) and the **coverage gap** (§5.1).

**Ground truth.** I did not use only the 10 sites named in my brief. I cross-read all eight
completed agent reports and assembled the full in-sample inventory: **25 distinct defect-bearing
functions** (17 FIX, 8 CONSIDER) across 11 of the 14 files. All correlation figures below are on
that basis; using only the 10 briefed sites understates the correlation substantially, and I flag
where the two differ.

---

## 0. Measurement caveat — answered, and it is not the one I was warned about

The brief warned that the tree-sitter chassis silently drops functions in files with
brace-unbalanced macros (`dictobject.c` loses ~39% of its lines). **`measure_c_complexity.py` is
not affected by that defect.** It does not call `tree_sitter_utils.extract_functions`; it carries
its own line-oriented regex extractor (`find_functions`, `measure_c_complexity.py:98-161`) that
looks for `{` at column 0 and walks back one line for a signature.

Verified directly:

| file | source lines | tree-sitter fns | regex fns | tree-sitter line-cov | regex line-cov |
|---|---|---|---|---|---|
| `dictobject.c` | 8598 | 187 | **231** | **50.2%** | 59.6% |
| `typeobject.c` | 13069 | 417 | 362 | 82.9% | 66.3% |
| `genericaliasobject.c` | 1071 | 37 | 27 | 82.9% | 65.0% |

On `dictobject.c` tree-sitter's line coverage collapses to 50.2% against its ~83% norm — the
reported defect, reproduced — while the regex extractor finds *more* functions there. So the
run-level caveat does not contaminate my rankings.

**But the regex extractor has its own blind spot, and for this agent it is worse.** It requires
the whole parameter list on one line, because it matches `^(\w+)\s*\(([^)]*)\)\s*$` against
`lines[i-1]`. CPython wraps long parameter lists:

```c
static PyObject *
subs_tvars(PyObject *obj, PyObject *params,
           PyObject **argitems, Py_ssize_t nargs)     /* prev line is ", Py_ssize_t nargs)" */
{                                                      /* no match -> function dropped */
```

Measured: **113 of 505 functions (22.4%) are invisible to the shipped tool.** In all 14 sample
files the regex set is a strict subset of the tree-sitter set (zero regex-only names) — pure loss,
not disagreement.

The loss is **biased toward the functions the tool exists to find**, because a wrapped parameter
list is a proxy for having more parameters. Casualties include **5 of the 25 confirmed
defect-bearing functions**, two of which are top-10 by score:

- `subs_tvars` — confirmed FIX, **rank 9 of 505**
- `structseq_new_impl` — confirmed FIX, **rank 3 of 505**
- `descr_get_qualname` — confirmed FIX (TSAN-0043)
- `_odict_popkey_hash` — confirmed CONSIDER
- `_PyStructSequence_InitBuiltinWithFlags` — confirmed CONSIDER
- plus every other Argument Clinic `*_impl` (`func_new_impl` rank 6, `property_init_impl` rank 13,
  `OrderedDict_copy_impl` rank 15, 20 more in `odictobject.c`) and all 6 `method_vectorcall_*`.

Per-file coverage is worst on the Clinic-heavy files: `structseq.c` 63.6%, `odictobject.c` 66.7%,
`descrobject.c` 70.2%, `genericaliasobject.c` 73.0%, `funcobject.c` 74.4%.

**Are my rankings complete for the sample?** The ones below are. I re-extracted all 505 functions
with `tree_sitter_utils.extract_functions` and fed each body through the shipped
`measure_function()` — the tool's own metrics, complete coverage. Every table marks which rows
the shipped tool would actually have shown you.

---

## 1. Does complexity predict defects here? **Ranking: yes. Gating: no. Recall: no.**

### 1.1 The score distribution is degenerate on this sample

| statistic | sample (n = 505) | the 25 defect functions |
|---|---|---|
| median score | 1.0 | **1.0** |
| max score | 3.1 | 3.1 |
| at the floor (score == 1.0) | **476 (94.3%)** | **20 (80%)** |
| median cyclomatic | 2 | **5** |
| median body lines | 8 | **23** |
| median nesting | 1 | 2 |
| hotspots (>= 5.0) | **0** | 0 |

The scoring function (`measure_c_complexity.py:222-247`) awards nothing below 50 body lines,
nothing below nesting depth 4, and nothing below cyclomatic 11. The median `Objects/` function is
8 lines with cyclomatic 2. **94.3% of the sample cannot score above 1.0 by construction** — a
metric that assigns one value to 476 of 505 items cannot rank them.

Yet the defect functions are measurably bigger and branchier than the sample: **median cyclomatic
5 vs 2, median body 23 lines vs 8.** The signal exists; the score's thresholds are set far above
where it lives.

### 1.2 Where the confirmed defects sit

All 25, ranked by the tool's own score:

| rank /505 | function | site | sev | score | lines | nest | cyc | shipped tool sees it? |
|---|---|---|---|---|---|---|---|---|
| **1** | `_Py_subs_parameters` | genericaliasobject.c:405 | FIX | **3.1** | 149 | 4 | 31 | yes |
| **2** | `_Py_make_parameters` | genericaliasobject.c:185 | FIX | **1.9** | 75 | 5 | 21 | yes |
| **3** | `structseq_new_impl` | structseq.c:166 | FIX | **1.9** | 92 | 3 | 20 | **NO** |
| **8** | `structseq_repr` | structseq.c:273 | FIX | **1.6** | 50 | 3 | 13 | yes |
| **9** | `subs_tvars` | genericaliasobject.c:273 | FIX | **1.6** | 43 | 5 | 11 | **NO** |
| 32 | `unionbuilder_add_single_unchecked` | unionobject.c:168 | FIX | 1.0 | 35 | 3 | 10 | yes |
| 34 | `func_get_annotation_dict` | funcobject.c:534 | CONSIDER | 1.0 | 39 | 3 | 10 | yes |
| 61 | `_odict_popkey_hash` | odictobject.c:1096 | CONSIDER | 1.0 | 23 | 2 | 7 | **NO** |
| 68 | `_PyStructSequence_InitBuiltinWithFlags` | structseq.c:667 | CONSIDER | 1.0 | 43 | 2 | 7 | **NO** |
| 69 | `calliter_iternext` | iterobject.c:243 | FIX | 1.0 | 24 | 2 | 7 | yes |
| 81 | `odictiter_new` | odictobject.c:1945 | FIX | 1.0 | 24 | 2 | 6 | yes |
| 84 | `iter_iternext` | iterobject.c:80 | FIX | 1.0 | 26 | 1 | 6 | yes |
| 88 | `tuple_hash` | tupleobject.c:371 | FIX | 1.0 | 23 | 2 | 5 | yes |
| 92 | `ga_getitem` | genericaliasobject.c:583 | FIX | 1.0 | 19 | 2 | 5 | yes |
| 116 | `PyStructSequence_New` | structseq.c:77 | FIX | 1.0 | 17 | 1 | 5 | yes |
| 117 | `PyStructSequence_InitType2` | structseq.c:700 | CONSIDER | 1.0 | 23 | 1 | 5 | yes |
| 134 | `templateiter_next` | templateobject.c:19 | CONSIDER | 1.0 | 18 | 2 | 4 | yes |
| 135 | `template_iter` | templateobject.c:225 | FIX | 1.0 | 21 | 1 | 4 | yes |
| 173 | `ga_hash` | genericaliasobject.c:611 | FIX | 1.0 | 10 | 1 | 3 | yes |
| 176 | `ga_iternext` | genericaliasobject.c:952 | FIX | 1.0 | 13 | 1 | 3 | yes |
| 222 | `weakref_hash_lock_held` | weakrefobject.c:190 | FIX | 1.0 | 10 | 1 | 3 | yes |
| 228 | `get_type_attr_as_size` | structseq.c:41 | FIX | 1.0 | 11 | 2 | 3 | yes |
| 242 | `lazy_import_name` | lazyimportobject.c:87 | CONSIDER | 1.0 | 9 | 2 | 3 | yes |
| **257** | `descr_get_qualname` | descrobject.c:624 | **FIX** | 1.0 | **4** | **0** | **2** | **NO** |
| **395** | `mappingproxy_hash` | descrobject.c:1204 | CONSIDER | 1.0 | **2** | **0** | **1** | yes |

**Five of 25 are in the top 10.** Under a hypergeometric null (N=505, K=10, n=25) that is
**p = 0.00004** against an expectation of 0.50 — a 10x enrichment and unambiguously real.

**Twenty of 25 are at the score floor**, and the tail is decisive. `descr_get_qualname` is a
**4-line function, cyclomatic 2, nesting 0** — the simplest shape a C function can have — carrying
a confirmed FT lazy-init race (TSAN-0043) whose guarded twin landed 7 days before HEAD:

```c
static PyObject *
descr_get_qualname(PyObject *self, void *Py_UNUSED(ignored))
{
    PyDescrObject *descr = (PyDescrObject *)self;
    if (descr->d_qualname == NULL)
        descr->d_qualname = calculate_qualname(descr);   /* unsynchronised; also a raw store */
    return Py_XNewRef(descr->d_qualname);
}
```

`mappingproxy_hash` (rank 395, **2 lines, cyclomatic 1**) is a CONSIDER for unguarded hash
descent. No complexity metric of any design ranks either into a review queue.

### 1.3 The shape of the correlation

Splitting the 25 by severity is instructive: the 17 FIX findings have median cyclomatic 5 and the
8 CONSIDER findings have median cyclomatic 5 as well — severity does not track complexity. What
tracks complexity is **which bug class**:

- The five top-10 hits are all **multi-step transformation** functions — parameter substitution
  and struct-sequence construction, where the bug is a mis-ordered cleanup in one of many error
  ladders. Complexity is genuinely causal there.
- The floor-dwellers are all **single-contract omissions** — a missing `Py_EnterRecursiveCall`, a
  missing critical section, a missing `Py_INCREF`, a `Py_SETREF` where `Py_CLEAR` was needed.
  These are one-line defects in short functions; complexity cannot see them and there is no reason
  it should.

That split is the actionable result. It says the `hotspots` command should be scoped to bug
classes whose mechanism is *error-path combinatorics*, and should explicitly disclaim the
contract-omission classes that dominate this sample's findings.

### 1.4 Threshold sweep — recall against review cost (N = 505, D = 25)

| gate | flagged | % of functions | defects | recall | precision | enrichment |
|---|---|---|---|---|---|---|
| **`score >= 5.0` (shipped hotspot)** | **0** | **0.0%** | **0** | **0%** | — | **—** |
| `score >= 3.0` | 1 | 0.2% | 1 | 4% | 100% | 20.2x |
| `score >= 1.9` | 4 | 0.8% | 3 | 12% | 75% | 15.1x |
| **`score >= 1.6`** | **9** | **1.8%** | **5** | **20%** | **56%** | **11.2x** |
| `score >= 1.1` (any signal) | 29 | 5.7% | 5 | 20% | 17% | 3.5x |
| `cyclomatic >= 10` | 34 | 6.7% | 7 | 28% | 21% | 4.2x |
| `cyclomatic >= 6` | 85 | 16.8% | 12 | 48% | 14% | 2.9x |
| `nesting >= 4` | 5 | 1.0% | 3 | 12% | 60% | 12.1x |
| `nesting >= 3` | 32 | 6.3% | 7 | 28% | 22% | 4.4x |
| `body lines >= 40` | 25 | 5.0% | 6 | 24% | 24% | 4.8x |
| `body lines >= 20` | 92 | 18.2% | 15 | 60% | 16% | 3.3x |

Two clean conclusions:

1. **At the tight end the composite score is the best available discriminator.** `score >= 1.6`
   flags 9 functions (1.8% of the sample) and 5 of them are confirmed defect sites — **56%
   precision, 11.2x enrichment.** That is a genuinely useful review queue. The score is doing
   real work; it is just doing it 3.4 points below where the threshold sits.
2. **The recall ceiling is low and cannot be raised by lowering the threshold.** Going from
   `>= 1.6` to `>= 1.1` triples the flagged set and finds zero additional defects. Past ~20%
   recall the score is exhausted, and only crude size gates (`lines >= 20`, 60% recall at 18%
   cost) keep going. **Complexity gating would discard 4 of every 5 real bugs in this sample.**

### 1.5 File level — the signal is real and survives the size confound

| file | defects | mean cyc | body lines | defects / kLOC |
|---|---|---|---|---|
| `structseq.c` | **6** | **5.1** | 512 | 11.72 |
| `genericaliasobject.c` | **6** | **5.3** | 686 | 8.75 |
| `templateobject.c` | 2 | 3.2 | 224 | 8.93 |
| `descrobject.c` | 2 | 2.9 | 912 | 2.19 |
| `odictobject.c` | 2 | 3.9 | 842 | 2.38 |
| `iterobject.c` | 2 | 2.8 | 202 | 9.90 |
| `tupleobject.c` | 1 | 4.8 | 711 | 1.41 |
| `unionobject.c` | 1 | 4.1 | 352 | 2.84 |
| `funcobject.c` | 1 | 3.0 | 944 | 1.06 |
| `weakrefobject.c` | 1 | 3.0 | 472 | 2.12 |
| `lazyimportobject.c` | **1** | **2.5** | **67** | **14.93** |
| `capsule.c` / `interpolationobject.c` / `cellobject.c` | 0 | 2.5 | 346 | 0.00 |

Pearson r(mean cyclomatic, defect count) = **0.746**; r(total body lines, defect count) = **0.359**.
**The two highest-mean-complexity files are the two highest-defect files**, and unlike my first
pass on the 10-site ground truth, complexity now clearly beats raw size as a file-level predictor.

The honest counterexample remains: `lazyimportobject.c` has the highest defect density in the
sample (14.93/kLOC) and the **lowest** mean complexity (2.5), and `iterobject.c` is third in
density (9.90) and second-lowest in complexity (2.8). With 14 data points I would not build a
gate on r = 0.746, but as a *prioritisation* input for `hotspots` it is defensible.

### 1.6 Honest caveat on the ground truth

These 25 are what eight *static-review* agents confirmed, and those agents hunt recursion guards,
refcount discipline, lazy-init races, uninitialised dealloc, and allocation overflow. That is a
biased sample of bug-space: most of those classes are local contract omissions with no structural
reason to prefer complex functions. **The verdict is scoped to this bug family.** A run targeting
logic errors in state machines or numeric edge cases would plausibly find a much stronger
correlation — and the genuinely complex `Objects/` code (`long_pow`, `pack_single`, `mark_stacks`)
was not reviewed this run at all.

---

## 2. Inverse check — are the most complex functions clean?

Top 12 of 505 by the tool's own score, with complete extraction:

| # | function | site | score | lines | nest | cyc | goto | shipped tool sees it? | defect? |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `_Py_subs_parameters` | genericaliasobject.c:405 | 3.1 | 149 | 4 | 31 | 0 | yes | **YES — UAF + unguarded recursion (FIX×2)** |
| 2 | `_Py_make_parameters` | genericaliasobject.c:185 | 1.9 | 75 | 5 | 21 | 0 | yes | **YES — CPY-0002 recursion (FIX)** |
| 3 | `structseq_new_impl` | structseq.c:166 | 1.9 | 92 | 3 | 20 | 0 | **NO** | **YES — heap-buffer-overflow WRITE site (FIX)** |
| 4 | `template_new` | templateobject.c:92 | 1.9 | 81 | 4 | 17 | 0 | yes | no |
| 5 | `ga_repr` | genericaliasobject.c:89 | 1.8 | 47 | 3 | 16 | 8 | yes | no — cited as the **guarded twin** |
| 6 | `func_new_impl` | funcobject.c:1014 | 1.7 | 66 | 3 | 22 | 0 | **NO** | no |
| 7 | `tuple_repr` | tupleobject.c:285 | 1.6 | 50 | 3 | 15 | 7 | yes | no — POLICY only (unguarded prealloc, safe) |
| 8 | `structseq_repr` | structseq.c:273 | 1.6 | 50 | 3 | 13 | 9 | yes | **YES — prealloc overflow + OOB read (FIX)** |
| 9 | `subs_tvars` | genericaliasobject.c:273 | 1.6 | 43 | 5 | 11 | 0 | **NO** | **YES — `Py_DECREF(NULL)` (FIX)** |
| 10 | `PyObject_ClearWeakRefs` | weakrefobject.c:1014 | 1.4 | 64 | 3 | 16 | 0 | yes | no — ACCEPTABLE (bounded count) |
| 11 | `_unpack_args` | genericaliasobject.c:363 | 1.4 | 37 | 4 | 13 | 0 | yes | no (its 2026 bug was fixed by gh-150146) |
| 12 | `property_copy` | descrobject.c:1775 | 1.3 | 28 | 1 | 16 | 0 | yes | no |

**Five of the top ten carry a confirmed FIX.** That is the strongest pro-complexity result in this
report and it should be reported as such: at 2% of the review budget, the metric delivers a queue
that is more than half true positives.

Three caveats that keep it honest:

1. **The five hits are two clusters, not five independent confirmations.** Ranks 1, 2, 9 are the
   mutually-recursive parameter-substitution core of `genericaliasobject.c`; ranks 3 and 8 are the
   `n_fields`-driven struct-sequence family in `structseq.c`. The metric found *two
   neighbourhoods*. That is still valuable — but "5 of 10" overstates the independence.
2. **The shipped tool would have shown you a worse top-10.** Two of the five hits
   (`structseq_new_impl`, `subs_tvars`) are invisible to the regex extractor. As shipped the
   top-10 contains 3 defect-bearing functions; with complete extraction, 5. **The coverage bug
   costs 40% of the metric's best yield.**
3. **Complexity ranked the guarded twin above the unguarded sibling in the class that dominates
   this run.** `tuple_repr` (rank 7, score 1.6) is *correct*; `tuple_hash` (rank 88, score 1.0) is
   CPY-0001. `ga_repr` (rank 5) is cited by the recursion agent as the guarded twin; `ga_hash`
   (rank 173) is the FIX. The repr functions score high **because they carry the
   `Py_ReprEnter`/`Py_ReprLeave` bracket and a `PyUnicodeWriter` error ladder** — the very
   machinery whose absence is the bug. For the recursion-guard class specifically, the metric is
   **anti-correlated**.

**Why the clean five are clean.** `template_new`, `func_new_impl`, `property_copy` are
constructors and argument marshalling: long, branchy, mechanically reviewed, covered by the
Argument Clinic contract. `PyObject_ClearWeakRefs` is 64 lines of callback-reentrancy handling in
a file with 53 historical bug-fix commits — it is complex *because* 53 bugs were fixed in it. This
is the standard survivorship effect: **in a 35-year-old codebase, high complexity marks where
attention has already been paid.** The two clusters that broke the pattern are both young code —
`genericaliasobject.c` has 5 distinct 2026 fixes, and the `structseq` `n_fields` surface is
reachable only through a writable type attribute nobody audited.

---

## 3. `genericaliasobject.c` — is complexity the explanation? **Partly. The mechanism is duplication of three unexpressed contracts.**

The file is the sample's #1 recency-weighted target: 5 distinct 2026 fixes in 1070 lines, and per
the history agent one commit *introduced* a bug while fixing another. Cross-reading the eight
reports, it carries **six confirmed defect-bearing functions**, not four: `_Py_make_parameters`,
`subs_tvars`, `_Py_subs_parameters` (two independent defects), `ga_getitem`, `ga_hash`,
`ga_iternext`.

Its complexity profile is genuinely the sample's highest (mean cyclomatic 5.3; ranks 1, 2, 5, 9,
11 of 505), and for the two big functions complexity **is** causal. But three of the six defects
are in functions ranked 92, 173 and 176. Complexity is not the general explanation. The general
explanation is visible in the source:

> **Every defect in this file is a site where one of three contracts is honoured in one copy of a
> duplicated idiom and violated in another.** In each case the correct sibling is in the same
> file. This is a fix-propagation failure, not a comprehension failure.

### Contract A — `tuple_extend` / `_PyTuple_Resize` NULL the destination on failure

`_PyTuple_Resize` decrefs the old tuple and sets `*pv = NULL` on **every** failure path
(`Objects/tupleobject.c:1085-1091`). `tuple_extend` (`:169-183`) forwards that without documenting
it. It has **exactly two callers**, and they disagree:

```c
/* subs_tvars:297-304  -- WRONG */              /* _Py_subs_parameters:549-557 -- RIGHT */
j = tuple_extend(&subargs, j, ...);             jarg = tuple_extend(&newargs, jarg, ...);
if (j < 0) {                                    Py_DECREF(arg);
    Py_DECREF(subparams);                       if (jarg < 0) {
    Py_DECREF(subargs);   /* always NULL */          Py_DECREF(item);
    return NULL;                                     Py_XDECREF(tuple_args);
}                                                    assert(newargs == NULL);   /* the contract */
                                                     return NULL;
                                                 }
```

`Py_DECREF(NULL)` — `genericaliasobject.c:302`, reproduced under `set_nomemory` with `op=0x0`. The
guarded twin is 250 lines away in the same file and even *asserts* the invariant. Two more twins
exist: `_Py_make_parameters:243-247` (post-gh-148222) and `structseq.c:522-525`
(`assert(keys == NULL)`).

The convention is applied four different ways at four sites in this one file — ignore (`:243`),
`Py_XDECREF` (`:258`), `assert` (`:555`), `Py_DECREF` (`:302`). **Four sites, four handlings, one
crash.** The provenance confirms the propagation failure: gh-145376 *added* the bad `Py_DECREF` in
March 2026; gh-148222 fixed the identical shape 60 lines above in April and did not touch it;
gh-150146 fixed a third site in May and did not touch it.

### Contract B — `args` is aliased to `tuple_args` when the input is a list

Both big functions open with the same three lines (`:190-196` and `:458-465`):

```c
PyObject *tuple_args = NULL;
if (is_args_list) {
    args = tuple_args = PySequence_Tuple(args);   /* args and tuple_args are ONE object */
```

After this, `Py_XDECREF(tuple_args)` invalidates `args`. The file hand-writes that cleanup **16
times** (7 in `_Py_make_parameters`, 9 in `_Py_subs_parameters`). Fifteen are ordered correctly.
One is not:

```c
/* _Py_subs_parameters:537-547 */
        Py_XDECREF(tuple_args);                              /* :541 frees the object ... */
        PyObject *original = PyTuple_GET_ITEM(args, iarg);   /* :542 ... and reads it. UAF */
        PyErr_Format(PyExc_TypeError, "... %T ... %T", original, arg);   /* %T derefs it */
```

ASan-confirmed heap-use-after-free. Reachable only when `args` arrived as a *list* — the
`list[[T]]` nested-parameter-list path — which is why fifteen correct copies and one wrong one
survived review. Below the 20-element tuple freelist threshold the freed memory reads back intact,
so it is invisible without ASan. **The bug is not that the function is 149 lines; it is that the
aliasing is invisible at the 16 cleanup sites where it matters.**

`_Py_subs_parameters` has **12 `return NULL` exits and 33 refcount operations in 164 lines**, each
exit hand-composing its own subset of {`newargs`, `item`, `tuple_args`}. Twelve chances to get a
3-element cleanup set wrong; one is wrong.

### Contract C — the `->parameters` lazy init is duplicated, and the 2026 fix landed on two of three

| site | accessor | critical section | raw store (leak)? |
|---|---|---|---|
| `unionobject.c:327` `union_init_parameters` | both union accessors | **yes** (gh-132713, 2025-04) | **yes** |
| `genericaliasobject.c:844` `ga_parameters_lock_held` | `__parameters__` getset | **yes** (gh-153298, 2026-07-08) | **yes** |
| **`genericaliasobject.c:583` `ga_getitem`** | **`alias[...]` mp_subscript** | **NO** | **yes** |
| **`descrobject.c:624` `descr_get_qualname`** | **`__qualname__` getset** | **NO** (TSAN-0043) | **yes** |

Commit `68abf17fa92` (gh-153298, **7 days before HEAD**) created the `ga_parameters_lock_held`
split specifically to add the critical section — and did not touch the byte-identical copy 260
lines earlier in `ga_getitem`. As the FT agent puts it: *"a critical section held by only one of
two accessors serializes nothing."* Three TSan races reproduced. `ga_getitem` is `mp_subscript`,
a hotter path than the getset that got fixed. This is the history agent's "fix introduced a bug"
— the fix created the asymmetry that is now the bug.

Note the second column: **all four sites use a raw assignment rather than `Py_XSETREF`**, so even
the two "fixed" ones leak under single-threaded re-entrancy through
`PyObject_HasAttrWithError(__typing_subst__)` (measured +1 refcount). The contract has *never*
been fully expressed anywhere.

`ga_getitem` scores 1.0, rank 92. `ga_parameters` — the fixed twin — scores 1.0, rank 354. **No
complexity metric distinguishes a guarded lazy init from an unguarded one.**

### Diagnosis

For `_Py_subs_parameters` and `_Py_make_parameters`, complexity is causal: 12 hand-composed
cleanup ladders is what produced the UAF. For `subs_tvars`, `ga_getitem`, `ga_hash` and
`ga_iternext`, it is not — those are ordinary-sized functions carrying an unexpressed contract.
The unifying cause is that three ownership contracts are documented nowhere except in the correct
copies of duplicated code. **The 2026 fix cadence — 4 fixes in 5 months, two in the same function
— is what you get when each fix patches one copy of an idiom that exists in two to four places.**

### Concrete structural proposal

**P1 — make `tuple_extend` unable to misreport (fixes `:302`).**
Take the failure out of the return channel so the caller cannot mistake it for an ordinary error:

```c
/* Returns 0 on success, -1 on failure. On failure *dst is NULL and has already
   been released -- the caller must NOT decref it. */
static int
tuple_extend(PyObject **dst, Py_ssize_t *dstindex, PyObject **src, Py_ssize_t count);
```

The `Py_ssize_t`-returning-index-or-`-1` signature is precisely what let `subs_tvars` run a normal
cleanup ladder on the failure. Minimum viable version, one line and no API change: add
`assert(*dst == NULL);` inside `tuple_extend`'s failure branch — that turns `:302` into an
immediate debug-build abort under the existing test suite.

**P2 — eliminate the `args`/`tuple_args` alias (fixes `:542`, removes 16 cleanup lines).**
The alias exists only so the body can use `PyTuple_GET_*` on a possibly-list input. Use one owned
local and never rebind the parameter:

```c
/* replaces :190-196 and :458-465 */
PyObject *argstup = PyList_Check(args) ? PySequence_Tuple(args) : Py_NewRef(args);
if (argstup == NULL) { ... }
/* body uses argstup throughout; every exit does exactly Py_DECREF(argstup) */
```

Cleanup becomes unconditional and identical at every exit, and because `args` is never rebound, a
read of `args` after cleanup is a use of a demonstrably different object. **The UAF at `:542`
becomes unwritable.**

Combined with a single `goto error:` epilogue for the twelve exits — the idiom `tuple_repr` and
`structseq_repr` in this same sample already use (7 and 9 gotos, and neither has a *cleanup*
defect) — `_Py_subs_parameters` drops from 33 refcount operations to ~12 and from 12
hand-composed cleanup sets to one:

```c
error:
    Py_XDECREF(newargs);
    Py_XDECREF(item);
    Py_XDECREF(argstup);
    return NULL;
```

Estimated: score 3.1 -> ~1.6, cyclomatic 31 -> ~24, body 149 -> ~115 lines. The score barely moves
— which is the point of this report — but the number of places a cleanup set can be composed wrong
goes from 12 to 1.

**P3 — collapse the lazy-init copies to one helper (fixes `:583`; generalises to `descrobject.c:624`).**
One accessor, `Py_XSETREF` for the store, and the raw field never read outside it:

```c
static PyObject *
ga_get_parameters(PyObject *self)          /* returns a new reference */
{
    PyObject *result;
    Py_BEGIN_CRITICAL_SECTION(self);
    gaobject *alias = (gaobject *)self;
    if (alias->parameters == NULL) {
        PyObject *params = _Py_make_parameters(alias->args);   /* can re-enter */
        Py_XSETREF(alias->parameters, params);                 /* not a raw store */
    }
    result = Py_XNewRef(alias->parameters);
    Py_END_CRITICAL_SECTION();
    return result;
}
```

`ga_parameters` becomes a one-line forwarder; `ga_getitem:583-588` becomes a call plus a
`Py_DECREF`. The `Py_XSETREF` also closes the re-entrancy leak that all four current copies share
— including the two already "fixed". `unionobject.c:327` should take the same treatment via the
shared header, and the identical prescription fixes `descrobject.c:624`, which is the same
contract in a file with **zero critical sections and zero atomics**. **This is the
highest-value change of the three**: small, closes a live FT race on the hot subscript path, and
removes the shape gh-153298 half-fixed.

**P4 (POLICY, not mine to fix) —** `_Py_make_parameters` and `_Py_subs_parameters` are mutually
recursive over Python-controlled structure with no `Py_EnterRecursiveCall` (CPY-0002, plus a
second independent site at `:482`), and `ga_hash:615/619` descends both alias fields unguarded.
P1–P3 do not address these; they belong to the recursion-guard-auditor. I note only that a guard
at the two `_Py_*` definitions covers all four entry points including `typing.Union`.

---

## 4. Complexity patterns across the sample

- **`Objects/` is not a complex codebase by this metric, and the tool is not calibrated for it.**
  94.3% of sample functions score the floor; all of `Objects/` yields 3 hotspots, and the top one
  (`_PyUnicode_ToNumeric`, score 6.5, cyclomatic **2024**) is a **generated lookup table in
  `unicodetype_db.h`** — a pure false positive that will head every `hotspots` run on `Objects/`.
- **Zero gotos is the risk marker, not many gotos.** Of the 25 defect functions, **24 have
  `goto_count == 0`**; the sole exception is `structseq_repr` (9), whose defect is an arithmetic
  overflow, not a cleanup error. Meanwhile the three highest-goto functions in the sample are
  `structseq_repr` (9), `ga_repr` (8), `tuple_repr` (7). In CPython a single-exit `goto error:`
  epilogue is the **correct** idiom, and its absence — a function hand-composing N cleanup sets at
  N returns — is the actual risk shape. `_Py_subs_parameters` has 12 returns and 0 gotos.
- **Argument Clinic `*_impl` functions are the file-level complexity peak and are systematically
  invisible** to the shipped extractor: `structseq_new_impl` (rank 3, a FIX) and `func_new_impl`
  (rank 6) are both dropped.
- **The genuinely complex `Objects/` code was not reviewed this run**: `long_pow` (5.7),
  `pack_single` (5.3), `PyLong_AsNativeBytes` (4.7), `_Py_module_getattro_impl` (4.5),
  `mark_stacks` (4.1), `inherit_slots` (3.7). No agent touched those files.

---

## 5. Toolkit assessment

### 5.1 Precision

Not applicable in the detector sense. The analogue is what the shipped hotspot threshold surfaces,
and on this scope it surfaces **nothing** (0 in the sample) or **noise** (1 of 3 in all of
`Objects/` is a generated table). The metric's *ranking* precision is good: top-10 by score holds
5 of 25 defect functions (50% precision, p = 0.00004) with complete extraction — 3 of 10 with the
shipped extractor.

### 5.2 Recall gaps

**R1 — the regex extractor drops 22.4% of functions, biased toward complex ones.**
`measure_c_complexity.py:98-161` requires `^(\w+)\s*\(([^)]*)\)\s*$` on the line before the
column-0 `{`. CPython wraps parameter lists for functions with many parameters, so the drop rate
rises with parameter count — the tool loses precisely the functions its `parameter_count` metric
exists to score. Measured 113/505 in the sample (strict subset, zero regex-only names), including
**5 of 25 confirmed defect functions and 2 of the 5 top-10 hits**.

**R2 — the hotspot threshold is 3.4 points above where the signal lives.** `score >= 5.0` returns
zero on a 13,250-line sample containing 25 confirmed defects. `score >= 1.6` returns 9 functions
of which 5 are defect sites. The threshold is calibrated for a codebase that is not CPython.

**R3 — the score's weighting under-uses its best components.** With median body 8 and median
cyclomatic 2, the line-count term (`>50`/`>100`/`>200`) contributes nothing to 94% of functions,
and the nesting term is capped at `(depth-3)*0.25` below depth 6 — a maximum of +0.5 for almost
everything. Nesting is the second-best discriminator measured (`nesting >= 4`: 60% precision at
1.0% cost) and is the most heavily suppressed input.

**R4 — no metric models the shape that actually caused this run's marquee bug.** Twelve returns,
zero gotos, three owned locals. See T3.

### 5.3 Prompt issues

- The agent definition says "identify hotspots with score >= 5.0". On CPython `Objects/` that
  instruction yields an empty analysis and the agent looks broken. It needs a **relative**
  fallback: "if fewer than N functions clear the absolute threshold, rank by percentile within
  scope and report the top N."
- The prompt's guidance that "high goto counts in CPython are normal and not a complexity concern"
  is correct but stops one step short. The data says high goto count is a **positive** signal, and
  the prompt should say so, because the useful inverse (many returns + zero gotos + several owned
  locals) is the real risk shape and nothing currently computes it.
- Nothing told me to check whether my own script shares the chassis' extraction defect. Given the
  run-level caveat, every measurement agent should be required to state its extractor and its
  measured coverage.
- The prompt frames the agent as a *ranking* tool. Its most valuable output here was
  **correlation against another agent's findings** — that mode should be a first-class instruction
  when the agent runs late in a pipeline.

### 5.4 Concrete tuning proposals, ranked by value

**T1 (highest) — fix `find_functions` to accept multi-line parameter lists.**
In `measure_c_complexity.py:108-122`, when `lines[i-1]` fails both signature patterns, walk
backwards up to 6 lines accumulating text until parenthesis depth balances, then re-match.
Recovers 113 functions (22.4%) and 40% of the metric's top-10 defect yield on this sample.
Longer term, **replace `find_functions` with `tree_sitter_utils.extract_functions`** to inherit the
chassis — but only *after* the brace-unbalanced-macro fix lands, since that would trade a 22.4%
loss for `dictobject.c`'s ~50% loss. Until then, run both and take the union (measured strictly
larger than either on every file checked).

**T2 — lower the CPython hotspot threshold to 1.5 and rebalance the weights.**
In `measure_c_complexity.py:222-247`:
```python
# nesting: currently capped at +0.5 below depth 6 -- best precision, most suppressed
if max_depth > 4:   score += min((max_depth - 4) * 0.8, 2.5)
elif max_depth > 2: score += (max_depth - 2) * 0.4
# line count: start earlier; CPython median is 8 lines
if   line_count > 100: score += min((line_count - 100) / 60, 3.0)
elif line_count > 30:  score += (line_count - 30) / 70 * 1.5
# cyclomatic: start at 6, not 10
if   cyclomatic > 15: score += min((cyclomatic - 15) / 8, 2.5)
elif cyclomatic > 6:  score += (cyclomatic - 6) / 9
```
Regression target on this sample: the 9 functions at `score >= 1.6` today (5 of them defect sites)
should land above the new threshold, and fewer than ~20 functions total should clear it. Better
still, make the threshold **relative** — "top 2% within scope" — so it self-calibrates and never
returns an empty hotspot list.

**T3 — add a `manual_cleanup_ladder` metric and finding type.**
This is the metric that would have caught the marquee UAF. Per function:
```
returns_with_cleanup = # `return` statements preceded within 5 lines by Py_(X)DECREF
owned_locals         = # distinct identifiers appearing in Py_(X)DECREF calls
risk = returns_with_cleanup * owned_locals   if goto_count == 0 else 0
```
`_Py_subs_parameters` scores 12 x 3 = **36**; `_Py_make_parameters` 7 x 2 = 14; every clean
top-10 function scores 0 because it uses a `goto error:` epilogue. Emit
`type: "manual_cleanup_ladder"` above ~12 with "N returns each hand-composing a cleanup set over M
owned references; consider a single `goto error:` epilogue". This is a *complexity* finding that
predicts *this codebase's* dominant bug family, which the current score does not.

**T4 — add a `duplicated_lazy_init` cross-function check.**
Three of this file's six defects, plus `descrobject.c:624`, are duplicated idioms where one copy is
guarded and another is not. Normalise the token stream of every
`if (<expr> == NULL) { <expr> = <call>; }` block, group identical normalisations across the scope,
and flag any group whose members disagree on being inside `Py_BEGIN_CRITICAL_SECTION` /
`*_lock_held` — or on using `Py_XSETREF` vs a raw store. On this sample it fires exactly four
times (`ga_getitem`, `ga_parameters_lock_held`, `union_init_parameters`, `descr_get_qualname`) and
catches two confirmed FIX sites with no false positives. Belongs in `scan_ft_races.py`, but it was
found by complexity-driven reading and should be recorded.

**T5 — exclude generated files from `discover_c_files`.**
`Objects/unicodetype_db.h` (`_PyUnicode_ToNumeric`, score 6.5, cyclomatic 2024) and
`Objects/clinic/*.h` (`code_replace` 4.2, `code_new` 3.0) occupy 3 of the top 14 slots for all of
`Objects/`. Skip `*/clinic/*`, `*_db.h`, and any file whose first 5 lines match
`/[Aa]uto-?generated|Argument Clinic|DO NOT EDIT/`, or tag them `generated: true` and rank them
separately. One-line fix; removes the #1 `Objects/` hotspot as a false positive.

**T6 (doc) — add a calibration entry to `data/cpython_non_bugs.md`.**
> **Complexity ranks, it does not gate — and it is anti-correlated with the recursion-guard class.**
> Measured on the 14-file `Objects/` sample (505 functions, 25 confirmed defect functions): the
> top 10 by score hold 5 defect sites (p = 0.00004, 50% precision), but 20 of 25 defects sit at the
> score floor, so a complexity gate discards 80% of real bugs. For the recursion-guard family the
> signal inverts: the *guarded* twin scores higher than the unguarded sibling every time
> (`tuple_repr` 1.6 vs `tuple_hash` 1.0; `ga_repr` 1.8 vs `ga_hash` 1.0), because the guard and its
> error ladder are what the metric is counting. Never use a low score to deprioritise a function
> for a contract-omission class.

---

## 6. Classes bounded (clean negatives)

- **No function in the 14-file sample reaches the shipped hotspot threshold of 5.0.** Max is 3.1.
  Verified on complete tree-sitter extraction, not just the regex subset — the ceiling is real,
  not a coverage artifact.
- **The seven clean functions in the top 12 are genuinely clean**, and three of them
  (`ga_repr`, `tuple_repr`, `PyObject_ClearWeakRefs`) are explicitly cited by other agents as
  guarded twins or ACCEPTABLE. Cross-checked against all eight reports.
- **`capsule.c`, `interpolationobject.c`, `cellobject.c`** — 40 functions, mean cyclomatic 2.5,
  max score 1.0, zero defects found by any agent. Structurally trivial and confirmed clean;
  consistent with the history agent marking `capsule.c` and `cellobject.c` dormant.
- **Complexity did not miss a whole file.** Every file with zero confirmed defects also has
  below-median mean complexity. The failures are all *within*-file rank failures, not file-level
  blindness — which is why T2's relative threshold is worth more than a better absolute one.

---

## Scope escapes

- The genuinely complex `Objects/` code was not reviewed this run: `long_pow` (5.7),
  `pack_single` (5.3), `PyLong_AsNativeBytes` (4.7), `_Py_module_getattro_impl` (4.5),
  `mark_stacks` (4.1), `inherit_slots` (3.7). If a future run wants to test the
  complexity-predicts-defects hypothesis on a bug family where it should hold — logic errors in
  multi-branch state machines, numeric edge cases — `longobject.c` and `memoryobject.c` are the
  targets, and this run provides no evidence about them either way.
- `descrobject.c:624` shares Contract C with `genericaliasobject.c:583`; proposal P3 fixes both.
  `unionobject.c:327` is already guarded but shares the raw-store half and should migrate to the
  same shared helper.
