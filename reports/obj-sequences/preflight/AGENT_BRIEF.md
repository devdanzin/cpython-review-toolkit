# Agent brief — slice `obj-sequences`

Read after `RUN_CONTEXT.md` and `informed_briefing.md`.

---

## 0. PHASE 1 RESULTS — these override §2 and §3 below

Both Phase 1 agents have run. **Read `agents/include-graph-mapper.md` and
`agents/git-history-context.md` before your own Phase 1.** The mapper's re-entrancy table is the
structural map for this slice. Three claims I made in §2/§3 are wrong; the original text is left
standing so the record shows what was written, but these corrections win.

**§2 shape 2 is wrong about the file.** I wrote that `list.sort`, `list.remove`, `list.index`,
`in` and `count` all hold a borrowed pointer or index across user code. **Four of the five do
not.** `list_sort_impl:2963-2973` *detaches* `ob_item`/`ob_size`/`allocated` before any user code
runs — the code's own comment calls mutation-during-sort "a core-dump factory". `index`, `count`
and `in` go through `list_get_item_ref`, which bounds-checks and returns a **strong** reference
every iteration. Only `list.remove` reads raw `ob_item[i]`, and it INCREFs first. **Do not hunt a
borrowed-`ob_item` UAF in `listobject.c`; it is not there.** The history agent reached the same
conclusion from the other direction: the `__eq__`-during-list-ops class was closed in 2019–2020.

**§2 shape 1 is disproven.** The memoryview-vs-resize analogy to CPY-0044 does not hold directly:
**9 of 9** Python-visible resize paths raise `BufferError` with a live memoryview, on release and
debug, and all six `_canresize` sites are present. What *does* transfer is `ob_exports` used as a
general **re-entrancy pin** — and the sites that fail to take that pin are where the bugs are.

**§3's `scan_lock_discipline` row is 57% of the truth.** "54 critical-section functions" is
functions, not regions (67 macro sites), and **41 more critical-section functions live in
`Objects/clinic/{listobject,bytearrayobject}.c.h`** and were never scanned — 95 total. Worse, **18
clinic wrappers run arbitrary user Python (`_PyNumber_Index`, `_PyEval_SliceIndex`,
`PyObject_GetBuffer`) *before* taking the lock and before the `.c` file is entered.** **You cannot
trust the `.c` file alone. Read the sibling `clinic/*.c.h`.** This is the same mistake I made on
mod-io, where 87 of 95 critical sections were clinic-emitted and I missed them.

### The live habitat, per Phase 1

Not `listobject.c`. It is **`bytearrayobject.c`**, and the shape is a **cached raw pointer into
`self` held across a buffer/coercion dunder** — `__buffer__`, `__release_buffer__`, `__index__`,
`__len__`. Every post-2022 instance of the re-entrancy class in this family is that.

### Already found and recorded — confirm siblings, do not re-derive

- **CPY-0180 — `bytearray.strip/lstrip/rstrip`, heap-use-after-free, reproduced by me with ASan.**
  `:2375` caches `myptr = PyByteArray_AS_STRING(self)`, `:2391` `PyBuffer_Release` runs the
  argument's PEP 688 `__release_buffer__`, `:2392` copies from the freed pointer.
  **It does not crash** — plain builds rc=0 — it *discloses*: `strip()` returns freed heap bytes
  (`b'H~\x00\x00\xb0\xf0\x99$H~\x00'`) instead of `b'hello world'`.
  The class has been fixed **twice** in this same file already — `220f0b10777` (gh-142560) and
  gh-143195, whose `ob_exports++` / `ob_exports--` bracket sits verbatim 280 lines away at
  `bytearray_hex_impl:2668-2676` **with the issue URL in the comment**. The strip helper survived
  both sweeps.
  **Your job on this: find the other unpinned sites.** Inventory every function in
  `bytearrayobject.c` and `bytesobject.c` that caches a raw `char*`/`Py_ssize_t` from `self` and
  then calls anything that can reach user Python, and check which take the `ob_exports` pin. The
  mapper reports 16 functions using the idiom; the useful number is how many *should*.
