---
description: "Find cleanup targets — complexity hotspots, refcount issues, and error handling bugs"
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# CPython C Code Hotspots

Run the three highest-value agents to find the worst functions to fix first: **c-complexity-analyzer**, **refcount-auditor**, and **error-path-analyzer**. Answers the question: "Where should I focus my review efforts?"

**Scope:** "$ARGUMENTS" (default: entire project)

## Workflow

1. Identify CPython project root
2. Run **include-graph-mapper** first (structural context)
3. Run with at most 2 agents in parallel, feeding context:
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
| Rank | Function | File | Score | Lines | Top Issue |
|------|----------|------|-------|-------|-----------|
| 1    | func     | f.c  | 8.5   | 450   | Deep nesting |

## Error-Prone Functions
[Functions with both high complexity AND refcount/error issues]

## Recommended Fix Order
1. [Highest-impact fix]
2. [Next]
3. [Next]
```

## Usage

```
/cpython-review-toolkit:hotspots              # Entire project
/cpython-review-toolkit:hotspots Objects/     # Objects directory
/cpython-review-toolkit:hotspots Python/      # Python directory
```
