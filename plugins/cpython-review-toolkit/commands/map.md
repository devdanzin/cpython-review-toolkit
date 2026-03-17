---
description: "Quick include graph mapping — understand CPython C file structure and dependencies"
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Include Graph Map

Run only the include-graph-mapper agent to quickly understand CPython's C file structure, include dependencies, and API tier boundaries.

**Scope:** "$ARGUMENTS" (default: entire project)

## Workflow

1. Identify CPython project root (look for Include/Python.h and Objects/object.c)
2. Launch **include-graph-mapper** agent with specified scope
3. Present the include graph directly — no synthesis needed

This is the fastest way to get oriented in a CPython C codebase. Use this before diving into specific analysis with other commands.

## Usage

```
/cpython-review-toolkit:map                  # Map entire project
/cpython-review-toolkit:map Objects/         # Map Objects directory
/cpython-review-toolkit:map Modules/_io/     # Map specific module
```
