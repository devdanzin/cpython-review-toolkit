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

### After upgrading the plugin

**Run `/reload-plugins` before `explore` or `informed-explore`.** A Claude Code session registers dispatchable agents when the plugin is *loaded*; agents added by a version installed mid-session stay invisible until you reload, and those commands then run silently with the older, smaller agent set.

## Quick Start

Navigate to a CPython source checkout, then:

```bash
/cpython-review-toolkit:map            # Understand include structure
/cpython-review-toolkit:health         # Quick health dashboard
/cpython-review-toolkit:hotspots       # Crash-class detectors + refcount + complexity
/cpython-review-toolkit:explore        # Full exploration (all agents)
/cpython-review-toolkit:known-issues   # Regression check vs the known-crash catalog
/cpython-review-toolkit:reproduce      # Turn a static candidate into a reproduced crash
```

Start with `map` to understand the include graph, then `hotspots` to find the highest-impact bugs.

## What's Included

- **23 analysis agents** covering reference counting, error handling, GIL discipline, complexity, NULL safety, PEP 7 style, include dependencies, API deprecation, macro hygiene, memory patterns, and temporal history — plus tree-sitter-based **crash-class detectors** (recursion-guard gaps, destructor exception-clobber, uninitialized-dealloc, init-bypass), **free-threading / data-race detectors** (iterator double-DECREF, stop-the-world safety, lock discipline, TSan triage), a **differential parity checker** against CPython's own shipped pure-Python twins, and an **OOM reproducer** that turns a static candidate into a confirmed crash.
- **7 commands** (`explore`, `informed-explore`, `known-issues`, `reproduce`, `map`, `hotspots`, `health`) for different analysis workflows.
- **Analysis scripts** — stdlib-only regex scanners for the legacy dimensions, plus tree-sitter crash-class detectors, a `known-issues` regression checker, and an `informed-explore` briefing generator.

## Reviewing the whole tree, one slice at a time

An `informed-explore` run works best when the whole scope fits in one context.
Measured: a 13,250-line slice of `Objects/` dispatched 12 agents and triaged
well; a 39,800-line slice of `Modules/` dispatched only 6 and strained. So the
reviewable surface — every non-vendored `.c` file under `Objects/` and
`Modules/`, 188 files and ~358,000 lines — is partitioned into **37 slices** of
at most 13,000 lines each, tracked in
[`data/review_slices.json`](plugins/cpython-review-toolkit/data/review_slices.json).

```bash
python tools/slice_status.py --catalog-dir ~/projects/cpython-review-findings
python tools/make_slice_context.py mod-io --cpython ~/projects/cpython \
    --catalog-dir ~/projects/cpython-review-findings
/cpython-review-toolkit:informed-explore Modules/_io all
```

`slice_status.py` is the campaign cursor — progress per tier, findings per
slice, and the next slice to run. `make_slice_context.py` does the setup that
was previously manual: the run tree, the informed briefing, every scanner run
both corpus-wide and re-run slice-scoped (never post-filtered, so denominators
stay honest), the calibration/new-territory split, and the `RUN_CONTEXT.md` the
agents read. When a slice finishes, flip its `status` to `done` and commit.

CPython gains and loses files, so the manifest can silently stop covering the
tree. `--verify` re-walks a checkout and fails on any unassigned or vanished
file; `--sync` refreshes line counts.

## Prerequisites

- **Claude Code** installed and running.
- **Python 3.10+** for the analysis scripts.
- **tree-sitter + tree-sitter-c** (`pip install tree-sitter tree-sitter-c`) for the crash-class detectors, `known-issues`, and `informed-explore`; the legacy scanners remain stdlib-only.

## How It Works

Two generations of scanner coexist: stdlib-only **regex scanners** for the legacy dimensions (refcounts, errors, NULL, GIL, complexity, PEP 7, includes), and **tree-sitter detectors** that parse a real C syntax tree to target specific reachable-from-Python crash and data-race classes (validated against confirmed CPython crashes). All scripts report candidates — expect 10-50% false positives depending on the detector — and the agents read the actual code to confirm or dismiss each finding and classify it FIX / CONSIDER / POLICY / ACCEPTABLE.

Beyond static analysis, the **`reproduce`** command closes the loop: it runs real `_testcapi.set_nomemory` OOM sweeps against a locally-built CPython to turn a static candidate into a reproduced crash — with the discipline that a *non*-reproduction is reported honestly and does not by itself refute the finding.

For detailed usage, agent descriptions, and recommended workflows, see the [plugin README](plugins/cpython-review-toolkit/README.md).

## License

MIT — see [LICENSE](LICENSE) for details.
