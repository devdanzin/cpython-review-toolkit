---
description: "Quick health dashboard — all agents in summary mode"
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# CPython C Code Health Dashboard

Run all agents in summary mode to produce a quick health dashboard. Each agent reports only its top-level findings — no deep analysis.

**Scope:** "$ARGUMENTS" (default: entire project)

## Workflow

1. Identify CPython project root (look for Include/Python.h and Objects/object.c)
2. Run **include-graph-mapper** first (structural context for all other agents)
3. Run all remaining agents with context, requesting summary-tier output only. Run at most 2 concurrently to limit memory usage.
4. Deduplicate before scoring: when the same issue is flagged by multiple agents, count it once.
5. Synthesize into a health dashboard:

```markdown
# CPython C Code Health Dashboard

| Dimension          | Status      | Score | FIX | Top Finding                    |
|--------------------|-------------|-------|-----|--------------------------------|
| Refcount Safety    | 🟢/🟡/🔴  | X/10  | N   | [1-line summary]               |
| Error Handling     | 🟢/🟡/🔴  | X/10  | N   | [1-line summary]               |
| GIL Discipline     | 🟢/🟡/🔴  | X/10  | N   | [1-line summary]               |
| Complexity         | 🟢/🟡/🔴  | X/10  | N   | [1-line summary]               |
| NULL Safety        | 🟢/🟡/🔴  | X/10  | N   | [1-line summary]               |
| PEP 7 Style        | 🟢/🟡/🔴  | X/10  | N   | [1-line summary]               |
| API Deprecation    | 🟢/🟡/🔴  | X/10  | N   | [1-line summary]               |
| Macro Hygiene      | 🟢/🟡/🔴  | X/10  | N   | [1-line summary]               |
| Memory Patterns    | 🟢/🟡/🔴  | X/10  | N   | [1-line summary]               |
| Include Graph      | 🟢/🟡/🔴  | X/10  | N   | [1-line summary]               |

## Overall Health: X/10

## Top 3 Priorities
1. [Most impactful improvement]
2. [Next]
3. [Next]

For detailed analysis, run:
  /cpython-review-toolkit:explore . [aspect] deep
```

## Scoring Rubric

Each dimension is scored 1-10:

- **10**: Exceptional — no findings above ACCEPTABLE
- **8-9**: Healthy — only CONSIDER-level findings
- **6-7**: Good with gaps — a few FIX items
- **4-5**: Concerning — multiple FIX items
- **2-3**: Problematic — many FIX items or systemic issues
- **1**: Severe — fundamental correctness issues

Score deductions:
- Each FIX finding: -0.5 to -1.0
- Systemic CONSIDER pattern: -0.5
- Individual CONSIDER finding: -0.1 to -0.2

🟢 8-10 | 🟡 5-7 | 🔴 1-4

## Usage

```
/cpython-review-toolkit:health              # Full project health
/cpython-review-toolkit:health Objects/     # Objects directory health
```
