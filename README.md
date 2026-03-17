# CPython C Code Review Toolkit

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin for exploring, analyzing, and reviewing CPython's C source code. It answers the question: **where are the bugs, style violations, and maintenance risks in this C codebase?**

Built for CPython's specific concerns — reference counting, GIL discipline, NULL safety, PEP 7 style — not general-purpose C analysis.

## Installation

### From the marketplace (recommended)

```bash
# Add the marketplace (one-time setup)
claude plugin marketplace add devdanzin/cpython-review-toolkit

# Install the plugin
claude plugin install cpython-review-toolkit@cpython-review-toolkit
```

### Direct install from GitHub

```bash
claude plugin install cpython-review-toolkit --source github:devdanzin/cpython-review-toolkit --path plugins/cpython-review-toolkit
```

### Without installing (try it first)

```bash
git clone https://github.com/devdanzin/cpython-review-toolkit.git
claude --plugin-dir cpython-review-toolkit/plugins/cpython-review-toolkit
```

## Quick Start

Navigate to a CPython source checkout, then:

```bash
/cpython-review-toolkit:map        # Understand include structure
/cpython-review-toolkit:health     # Quick health dashboard
/cpython-review-toolkit:hotspots   # Refcount leaks + error bugs + complexity
/cpython-review-toolkit:explore    # Full exploration (all 10 agents)
```

Start with `map` to understand the include graph, then `hotspots` to find the highest-impact bugs.

## What's Included

- **10 analysis agents** covering reference counting, error handling, GIL discipline, complexity, NULL safety, PEP 7 style, include dependencies, API deprecation, macro hygiene, and memory patterns.
- **4 commands** (`explore`, `map`, `hotspots`, `health`) for different analysis workflows.
- **7 analysis scripts** (stdlib-only Python) for include graphing, complexity measurement, refcount scanning, error path analysis, NULL safety checking, GIL usage scanning, and PEP 7 style checking.

## Prerequisites

- **Claude Code** installed and running.
- **Python 3.10+** for the analysis scripts (type syntax from 3.10+).
- No third-party packages — all scripts use only the standard library.

## How It Works

The scripts use regex-based scanning to find candidate issues in C source files. This is intentionally imprecise — scripts identify candidates with an expected 30-50% false positive rate, and the agents read the actual code to confirm or dismiss each finding. This approach works well for CPython because PEP 7 makes the code style very regular and predictable.

For detailed usage, agent descriptions, and recommended workflows, see the [plugin README](plugins/cpython-review-toolkit/README.md).

## License

MIT — see [LICENSE](LICENSE) for details.
