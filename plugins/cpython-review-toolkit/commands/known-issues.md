---
description: "Cross-reference the seed catalog of previously-found CPython crashes against a fresh scan, so a fix (or a regression-after-revert) is detected. Static, drift-tolerant regression — no repros run."
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# CPython Known-Issues Regression

`data/cpython_known_bugs.tsv` records crash sites previously confirmed in CPython — from the findings repos (`OOM-####`, `TSAN-####`) and the upstream tracker (`gh-NNNNNN`). This command runs, per catalog entry, the scanner matching that entry's category over the referenced file and reports whether the known bug is still in the tree.

It is **static and drift-tolerant** — it does not run repros. It answers: "of the crashes we already know about, which are still present at this checkout, which have drifted, and which look fixed?"

**Scope:** "$ARGUMENTS" (path or glob; default the whole project). The scope's checkout root is auto-detected — catalog paths are resolved relative to it, so a narrow scope still locates the right files.

**Plugin root:** `<plugin_root>` is the `plugins/cpython-review-toolkit/` directory.

## Category → scanner map

| Category | Scanner |
|----------|---------|
| `recursion` | `scan_recursion_guards.py` |
| `pyerr-clear` | `scan_pyerr_clear.py` |
| `uninit-dealloc` | `scan_uninit_dealloc.py` |
| `null-deref` | `scan_null_checks.py` |
| `refcount` | `scan_refcounts.py` |
| `tsan`, `init-bypass` | *(no scanner in v0.5 → `no_scanner`)* |

## Workflow

1. **Cross-reference:**
   ```
   python <plugin_root>/scripts/check_known_issues.py [scope]
   ```
   Optional flags: `--max-files N`, `--catalog <path>` (override the default TSV).
   Record the reviewed commit (`git -C <project_root> rev-parse --short HEAD`) — the catalog was captured at a slightly different commit, so line drift is expected.

2. **Read the `catalog_results` / `bug_rollup` blocks.** Each entry gets one of:
   - **`present`** — a finding of that category at (or within ±5 lines of) the catalog line, or — when the catalog line is unknown (`0`) — a finding in the named function. Still unfixed. → high confidence.
   - **`line_drifted`** — the file still has findings of that category but not at the catalog site; `nearest_line` points at the closest one. The bug likely just moved a few lines. → medium.
   - **`absent`** — the file was scanned and has no findings of that category (likely fixed or refactored).
   - **`file_missing`** — the path no longer exists under the checkout.
   - **`no_scanner`** — `tsan` / `init-bypass` categories have no scanner in v0.5; carried through for completeness, not cross-referenced.

   The `bug_rollup` collapses a multi-site bug's sites into one verdict (`present` / `line_drifted` / `likely_fixed` / `no_scanner`).

3. **Verify the drifted and absent ones.** For each `line_drifted` bug, Read the file around `nearest_line` and confirm the same bug shape. For each `absent` bug, Read the file to confirm the bug is actually gone.

> **Caveat (baked into the JSON `notes` field, repeated here):** an `absent` verdict is **NOT** proof of a fix. Some crash shapes carry no scannable token at the exact site — a native C-stack overflow (`recursion`), a data race (`tsan`) — so a fresh scan can read a still-unfixed bug as `absent`. **Always read the file before concluding a bug is fixed.** Likewise, `no_scanner` entries (`tsan`, `init-bypass`) are never cross-referenced and must be checked by hand.

## Output

```markdown
# Known-Issues Regression — CPython @ [commit]

Catalog: [N] entries, [B] distinct bugs, [F] files scanned.

| Bug | Verdict | Site(s) | Notes |
|-----|---------|---------|-------|
| gh-154318 | present | Objects/tupleobject.c:tuple_hash | copy-pasted guardless hash; deep tuple -> C-stack SIGSEGV |
| gh-154275 | line_drifted | Objects/genericaliasobject.c:~231 | parameter walk, nearest finding a few lines off |
| OOM-0023  | likely_fixed | Objects/typeobject.c:subtype_dealloc | read the file to confirm |
| TSAN-0053 | no_scanner | Objects/dictobject.c:6158 | no tsan scanner in v0.5 — check by hand |
| ... | | | |

**Still present: [n]**   **Drifted: [m]**   **Likely fixed: [k]**   **No scanner: [j]**

## Confirmed still-present (act on these)
[List the `present` bugs — the highest-confidence regression signals.]

## Likely fixed since the catalog
[List the `absent` / `file_missing` bugs — verify each by reading the file.]

## Not cross-referenced (manual check required)
[List the `no_scanner` bugs — tsan / init-bypass.]
```

Note: a `present` verdict means a previously-confirmed crash site is still in the tree. When sharing upstream, follow `WORKING_WITH_MAINTAINERS.md` and cite the catalog `bug_id`.

## Usage

```
/cpython-review-toolkit:known-issues                 # whole project
/cpython-review-toolkit:known-issues Objects/         # scope to Objects/
/cpython-review-toolkit:known-issues . --max-files 200
```