- **`bytearrayobject.c:938`** — a Python-reachable failing `assert`: SIGABRT on debug-gil and
  debug-ft, `BufferError` on both release builds.
- **`bytearray.__new__(bytearray).append(1)`** → SIGSEGV, confirming both `scan_init_bypass`
  findings.

### A constraint on any fix you propose

`list` has a **static `tp_version_tag`** and an inline bytecode implementation: `Python/bytecodes.c`
reimplements subscript, store-subscript, `FOR_ITER` and `UNPACK_SEQUENCE` with direct
`PyList_GET_ITEM` / `_PyList_ITEMS` access. **A guard added to `list_item` or `list_ass_item` does
not run** for `a[i]`, `a[i] = v`, or `for x in a` on the fast path. Say so if you propose one.

### Still open, and unmerged as of the review ref

**gh-153578 / PR #153579** — `bytearray.extend()` OOB write via a re-entrant `__buffer__`, a
`+3/-0` change to `bytearray_setslice`. It does not reach the strip helper. **PR #14771**,
"Align bytearray strip methods", has been open since 2019.

---

**This brief deliberately contains no structural map of the slice.** On the previous slice I
hand-wrote one instead of running the Phase 1 mapper, and three of its claims were wrong — one of
them sent ten agents hunting a lock-leak class that did not exist. The structural map for this
slice comes from `agents/include-graph-mapper.md` and `agents/git-history-context.md`, which run
first. If you are in Group A or later, **those two reports exist by the time you start: read them.**

---

## 1. Scope — hard boundary

Exactly four files, listed in `preflight/slice_files.txt`:

```
Objects/listobject.c
Objects/bytesobject.c
Objects/bytearrayobject.c
Objects/bytes_methods.c
```

`Objects/` is far wider than the slice. If your scanner's scope argument is the directory, filter
its output to these four before triaging. Cite anything else as context; review it never.

Three files are new territory; one carries prior catalog entries (see the briefing's folded-in
records). Weight your toolkit assessment toward **what you found by reading that the scanner
missed** — that is the output a cold scanner run cannot produce.

**No parity oracle here.** These types have no shipped pure-Python twin, so Group D2 is skipped by
`explore.md`'s own condition, and `_pyio`-style differential evidence is unavailable. Where the
previous slice could settle a question in one subprocess pair, this one cannot. Say so rather than
substituting a weaker argument.

## 2. What the slice notes point at

The campaign manifest flags two shapes for this slice:

1. **Resize-during-buffer-export** — the `_struct` CPY-0044 shape in another guise. A live
   `memoryview` over a `bytearray` whose buffer is then reallocated.
2. **User `__eq__` / `__lt__` re-entry during list operations** — `list.sort`, `list.remove`,
   `list.index`, `in`, `count`, and the rich-compare paths all call back into Python while
   holding a borrowed pointer or an index into `ob_item`.

Shape 2 is the same family as the previous slice's headline and as gh-154709: **read state, call
into user Python, keep using the state.** You already know what it looks like. `listobject.c` is
the densest habitat for it in the tree.

Neither is a hypothesis I have tested on this slice. Treat both as leads, not findings.

## 3. Scanner baseline — check the denominator before certifying anything clean

