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
| `tsan` | `scan_ft_races.py` |
| `init-bypass` | `scan_init_bypass.py` |

Every shipped category has a scanner as of v0.7 — `no_scanner: 0` is the expected
result, and a non-zero count means a category was added to the catalog before its
scanner. Preserve that invariant when extending the catalog.

## Workflow

1. **Cross-reference:**
   ```
   python <plugin_root>/scripts/check_known_issues.py [scope]
   ```
   Optional flags: `--max-files N`, `--catalog <path>` (override the default TSV).
   Record the reviewed commit (`git -C <project_root> rev-parse --short HEAD`) — the catalog was captured at a slightly different commit, so line drift is expected.

2. **Read the `catalog_results` / `bug_rollup` blocks.** Each entry gets one of:
   - **`present`** — a finding of that category at (or within ±5 lines of) the catalog line, or — when the catalog line is unknown (`0`) — a finding in the named function. Still unfixed. → high confidence.
   - **`line_drifted`** — the file still has findings of that category but not at the catalog site, **and** the catalog's named function is gone (renamed/removed) or was never recorded (`0`). `nearest_line` points at the closest finding. The bug likely just moved. → medium.
   - **`absent_in_function`** — the catalog names a function, that function **still exists** in the file, and it carries **no** finding of the category — while the file has findings elsewhere. This is absence *in that function*, not drift. Drift says "still there, moved"; this says "possibly fixed here". → weak signal, not a finding.
   - **`absent`** — the file was scanned and has no findings of that category anywhere (likely fixed or refactored).
   - **`file_missing`** — the path no longer exists under the checkout.
   - **`no_scanner`** — a catalog category with no entry in `CATEGORY_SCANNERS`; carried through for completeness, not cross-referenced. As of v0.7 every shipped category has a scanner, so this should read `0`.

   The `bug_rollup` collapses a multi-site bug's sites into one verdict, strongest signal first: `present` > `line_drifted` > `absent_in_function` > `likely_fixed` > `no_scanner`.

   Only `present` and `line_drifted` become entries in `findings[]` — those are the regression signals. `absent_in_function` / `absent` are reported in `catalog_results` for triage but are not actionable on their own.

3. **Verify the drifted and absent ones.** For each `line_drifted` bug, Read the file around `nearest_line` and confirm the same bug shape. For each `absent_in_function` bug, Read *that function* — the scanner may simply not express the shape there. For each `absent` bug, Read the file to confirm the bug is actually gone.

> **Caveat (baked into the JSON `notes` field, repeated here):** an `absent` verdict is **NOT** proof of a fix. Some crash shapes carry no scannable token at the exact site — a native C-stack overflow (`recursion`), a data race (`tsan`) — so a fresh scan can read a still-unfixed bug as `absent`. **Always read the file before concluding a bug is fixed.** The same applies to `absent_in_function`. Likewise, any `no_scanner` entry is never cross-referenced and must be checked by hand.

## Output

```markdown
# Known-Issues Regression — CPython @ [commit]

Catalog: [N] entries, [B] distinct bugs, [F] files scanned.

| Bug | Verdict | Site(s) | Notes |
|-----|---------|---------|-------|
| gh-154318 | present | Objects/tupleobject.c:tuple_hash | copy-pasted guardless hash; deep tuple -> C-stack SIGSEGV |
| gh-154275 | line_drifted | Objects/genericaliasobject.c:~231 | parameter walk, nearest finding a few lines off |
| gh-149146 | absent_in_function | Objects/tupleobject.c:tuple_dealloc | function still present and clean; file has findings elsewhere |
| gh-152851 | likely_fixed | Modules/blake2module.c:py_blake2_clear | read the file to confirm |
| FUTURE-1  | no_scanner | (a catalog category added before its scanner) | check by hand |
| ... | | | |

**Still present: [n]**   **Drifted: [m]**   **Absent in function: [a]**   **Likely fixed: [k]**   **No scanner: [j]**

## Confirmed still-present (act on these)
[List the `present` bugs — the highest-confidence regression signals.]

## Absent in the named function
[List the `absent_in_function` bugs. The function is still there and clean —
read it to decide whether the bug was fixed or the scanner simply cannot
express the shape at that site.]

## Likely fixed since the catalog
[List the `absent` / `file_missing` bugs — verify each by reading the file.]

## Not cross-referenced (manual check required)
[List the `no_scanner` bugs, if any.]
```

Note: a `present` verdict means a previously-confirmed crash site is still in the tree. When sharing upstream, follow `WORKING_WITH_MAINTAINERS.md` and cite the catalog `bug_id`.

## Usage

```
/cpython-review-toolkit:known-issues                 # whole project
/cpython-review-toolkit:known-issues Objects/         # scope to Objects/
/cpython-review-toolkit:known-issues . --max-files 200
```
