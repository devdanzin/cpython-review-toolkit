---
description: "Find cleanup targets — complexity hotspots, refcount issues, and error handling bugs"
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# CPython C Code Hotspots

Run the highest-value agents to find the worst functions to fix first: the crash-class detectors plus **c-complexity-analyzer**, **refcount-auditor**, and **error-path-analyzer**. Answers the question: "Where should I focus my review efforts?"

**Scope:** "$ARGUMENTS" (default: entire project)

## Workflow

1. Identify CPython project root
2. Run **include-graph-mapper** first (structural context)
3. Run with at most 2 agents in parallel, feeding context:
   - **recursion-guard-auditor** — native-stack-overflow SIGSEGV in recursion-prone slots
   - **pyerr-clear-auditor** — exception-clobber in the destructor family
   - **uninitialized-dealloc-auditor** — half-built object freed on an error path
   - **init-bypass-checker** — NULL field deref after `__new__` bypass / a deletable member
   - **refcount-auditor** — find reference counting errors
   - **error-path-analyzer** — find error handling bugs
   - **c-complexity-analyzer** — find the hardest-to-maintain code
4. Synthesize into a prioritized hotspot report:

```markdown
# CPython C Code Hotspots

## Critical Issues (FIX)
[Refcount leaks, NULL dereferences, error handling bugs]
- [agent]: Issue in `function` (file.c:line)

## Complexity Hotspots
_Relative threshold: top 2% by score (`hotspot_threshold` from the script).
Extraction coverage: N%._

| Rank | Function | File | Score | Lines | Gotos | Top Issue |
|------|----------|------|-------|-------|-------|-----------|
| 1    | func     | f.c  | 7.3   | 450   | 28    | Deep nesting |

## Manual Cleanup Ladders (goto-free cleanup burden)
| Rank | Function | File | Ladder | Owned locals | Returns w/ cleanup |
|------|----------|------|--------|--------------|--------------------|
| 1    | func     | f.c  | 78     | 6            | 13                 |

## Error-Prone Functions
[Functions appearing in BOTH a crash-class agent's findings and either
complexity list — the intersection is the real priority]

## Recommended Fix Order
1. [Highest-impact fix]
2. [Next]
3. [Next]
```

## Interpreting the complexity signal

**The complexity threshold is relative, not absolute.** `measure_c_complexity.py`
selects the top 2% by score (`--top-percent`, optional `--min-score` floor). The
old absolute `score >= 5.0` cutoff fired **3 times across all of `Objects/`**,
whose maximum observed score is 7.3 — it was effectively "never".

**Complexity ranks well but gates badly.** Measured on a 14-file `Objects/`
sample with 25 confirmed defect-bearing functions: the top 10 by score held 5 of
the 25 (10x enrichment), but **20 of the 25 sat at the score floor**. Use the
ranking to choose a reading order; never use it to *exclude* a function, and
never report a low-scoring function as lower risk.

**Do not use complexity as a severity input for the crash-class agents.** For
recursion findings the metric inverts — `tuple_repr` 1.6 vs the buggy
`tuple_hash` 1.0 — because the recursion guard is itself a branch, so the
correctly-guarded twin outscores the defective one. The same caution applies to
free-threading findings: `descr_get_qualname` is 4 lines, cyclomatic 2, ranked
257th, and carries a confirmed FT race.

**A `goto` ladder is a positive signal in CPython.** 24 of those 25 defect
functions had zero gotos. The `manual_cleanup_ladder` column
(`owned_locals x returns_with_cleanup`, reported only when `goto_count == 0`)
is the complementary metric: it ranks `_Py_subs_parameters` first on `Objects/`
while every top-10-by-score function scores 0. Report both lists and call out
where they disagree.

**Check `coverage.coverage_pct`** (~98.5% on CPython) before trusting any
ranking — a lower value means functions were silently dropped from the corpus.

## Usage

```
/cpython-review-toolkit:hotspots              # Entire project
/cpython-review-toolkit:hotspots Objects/     # Objects directory
/cpython-review-toolkit:hotspots Python/      # Python directory
```