| scanner | slice | denominator | reading |
|---|---|---|---|
| `scan_deprecated_apis` | **10** | — | the largest pile here |
| `scan_ft_races` | **6** | 3 iternext fns, 0 local lock wrappers | real |
| `scan_error_paths` | **3** | 32 fallible assignments, 165 int-status callees | real |
| `scan_pyerr_clear` | **3** | 10 destructor fns | real |
| `scan_init_bypass` | **2** | 1 file with nullable fields, 2 total | narrow but real |
| `scan_recursion_guards` | **1** | **1** recursion-prone slot fn | 1 of 1 — triage it carefully |
| `scan_uninit_dealloc` | **1** | 5 allocation sites | real, and non-zero unlike mod-io |
| `scan_null_checks` | 0 | **1,309** assignment sites, 314 fallible sources | **strong real negative** |
| `scan_lock_discipline` | 0 | **54** critical-section fns, 0 mutex fns | **real negative** — 54 regions, no leaks. Contrast mod-io, where the same zero was structural |
| `scan_refcounts` | 0 | 37 borrowed-slot load sites | see below |
| `scan_memory_patterns` | 0 | 1 varobject site | thin |
| `scan_stw_safety` | 0 | **0** STW fns | structural — nothing to check |
| `scan_gil_usage` | 0 | **`vocabulary_resolved: 0`**, 16 tokens seen | **STRUCTURAL ZERO. Do not certify clean.** |

Two specific instructions from the previous slice:

- **`scan_refcounts`'s 37 is a headline, not a denominator.** On mod-io the equivalent 26 turned
  out to be 13 non-PyObject scalars, 8 module-state/static types and **5 genuine `PyObject*`
  borrows**. Type them before you quote the number, and quote the typed figure.
- **`scan_gil_usage` resolved none of its vocabulary.** Its zero is silence. Either these files
  genuinely contain no GIL constructs, or the constructs are spelled in a way the scanner cannot
  read. Establish which, and say so in one line.

## 4. Method — the lessons, each one paid for

1. **A verdict rests on a crash count or a debugger frame. An explanation does not.** On
   obj-mappings six causal stories failed while four verdicts held. Report what you measured —
   exit codes, N-of-M, the ASan or gdb frame. Mark any mechanism as a hypothesis and say what
   would falsify it.

2. **Test the right observable.** On mod-io I dropped a real finding because I compared two
   backends' final exception *type*, which matched, when the actual difference was that one
   backend ran the user's `__index__` and the other never called it. Ask what the defect would
   change, then measure *that*.

3. **Do not relay a sibling agent's conclusion as established.** Cite its evidence too, or label
   it unverified. Two agent claims on the previous slice did not survive checking: one reported a
   rule at "0/26 precision, 0/7 recall" when the raw output listed all seven crash sites at exact
   line numbers, and one described an envelope-merge bug that does not exist.

4. **Check the denominator before certifying a clean negative.** §3 does this for you.

5. **Debug builds both hide and manufacture bugs.** Run crash clusters on `debug-gil-nojit` **and**
   `release-gil-nojit` before claiming a severity.

6. **FT ASan has no shadow for the object heap** (mimalloc, `MI_TRACK_ASAN` undefined). Use a
   **GIL** ASan build for heap evidence; an FT ASan run reporting nothing has told you nothing.

7. **A revive-by-address acquisition is never a valid control.** Acquire through a live reference
   or not at all.

8. **Prior art before novelty.** `gh search issues` silently returns nothing here. Use
   `gh api -X GET search/issues -f q='repo:python/cpython <terms>'`. bpo → gh is **+44181**.

9. **Never write multi-line Python through a heredoc.** It mangles silently and prints success.
   Write a `.py` file into `repro/`.

10. **Ambient `python` is RustPython** — empty output, exit 0. Always name an interpreter:
    `~/projects/python_build_matrix/builds/<name>/python`.

11. **Diff before you cite.** Build matrix is at `a1d580430c8`, review target `4f3be1b5777`.
    State whether the file you cite is identical between them.

## 5. Output

Report to `reports/obj-sequences/agents/<your-agent-name>.md`; reproducers as `.py` files in
`reports/obj-sequences/repro/`. Three required sections:

- **Findings** — FIX / CONSIDER / POLICY / ACCEPTABLE, each with `file:line`, the **guarded twin**
  where one exists, what you measured, and what you did not.
- **Classes bounded** — what you checked and found clean, *with the denominator that makes the
  negative meaningful*.
- **Toolkit assessment** — precision of each rule that fired, recall gaps found by reading, and a
  concrete tuning proposal. Do not edit the toolkit yourself; propose, and I will implement and
  test.
