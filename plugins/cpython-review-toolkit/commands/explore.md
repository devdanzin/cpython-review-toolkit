---
description: "Comprehensive CPython C code exploration and analysis using specialized agents"
argument-hint: "[scope] [aspects] [options]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Comprehensive CPython C Code Exploration

Run a comprehensive analysis of CPython C source code using multiple specialized agents, each focusing on a different aspect of C code quality. The include-graph-mapper runs first to provide structural context for all subsequent agents.

**Arguments:** "$ARGUMENTS"

## Argument Parsing

Parse arguments into three categories:

**Scope** (path or glob):
- `.` or omitted → entire project (default)
- `Objects/` → specific directory tree
- `Objects/listobject.c` → specific file
- `Modules/_io/` → specific module

**Aspects** (which agents to run):
- `includes` → include-graph-mapper only
- `refcounts` → refcount-auditor
- `errors` → error-path-analyzer
- `gil` → gil-discipline-checker
- `complexity` → c-complexity-analyzer
- `style` → pep7-style-checker
- `null-safety` → null-safety-scanner
- `deprecation` → api-deprecation-tracker
- `macros` → macro-hygiene-reviewer
- `memory` → memory-pattern-analyzer
- `all` → all agents (default)

**Options**:
- `deep` → full detail, no output truncation
- `summary` → summary tier only (faster)
- `parallel` → run agents concurrently where possible
- `--max-parallel N` → cap concurrent agents per group (default: 2)

## Execution Workflow

### Phase 0: Project Discovery

Before launching any agents:
1. Identify CPython root (look for Include/Python.h and Objects/object.c)
2. Count .c and .h files in scope
3. Check for CLAUDE.md or equivalent project documentation
4. Identify CPython version if possible (Include/patchlevel.h)
5. Print a brief project summary to confirm scope

### Phase 1: Foundational Context (always runs first)

Launch the foundational context provider with the specified scope:

**Group 0 — Structural context (always runs first)**:
- **include-graph-mapper** — include dependencies, API tiers, coupling

Store output for injection into Phase 2 agents.

### Phase 2: Targeted Analysis

Based on the requested aspects (default: all), launch the appropriate agents. Each agent receives the specified scope and the include-graph-mapper output as structural context.

**Agent dispatch order** (sequential by default):

**Group A — Safety-critical analysis** (highest value):
1. refcount-auditor
2. error-path-analyzer

**Group B — Memory safety**:
3. null-safety-scanner
4. gil-discipline-checker

**Group C — Code quality**:
5. c-complexity-analyzer
6. pep7-style-checker

**Group D — Maintenance and hygiene**:
7. api-deprecation-tracker
8. macro-hygiene-reviewer
9. memory-pattern-analyzer

If `parallel` is specified, run agents within each group concurrently. Run at most `--max-parallel` agents concurrently within each group (default: 2). Groups still execute sequentially because later groups may benefit from earlier findings.

### Phase 3: Synthesis

After all agents complete, perform deduplication and conflict resolution, then produce a unified summary.

#### Deduplication and Conflict Resolution

1. **Merge overlapping findings**: When two or more agents flag the same file:line, merge them into a single finding:
   ```
   - [refcount-auditor, error-path-analyzer]: Leaked reference on error
     path in list_extend (Objects/listobject.c:842)
   ```

2. **Surface contradictions**: When agents disagree, present both sides:
   ```
   ## Tensions
   - **Complexity vs. correctness** at Python/ceval.c:1200:
     c-complexity-analyzer flags high complexity.
     refcount-auditor confirms all paths are correct.
     → Complexity is essential to the instruction dispatch loop.
   ```

3. **Attribute to the most specific agent**: Refcount issues → refcount-auditor (not error-path-analyzer).

#### Summary Template

```markdown
# CPython C Code Exploration Report

## Project: CPython [version]
## Scope: [what was analyzed]
## Agents Run: [list]

## Executive Summary
[3-5 sentence overview of C code health across all dimensions]

## Key Metrics
- Refcount Safety: [status] — [summary]
- Error Handling: [status] — [summary]
- GIL Discipline: [status] — [summary]
- Complexity: [N hotspots, N critical]
- NULL Safety: [N unchecked allocations]
- PEP 7 Style: [N violations]
- API Deprecation: [N deprecated usages]
- Macro Hygiene: [N issues]
- Memory Patterns: [N issues]
- Include Graph: [status]

## Findings by Priority

### Must Fix (FIX)
[Crash risks, memory corruption, undefined behavior]

### Should Consider (CONSIDER)
[Improvement opportunities with trade-offs]

### Tensions
[Where agents disagree]

### Policy Decisions (POLICY)
[Team-level decisions needed]

## Strengths
[What the C code does well]

## Recommended Action Plan

### Immediate
1. [FIX items — safety-critical]

### Short-term
1. [CONSIDER items — quality improvements]

### Ongoing
1. [POLICY decisions to make]
```

## Usage Examples

**Full exploration:**
```
/cpython-review-toolkit:explore
```

**Specific scope:**
```
/cpython-review-toolkit:explore Objects/
```

**Specific aspects:**
```
/cpython-review-toolkit:explore . refcounts errors
/cpython-review-toolkit:explore . complexity style
```

**Quick summary:**
```
/cpython-review-toolkit:explore . all summary
```

**Deep dive on a module:**
```
/cpython-review-toolkit:explore Modules/_io/ all deep
```

## How Foundational Context Flows

When passing include-graph-mapper output to other agents:

```
[Include include-graph-mapper output]

The above is the include dependency analysis of this CPython codebase. Use it to:
- Understand which files include which headers (coupling)
- Prioritize findings in highly-depended-on code (high fan-in)
- Identify when an issue is in public API headers vs. internal code
- Calibrate severity based on API tier (public > cpython > internal)
```

## Tips

- **Start broad, then narrow**: Run with `summary` first, then drill into specific aspects
- **Safety first**: The `refcounts errors` aspects find the most impactful bugs
- **Complexity + safety**: Functions that are both complex and have refcount issues are highest priority
- **Scope to what matters**: Objects/ and Python/ contain the most critical code
