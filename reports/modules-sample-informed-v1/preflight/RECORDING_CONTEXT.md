# Recording context — Modules/ findings → cpython-review-findings

## Tracker search: use the REST API, NOT `gh search`

`gh search issues` **silently returns nothing** on this box — it reported 0 hits for terms that
do have matches. Every prior-art claim must come from the API:

```bash
gh api -X GET search/issues -f q='repo:python/cpython <terms>' -f per_page=10 \
  --jq '.total_count as $t | "total=\($t)", (.items[]? | "  #\(.number) [\(.state)] \(.title[0:80])")'

gh api repos/python/cpython/issues/<N> --jq '"[\(.state)] \(.title)\nclosed \(.closed_at)\n\n\(.body[0:1200])"'
gh api repos/python/cpython/issues/<N>/timeline --paginate \
  --jq '.[] | select(.event=="closed" or .event=="cross-referenced") | "\(.event) \(.commit_id[0:11] // "") \(.source.issue.title[0:70] // "")"'

# Did upstream already fix it AFTER our stale clone?
gh api -X GET repos/python/cpython/commits -f path=Modules/<file>.c -f since=2026-07-15T00:00:00Z \
  -f per_page=10 --jq '.[] | "  \(.sha[0:11]) \(.commit.author.date[0:10]) \(.commit.message|split("\n")[0][0:80])"'
```

**Our clone is stale** — HEAD `4f3be1b5777` (2026-07-15); matrix builds `a1d580430c8` (2026-07-18).
Upstream has moved. **Always check for a post-HEAD fix before calling something live**, and record
what you checked. Diff any file you cite between the two commits and state whether it is identical.

## Prior art already established — do not re-derive

- **`gh-154013`** *(closed 2026-07-19)* — "free threading: struct iter_unpack is not memory safe".
  Closed with only a **test** added (gh-154141); no fix commit in the timeline.
- **`gh-146020`** *(closed 2026-03-16)* — "struct.Struct crashes (seg fault) under concurrent
  re-initialization and unpack in free-threading builds". Identifies the root cause exactly
  (`__init__` on an initialized Struct frees and replaces `s_codes`) but frames it **entirely as a
  locking problem**, and its reproducer **`sys.exit()`s unless `--disable-gil`**.
- **`gh-145743`** / PRs gh-145744, gh-145763, gh-145764 — "inconsistency after calling
  `Struct.__init__()` with invalid format".
- **`gh-149816`** *(open)* — "22 free-threading race conditions", the umbrella.

**Why our `_struct` re-init finding is still novel:** it is **single-threaded, on the default GIL
build**, needs no threads and no FT build, and the mechanism is different — a live `iter_unpack`
iterator holds a buffer length captured at creation while re-reading `s_size` each step. The prior
issues are all about concurrent access to `s_codes`. Say this explicitly in the record and cite
gh-146020 as the closest prior art rather than claiming a clean discovery.

Note `_struct.c:1988-1990` already emits
`FutureWarning: Re-initialization of Struct by calling the __init__() method will not work in
future Python versions` — it **warns and then continues into the out-of-bounds read**. So the
behavioural hazard is known and slated for removal; the memory-safety consequence is unguarded in
the interim. That is the right framing for a report, and it makes the fix cheap to argue.

## Scope of the disclosure (measured, use this wording)

Same-process only; it cannot read another process's memory — the read stays inside the interpreter's
own address space. But it **does** reach unrelated same-process allocations, not just adjacent
padding: planting `0xDEADBEEF` across 400 separate `bytearray`s and running the over-read 200 times
recovered the marker **200/200**. Impact is real where untrusted Python shares a process with
secrets (sandboxes, multi-tenant eval services) and for ASLR-defeating pointer leaks. It is not a
privilege-boundary break on its own. Do not inflate it beyond this.

## Repo conventions

Read `/home/danzin/projects/cpython-review-findings/CLAUDE.md` and use
`reports/CPY-0001-tuple-hash-recursion/` as the template (`meta.json` + `report.md` + `repro.py` +
`evidence.txt`). `meta.json` is the only source of truth; `INDEX.md` and `catalog/known_bugs.tsv`
are generated.

- `status`: `reproduced` **only** where you personally observed it; else `static-confirmed` or `lead`.
- `found_date`: 2026-07-25. `found_by`: the agent that surfaced it.
- Record the **guarded twin** for every finding — it is both the confirmation and the fix.
- Never fabricate a transcript. A well-evidenced negative is a real result.
- **Do NOT `git commit`** and do **NOT** run `scripts/gen_index.py` — several agents write
  concurrently; the orchestrator commits and regenerates once at the end.
- Existing records run to **CPY-0033**. Use only the ID range you were assigned.

## Builds

`/home/danzin/projects/python_build_matrix/builds/<name>/python` — `debug-gil-nojit`,
`debug-gil-nojit-asan`, `debug-ft-nojit`, `debug-ft-nojit-asan`, `debug-ft-nojit-tsan`, `release-*`.
FT builds need `PYTHON_GIL=0`. The in-tree `./python` has a stale `_sre` (breaks `re`/`xml`) — avoid.
Run crash candidates in a subprocess and report the real exit code (139 SIGSEGV, 134 SIGABRT).
