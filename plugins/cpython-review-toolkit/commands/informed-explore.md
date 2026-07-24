---
description: "Like `explore`, but INFORMED: every agent first reads a briefing of recurring CPython C bug SHAPES (sibling-hunt templates with their guarded twins), the cross-cutting triage rules, and the false-positive taxonomy — so it confirms-without-relitigating, suppresses known FPs, and hunts un-found siblings of known shapes instead of re-discovering from scratch. Use for a thorough audit, a re-review of a subsystem, or whenever fix-propagation matters more than raw speed."
argument-hint: "[scope] [aspects] [options]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# Informed CPython C Code Exploration

Same coverage as [`explore`](explore.md), but **informed**: every agent is seeded with the toolkit's
accumulated knowledge before it triages. That is what turns a cold, re-discovering pass into a
fix-propagation sweep — finding *every* instance of a known bug shape and naming the guarded twin that
fixes it. Validated pattern, ported from the sibling toolkits' informed runs.

**Arguments:** "$ARGUMENTS"

**Plugin root:** `<plugin_root>` is the `plugins/cpython-review-toolkit/` directory — this command
file's grandparent. Resolve it relative to this file.

## Argument Parsing

Identical to `explore`: **scope** (path or glob, default the whole checkout), **aspects** (the same
aspect→agent table as `explore.md`, default `all`), and **options** (`deep`, `summary`, `parallel`,
`--max-parallel N`).

## Execution Workflow

### Phase 0 — Project Discovery
Exactly as `explore` Phase 0: identify the CPython root (`Include/Python.h` + `Objects/object.c`),
count `.c`/`.h` files in scope, read the version from `Include/patchlevel.h`, print a brief scope
summary.

### Phase 0.5 — Build the informed briefing  ← the new step

```
python <plugin_root>/scripts/build_informed_briefing.py > reports/preflight/informed_briefing.md
```

If you keep the durable findings repo
([`cpython-review-findings`](https://github.com/devdanzin/cpython-review-findings)) checked out — the
static analog of the `cpython-oom-findings` / `cpython-tsan-findings` repos — add
`--catalog-dir <path-to-cpython-review-findings>` to fold its `reports/*/meta.json` entries into the
briefing as "confirm, don't re-litigate" leads, so a re-review is cumulative.

This assembles a Markdown briefing from three catalogs:
- **`data/cpython_bug_shapes.json`** — recurring CPython C bug SHAPES, each with its **guarded twin**
  (the correctly-handled sibling = the fix), a sibling-hunt directive, a confirming differential, and
  which scanner surfaces it.
- The **cross-cutting triage rules** (guarded-twin-is-the-signal, both-crash ≠ acceptable,
  reachability-first, confirm-don't-relitigate, Class-J out of scope).
- **`data/cpython_non_bugs.md`** — the false-positive taxonomy, reproduced inline.

Read `reports/preflight/informed_briefing.md` yourself before dispatching, and print the shape count
and whether a catalog dir was folded in.

### Phase 1 — Foundational Context (always runs first)
Dispatch **include-graph-mapper** with the scope, exactly as `explore` Phase 1. Store its output for
injection into Phase 2.

### Phase 2 — Targeted Analysis (informed)

Dispatch the aspect agents in the **same groups A–E as `explore`** (Safety-critical → Crash-class
detectors → Memory safety → Code quality → Maintenance → History). In addition to the scope and the
include-graph-mapper output, give **every** agent the contents of
`reports/preflight/informed_briefing.md` and these instructions:

> **Read the informed briefing first.** Then run your scanner and triage in informed mode:
> 1. **Confirm, don't re-litigate** a previously-recorded finding — tally it in one line and move on.
> 2. **Skip the false-positive classes** in the taxonomy; if you flag one of those shapes anyway, you
>    must justify why *this* instance is not that FP class.
> 3. **Hunt siblings** of the bug SHAPES relevant to you: for each finding, name its **guarded twin**
>    (the correctly-handled sibling in the same file/family = the fix), and search for the other sites
>    that lack it. New siblings + new territory are the goal, not re-describing the catalog. Where a
>    differential is listed for the shape, run it to confirm or kill the lead.

Groups run sequentially (History last, so it can cross-reference the others); `parallel` runs agents
*within* a group concurrently, capped by `--max-parallel`.

### Phase 3 — Synthesis

Produce the unified report exactly as `explore` Phase 3 (same summary template), with two additions:
1. A **"New siblings of known shapes"** subsection — the fix-propagation yield (the highest-value
   output of an informed run), each paired with its guarded twin.
2. A **"Classes bounded"** note — shapes you checked and found the scope is *clean* on (a negative a
   cold run never states).

If a finding is confirmed as novel and worth recording, add it to the `cpython-review-findings` repo
(the `reports/<id>/meta.json` layout) so the next informed run is seeded with it. Check the CPython
tracker (`label:type-crash`) for prior art before calling anything novel — a native-stack overflow or
UAF is frequently already known.

## Extending the catalogs

- A **new recurring bug shape** → add an entry to `data/cpython_bug_shapes.json` (`id`, `title`,
  `pattern`, `guarded_twin`, `hunt`, `severity`, `differential`, `scanner`). It becomes a sibling-hunt
  template for every future run.
- A **new false-positive class** confirmed in triage → add it to `data/cpython_non_bugs.md`. This is
  the feedback that keeps precision from decaying.

## Usage Examples

```
/cpython-review-toolkit:informed-explore
/cpython-review-toolkit:informed-explore Objects/ recursion pyerr-clear
/cpython-review-toolkit:informed-explore Modules/_io/ all deep parallel
/cpython-review-toolkit:informed-explore . all --catalog-dir ~/projects/cpython-review-findings
```

## When to use `explore` vs `informed-explore`

- **`explore`** — a fast first pass on an unfamiliar subsystem, or when you want raw scanner output
  without the catalog framing.
- **`informed-explore`** — a thorough audit, a re-review of code you've seen before (the findings repo
  makes it cumulative), or whenever fix-propagation (find *every* instance of a known shape) matters
  more than speed.
