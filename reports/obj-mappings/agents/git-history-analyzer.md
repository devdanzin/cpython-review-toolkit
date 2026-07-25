# Git History Analyzer (final-phase temporal cross-reference) — slice `obj-mappings`

Agent: `git-history-analyzer` · Target: `/home/danzin/projects/cpython` @ `4f3be1b5777` (2026-07-15)
Scope for findings: `Objects/dictobject.c` + `Objects/setobject.c`. Tree-wide for propagation targets.
Runs LAST, after 17 agent reports + `FINDINGS_LEDGER.md`.

**Clone health re-verified independently:** `git rev-parse --is-shallow-repository` → `false`;
`git rev-list --count HEAD` → **132,320**. No `notes[]` warning, no commit cap, no timeout.

---

## 1. Denominator

| quantity | value |
|---|---|
| Finding sites blamed with `--introduced-by` | **29** (covering all 16 findings; multi-site findings blamed per site) |
| Enclosing functions given a full `git log -L:func:file` history | **19** (one regex miss: `_PyObject_SetManagedDict`) |
| Reverse-iterator implementations swept tree-wide | **16 sites / 9 distinct iternext bodies** |
| Watcher/callback dispatchers swept tree-wide | **30 examined, 13 matched, 62 callers, 27 hold state** |
| Stale-index candidates swept in `odictobject.c` / `listobject.c` | **18 / 22 functions** |
| Revert / re-land pairs enumerated in the two slice files | **14** (preflight found 9; this pass adds 5) |
| GitHub PRs / issues fetched for review-record archaeology | **11** |

Per the brief's lesson 2, every zero below carries its denominator. Per lesson 4, I mark each
claim as **verified-by-me** (I read the code or the commit myself) or **swept** (a sub-sweep
produced it and I did not independently re-read every site). The centerpiece archaeology in §2
is entirely verified-by-me from the GitHub review record.

**Toolkit defect D-22 honoured:** `function_churn` was not used for any density claim. All
per-function counts below come from `git log -L:funcname:file`, i.e. hunk-level attribution.

---

## 2. THE HEADLINE — P3-F14's upper bound was **written, reviewed, and deliberately removed**

The brief asked whether bpo-38555 "discussed, deliberately omitted, or simply overlooked" the
upper bound, because that framing decides how the upstream issue gets written. The answer is
sharper than any of the three options, and it is not in bpo-38555 at all.

### 2.1 First correction: the ledger conflates two different 2019 issues

`FINDINGS_LEDGER.md` records the nearest prior art as *"gh-82706 … is bpo-38555 (2019)"*.
**That is wrong, and the correction matters.** From `gh api repos/python/cpython/issues/82706`:

```
number = 82706, title = 'Strange reversed dict behavior'
BPO     = 38525          <-- not 38555
type    = 'crash',  priority = 'critical',  resolution = 'fixed'
closed  = 2019-10-19,  closer = 'pablogsal'
PRs     = 16846, 16847, 16853
```

There were **two** reverse-iterator bound fixes in October 2019, four days apart:

| # | date | commit | issue | what it fixed | review comments |
|---|---|---|---|---|---|
| 1 | 2019-10-19 | `24dc2f8c566` | **bpo-38525** (GH-16846), `type-crash`, priority critical | segfault in `reversed({})` — corrected the **seed** | **13** |
| 2 | 2019-10-23 | `2e3d873d3bd0` | bpo-38555 (GH-16883) | pointer-formation UB — hoisted the **lower** bound | **0** |

bpo-38555 is a one-line-message, zero-review-comment, no-test, no-NEWS commit by Serhiy
Storchaka; it moved `if (i < 0) goto fail;` above the `&DK_ENTRIES(k)[i]` expression so the
out-of-range pointer is never *formed*. It never mentions an upper bound. On its own it would
read as "simply overlooked".

**The real story is in bpo-38525's review thread.**

### 2.2 The upper bound was proposed, questioned, and deleted — on reviewer request

PR 16846 has 8 branch commits. The seventh, `b86f723acd1d`, still carried an upper bound. The
**eighth and final** commit, `bf61754fdf62` ("bpo-38525: Update"), does exactly one thing
(fetched via `gh api repos/python/cpython/commits/bf61754fdf62`):

```diff
@@ -3831,7 +3831,7 @@ dictreviter_iternext(dictiterobject *di)
     if (d->ma_values) {
-        if (i < 0 || i >= d->ma_used) {
+        if (i < 0) {
             goto fail;
         }
```

The review comment that caused it, at `Objects/dictobject.c:3834`:

> **serhiy-storchaka:** "Is this change still needed?"
> **corona10:** "No, it is not needed. I removed it on the latest commit."

The `diff_hunk` attached to that review comment names the exact line
`- if (i < 0) { + if (i < 0 || i >= d->ma_used) {`. This is not inference — it is the review
record.

**Verdict for the upstream issue: neither overlooked nor deliberately omitted on the merits.
The bound was authored, then removed as redundant during review of a crash fix.**

### 2.3 Why "redundant" was defensible then and is false now — and the guarded twin proves it

The removal was reasoned about *the branch it was on*. In the same PR, commit `8618a7a582fd`
had just changed the split-table seed to `di_pos = ma_used - 1`. With that seed plus the
`di_used != ma_used` staleness check, `i >= ma_used` genuinely is redundant **on the
`ma_values` branch**. The reviewer was right about the code in front of him.

Two things falsify it today, and I verified both by reading the current file:

1. **The combined-table branch was never covered by the proposed bound and still is not.** It
   is seeded from `load_keys_nentries(dict) - 1` (`dictobject.c:5636`) — `dk_nentries`, which
   `ma_used` does not track. That is the branch P3-F14 segfaults on, at `:6283`.

2. **The three forward twins still carry the literal check that was deleted from the reverse
   one.** Verified by direct read at `4f3be1b5777`:

   ```c
   /* dictobject.c:5732, :5855, :5978 — all three forward iterators */
       if (_PyDict_HasSplitTable(d)) {
           if (i >= d->ma_used)
               goto fail;
           int index = get_index_from_order(d, i);
   ```

   ```c
   /* dictobject.c:6275 — the reverse iterator, same branch, no bound */
       if (_PyDict_HasSplitTable(d)) {
           int index = get_index_from_order(d, i);
   ```

   The guarded twin is not a distant analogue. It is the *same three lines*, and the reverse
   iterator is the only one of four that lost them.

3. **The split branch's last remaining bound is a debug-only `assert`.** `get_index_from_order`
   (`dictobject.c:672-677`) contains `assert(i < mp->ma_values->size);` and nothing else. On a
   release build the 2019 removal is therefore load-bearing on the split branch too — this is
   the CPY-0079 "assert-as-bound" shape, and it is *also* P3-F11's site.

**Recommended framing for the upstream issue.** Do not write it as "nobody thought of the
bound". Write it as: *the bound existed, was removed during review of GH-16846 as redundant
given the split-table seed fix in the same PR, and that redundancy argument never applied to
the combined-table branch — which is the branch that crashes, and which the three forward
iterators still guard.* Cite `bf61754fdf62` and the `serhiy-storchaka` review comment. This
converts the report from "you missed a check" into "here is where the check went", which is
both accurate and much harder to dismiss.

### 2.4 One more temporal fact for the write-up

`dictreviter_iter_lock_held` has **15 commits total, 14 since the 2018 introduction**, and
exactly **one** was fix-shaped: `2e3d873d3bd0`, the 2019 UB fix. A fix commit passed through
this function once in seven years, four days after the bound was deleted, and restructured the
very `if (i < 0)` test — without restoring the deleted sibling check.

---

## 3. Fix-completeness on each finding's blame origin

Method: `analyze_history.py --introduced-by` on 29 sites (per the toolkit's own primitive),
then `git log -L:funcname:file` for the enclosing function, then the upstream issue's own
label via `gh api` to decide "was this commit fixing a bug" objectively rather than by
subject-line heuristic.

| finding | site | introduced by | date | origin class (tracker label) |
|---|---|---|---|---|
| **P3-F14** | `dictobject.c:6272` (the only bound) | `2e3d873d3bd0` bpo-38555 | 2019-10-23 | **bug fix**; and the *removal* by `24dc2f8c566` bpo-38525 `type-crash` |
| **P3-F5** | `setobject.c:2658` narrowing | `b02c35e2081` SF #1257731 | 2005-08-12 | **bug fix** |
| **P3-F6** | `dictobject.c:7932` assert | `bf542f8bb9f1` gh-124470 | 2024-11-21 | **bug fix** (`type-bug`) |
| **P3-F8** | `dictobject.c:2103` notify | `5592399313c9` gh-122208 | 2024-07-24 | **bug fix** (`type-bug`) |
| P3-F15 | `dictobject.c:2030`, `:4861` | `d5df25268b03` gh-112075 | 2024-04-25 | FT migration (`type-feature`) |
| P3-F8a | `dictobject.c:1910` `dk_usable` | `176df09adbb4` gh-112075 | 2024-02-20 | FT migration (`type-feature`) |
| P3-F13a | `dictobject.c:7976` | `8b541c017ea9` gh-112075 | 2024-04-21 | FT migration (`type-feature`) |
| P3-F13b | `dictobject.c:8019` | `6c450f44c283` gh-130313 | 2025-02-20 | FT perf (`type-feature`) |
| P3-F7 | `dictobject.c:6145` FT arm | `1002fbe12e0b` gh-112075 | 2024-02-22 | FT migration (`type-feature`) |
| P3-F10 | `dictobject.c:6347` latch | `92abb0124037` gh-112075 | 2024-02-06 | FT migration (`type-feature`) |
| P3-F9 | `dictobject.c:8314` | `a4b779488792` GH-91052 | 2022-10-06 | feature (watcher C API) |
| P3-F16a | `dictobject.c:5051` | `a4b779488792` GH-91052 | 2022-10-06 | feature |
| P3-F16b | `dictobject.c:2963` assert | `778928b0c7aa` bpo-29304 | 2017-08-03 | refactor |
| P3-F16c | `dictobject.c:2943` | `a8b9350964f4` bpo-45340 | 2021-10-13 | feature |
| P3-F11 | `dictobject.c:676` | `a7252f88d3fa` bpo-40116 | 2021-10-06 | feature |
| P3-F12 | `dictobject.c:5682` | `0ce6dc853000` | 2004-03-18 | feature |
| P3-F1 | `setobject.c:290/294` | `8651a504752f` → ordering baked in by `2cc9b8486dd9` | 2015 / 2020 | refactor |
| P3-F4 | `setobject.c:319` | `15f08696096a` | 2015-07-03 | refactor |
| P3-F3 | `setobject.c:2916/3008` | `a690a9967e71` | 2003-11-16 | initial import |
| P3-F2 | `Python/critical_section.c:50` | `0fdf6a9a71f` gh-144513 | 2026-02-06 | feature (`type-feature`) |

### 3.1 The four fix-introduced findings, each stated as an incomplete/self-defeating fix

#### [FIX] P3-F14 — a crash fix removed the bound; a second fix restructured past it
**What was fixed:** `24dc2f8c566` (bpo-38525) fixed a critical segfault in `reversed({})` by
correcting the seed. `2e3d873d3bd0` (bpo-38555) then fixed pointer-formation UB.
**What was missed:** the first fix *deleted* `i >= d->ma_used` (commit `bf61754fdf62`) on
reviewer request; the second fix rewrote the surrounding bound test four days later and did not
notice the asymmetry with the three forward twins.
**Evidence:** §2.2 review record; `dictobject.c:5732/5855/5978` vs `:6275`, verified by read.

#### [FIX] P3-F5 — the fix that created the over-broad clear
**What was fixed:** `b02c35e2081` (2005-08-12) — *"Fix SF #1257731. Make `__contains__()`,
`remove()`, and `discard()` only do a frozenset conversion when the initial search attempt
fails with a TypeError and the key is some type of set."*
**What was missed:** the narrowing it introduced (`PyErr_ExceptionMatches(PyExc_TypeError)`)
assumes the only `TypeError` reaching that point is "unhashable set". `set_discard_key` has two
failure modes — `PyObject_Hash` *and* `PyObject_RichCompareBool` against an existing element —
so a user `__eq__`'s `TypeError` is cleared too. **The narrowing that was the fix is the bug.**
**Corroborating temporal fact:** `set_remove_impl` has **18 commits, 10 since 2005, zero
fix-shaped**; `set_discard_impl` has 20 / 11 / one (2011, an unrelated return-value fix). This
code has not been re-examined in 21 years.
**Guarded twin (from the ledger, confirmed as a real twin):** `_PySet_Contains:2559` and
`frozenset___contains___impl:2619` hoist the hash themselves. Note `c98182be8d4` (2025-12-13)
touched the narrowing in the *contains* path during the lock-free rewrite and left
`remove`/`discard` alone — a modern fix commit passing through the family without noticing.

#### [FIX] P3-F6 — a crash fix introduced an assert that aborts
**What was fixed:** `bf542f8bb9f1`, gh-124470 (`type-bug`) — *"Fix crash when reading from
object instance dictionary while replacing it"*, 7 files.
**What was missed:** the fix added `assert(new_dict == NULL)` at `:7932` asserting a
relationship the code never establishes. Debug **SIGABRT 10/10** per the ledger. A crash fix
introduced a new abort, 2 years 8 months ago, unrevisited.

#### [FIX] P3-F8 — a notify-ordering fix moved the notify into a *different* hazard
**What was fixed:** `5592399313c9`, gh-122208 (`type-bug`) — *"Dictionary watchers deliver
`added` event before it's guaranteed to be successful leading to possible inconsistent state"*.
The fix moved `_PyDict_NotifyEvent(PyDict_EVENT_ADDED, …)` to a point where the insert can no
longer fail.
**What was missed:** it relocated the notify to sit **between a state decision and its write**
(`insert_to_emptydict:2103`), which is precisely P3-F9's window. The fix correctly removed the
"event fires then insert fails" hazard and installed the "event runs Python then we write stale
state" hazard. This is the strongest single argument that P3-F9 is a *class* defect rather than
one site: the maintainers have already moved this notify once for correctness reasons and the
new position is worse in a different dimension.

### 3.2 The dominant origin class is **not** fix-adjacency — it is the FT migration

Six of sixteen findings sit on code introduced by a single epic, **gh-112075 "Make `dict`
objects thread-safe in `--disable-gil` builds"** (`type-feature`), plus its follow-on
gh-130313: P3-F7, P3-F8a, P3-F10, P3-F13a, P3-F13b, P3-F15 (+ P3-F15b). Five distinct commits,
all by the same author, 2024-02 to 2025-02.

The script's subject-line classifier called two of these (`176df09adbb4`, `8b541c017ea9`)
`fix/high/data-race`. The tracker calls the parent issue `type-feature`. **The heuristic
over-calls FT-migration commits as fixes** — see §8.

---

## 4. Propagation beyond the slice — the highest-value output

### 4.1 Shape A: `PyErr_FormatUnraisable` runs Python on the callback's behalf (P3-F9)

Swept: **30 dispatchers examined, 13 match the shape, 62 callers, 27 hold state across the
dispatch.** Every PEP 669-family watcher was enumerated.

**Bounded-negative + positive result, per family:**

| family | dispatcher | unraisable | callers holding state |
|---|---|---|---|
| dict (reference) | `dictobject.c:8309` `_PyDict_SendEvent` | `:8314` | 10 / 14 — the slice finding |
| **type** | `typeobject.c:1222` `_PyType_Modified_Unlocked` | `:1223` | **14 / 21** (6 strong) |
| **code** | `codeobject.c:54` `notify_code_watchers` | `:55` | 0 / 2 (both resurrect-bracketed or terminal) |
| **function** | `funcobject.c:41` `notify_func_watchers` | `:42` | 6 / 9 (1 strong: `func_set_code:687`) |
| **context** | `context.c:133` `notify_context_watchers` | `:134` | 0 / 2 — clean |
| weakref | `weakrefobject.c:1000`; GC copy `gc.c:923` | both | 2 / 2 (mitigated by pre-collected tuple) |
| GC callbacks | `gc.c:1298`, `gc_free_threading.c:1972` | both | 4 / 4 |
| `__del__` | `typeobject.c:11232` `slot_tp_finalize` | | 3 / 4 |
| at-fork | `posixmodule.c:667/678` `run_at_forkers` | | 2 / 3 (incl. the pre-STW window) |
| ctypes | `_ctypes/callbacks.c:203` | | 1 / 1 (raw libffi result buffer) |
| signals | `signalmodule.c:1853` | | self (raw `_PyInterpreterFrame *`) |
| module clear | `moduleobject.c:1074/1096` | | self (`PyDict_Next` index) |
| atexit | `atexitmodule.c:133` | | **0 / 1 — clean** (private list snapshot) |

**Verified-by-me sub-finding, and a genuine in-slice result:** the type-watcher family's
strongest caller is **`Objects/dictobject.c:1971` `insert_split_key` — inside my slice**. I read
`dictobject.c:1962-1980` directly:

```c
    LOCK_KEYS(keys);
    ix = unicodekeys_lookup_unicode(keys, key, hash);
    if (ix == DKIX_EMPTY && keys->dk_usable > 0) {          /* :1964  capacity precondition */
        ...
        if (type) {
            _PyType_Modified_Unlocked(type);                /* :1971  -> unraisable -> Python */
        }
        Py_ssize_t hashpos = find_empty_slot(keys, hash);   /* :1973  uses the stale precondition */
        ix = keys->dk_nentries;
        dictkeys_set_index(keys, hashpos, ix);
        PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(keys)[ix];
        STORE_SHARED_KEY(ep->me_key, Py_NewRef(key));       /* :1977 */
```

This means **CPY-0096 / P3-F15 is reachable through a registered *type* watcher, with no user
`__del__`, no user `__eq__`, and no `sys.settrace`** — the same doc-conforming-callback route
P3-F9 documents for dict, arriving at a different dispatcher. Worse: `Doc/c-api/type.rst:138-155`
documents `PyType_WatchCallback` **without stating the error protocol at all** — it never says
"return -1 if you set an exception" — yet `typeobject.c:1222` treats `< 0` as exactly that and
reports it via `PyErr_FormatUnraisable`. The dict contract at least documents the obligation it
then discharges wrongly; the type contract does not document it and discharges it identically.

**CPython already knows.** `typeobject.c:1219-1220`, added by `fbbbc10055e` (2025-05-27,
gh-127266 — itself a **re-land** after `eecafc33800` reverted `e414a2d81c3`):

```c
                // Note that PyErr_FormatUnraisable is potentially re-entrant
                // and the watcher callback might be too.
```

That comment is upstream's own acknowledgment of P3-F9's premise, in a different file, 14
months before the target ref. `dictobject.c` has no equivalent. **Cite this comment in the
upstream issue** — it converts P3-F9 from a novel claim into "you documented this hazard in
typeobject.c and dictobject.c's ten notify sites were never audited against it."

**Second, cleanly bounded sub-finding — a NULL-callback call in 3 of 5 watcher families.**
Verified by direct read of all five dispatchers and all four `*_ClearWatcher` functions:

| family | dispatch guard | `ClearWatcher` behaviour |
|---|---|---|
| dict `dictobject.c:8309` | `if (cb && (cb(...) < 0))` — **guarded** | NULLs slot, leaves bit set |
| type `typeobject.c:1222` | `if (cb && (cb(type) < 0))` — **guarded** | — |
| code `codeobject.c:52-54` | `assert(cb != NULL); if (cb(...) < 0)` — **unguarded** | NULLs slot **and** clears bit (`:105-106`) |
| func `funcobject.c:39-41` | `assert(cb != NULL); if (cb(...) < 0)` — **unguarded** | NULLs slot **and** clears bit (`:111-112`) |
| context `context.c:131-133` | `assert(cb != NULL); if (cb(...) < 0)` — **unguarded** | NULLs slot **and** clears bit (`:177-178`) |

All five snapshot `bits` into a local *before* the loop. The three unguarded ones carry the
comment `// callback must be non-null if the watcher bit is set` — an invariant that a
re-entrant `Py*_ClearWatcher` breaks, reachable either from an earlier watcher in the same loop
(pure C, no Python needed) or from `sys.unraisablehook` fired by an earlier watcher. In a
release build the `assert` is compiled out and `cb(...)` is a NULL call.
**This is not a failed fix propagation** — I checked the births: dict `a4b779488792` (2022-10-06)
and type `82ccbf69a84` (2022-10-21) were guarded from birth; code `3c137dc613c` (2022-12-02),
func `3db0a21f731` (2022-11-22) and context `d87482bc4ee` (2024-09-23) copied the weaker
template. Three later families inherited from the wrong sibling.
**Classification: CONSIDER, cross-slice** (`Objects/codeobject.c`, `Objects/funcobject.c`,
`Python/context.c` — none is in this slice). One-line fix each.

**Sweep completeness statement:** of 5 PEP 669-family watcher dispatchers, **2 guard the
callback pointer, 3 do not**. Of 30 callback dispatchers tree-wide, **1 (`atexit`) is
structurally immune** by snapshotting into a private list — that is the fix template for the
family.

### 4.2 Shape B: stale index / raw pointer across a Python-running call (P3-F15/F16)

Swept `odictobject.c` (18 functions) and `listobject.c` (22 functions/paths).

**`Objects/listobject.c`: 0 stale out of 22 — a clean bounded negative.** This is the more
valuable half of the result. list defends with three consistently applied idioms:
detach-then-mutate (`list_clear_impl:882-885`, `list_sort_impl:2971-2973`), defer-all-DECREFs
(`list_ass_slice_lock_held:1029-1030`), and re-read-`ob_item`-and-`Py_SIZE`-after-any-callout
(`:1016`, `:1022`, `:3490`, `:3868`), plus the bounds-checking `list_get_item_ref` accessor used
by all three comparison-driven scans. `list_remove_impl:3409` is stale-by-index but the callee
clamps, so it is a correctness bug, not memory unsafety.

**`Objects/odictobject.c`: 8 stale out of 18.** Two roots:
`_odict_get_index_raw:540` caches `PyDictKeysObject *keys` across `_Py_dict_lookup` and returns
`keys->dk_nentries` at `:549`; `_odict_get_index:607-612` establishes the table-sync
precondition *before* the Python-running lookup at `:614`. **Verified-by-me** by reading
`odictobject.c:536-552` and `:561-590`. The worst consequence is `_odict_resize:585`
(`fast_nodes[i] = node`) — an OOB **write** of a possibly-freed `_ODictNode *`.

**This is the fix-completeness result of the sweep.** `odictobject.c` has been fixed for
*exactly this shape* **twice**, in two single-function commits, and neither propagated:

| # | date | commit | function fixed | scope |
|---|---|---|---|---|
| 1 | 2024-09-24 | `38a887dc3ec`, gh-119004 (GH-121329) | `_odict_keys_equal` (`__eq__`) | 1 C file, +33/−11; backported to 3.12/3.13 |
| 2 | **2026-06-28** | `7d128e319f3`, gh-148660 | `OrderedDict_copy_impl` | 1 C file, +36/−10, **10 hunks all in one function** |

`7d128e319f3`'s own commit message says it all:

> "Detect this the same way `OrderedDict.__eq__` already does (**gh-119004**): snapshot
> `od_state` before the loop … and raise RuntimeError if the state changed before advancing."

The author recognised the shape had a guarded twin, applied the twin's idiom to one more
function, and stopped. **Landed 18 days before the target ref, and backported to 3.13, 3.14 and
3.15.** Eight sites in the same file still lack the `od_state` snapshot.
**Classification: FIX, cross-slice — hand to the `obj-odict` slice owner as a pre-seeded
sibling hunt with the in-file fix template already identified (`:824-825/:847-851` and
`:1256/:1274-1275`).**

**Adjacent bounded negatives from the same sweep:** `_collectionsmodule.c` deque scans
(`deque_count_impl`, `deque_contains_lock_held`, `deque_index_impl`, `deque_remove_impl`) are
**4/4 guarded** by a `deque->state` snapshot re-checked before the block-pointer advance.
`deque_remove_impl:1477-1481` is the guarded twin of `list_remove_impl:3415`, which has no such
check — worth one line to the list owner as a correctness (not safety) gap.

### 4.3 Shape C: reverse iterator lacks the bound its forward twin has (P3-F14)

Swept: **every `__reversed__` in C tree-wide** (`grep -rn "__reversed__" Objects Modules Python
Include` resolves to exactly nine source files). **16 sites, 9 distinct iternext bodies.**

**Result: 1 unguarded body, 4 guarded, 4 structurally N/A. The single unguarded body is
`dictreviter_iter_lock_held` — the reference. The shape does not occur anywhere else in
CPython.**

| body | guard | verdict |
|---|---|---|
| `dictobject.c` `dictreviter_iter_lock_held` (4 entry points: `dict`, `.keys()`, `.items()`, `.values()`) | `i < 0` only | **UNGUARDED** — P3-F14 |
| `listobject.c:4220` `listreviter_next` | `list_get_item_ref` re-reads current `Py_SIZE` every step | GUARDED |
| `enumobject.c:440` `reversed_next` | delegates to `PySequence_GetItem` against current size | GUARDED |
| `odictobject.c:1740` `odictiter_nextkey_lock_held` (4 entry points) | current `od_state` **and** current `PyODict_SIZE`, key re-looked-up | GUARDED |
| `_collectionsmodule.c:2120` `dequereviter_next_lock_held` | `deque->state` bumped on every structural mutation | GUARDED |
| `rangeobject.c` `rangeiter_next` / `longrangeiter_next` | holds no container | N/A |
| `frameobject.c:862` FrameLocalsProxy | eager snapshot list | N/A |
| `descrobject.c:1159` mappingproxy / `weakrefobject.c:792` weakproxy | pure delegation | N/A (a `mappingproxy(dict)` inherits P3-F14) |

**Negative sweep confirmed:** `tupleobject.c`, `bytesobject.c`, `bytearrayobject.c`,
`unicodeobject.c`, `memoryobject.c`, `Modules/arraymodule.c` and `setobject.c` define no
`__reversed__` at all; all route through the guarded `reversed_next`.

**This bounded negative is the single most useful thing to put in the upstream issue.** It
converts P3-F14 from "a bug" into "**the only reverse iterator in CPython without a current-size
bound, out of nine**", and it removes the maintainer's most likely reflex ("is this a whole
class we'd have to sweep?"). It is not a class. It is one function, and the fix is the three
lines its own forward twins already contain.

---

## 5. Revert / re-land archaeology — completing the preflight

The preflight catalogued 9 revert-related commits (§2a-2e). A broader search
(`--grep='revert'` case-insensitive, plus `'This reverts commit'` bodies, plus `take N` /
`re-land`, excluding svn-merge megacommits) yields **14**. Five are new here.

### 5.1 The complete list, with the recurring hazard

| # | pair | recurring hazard | live? |
|---|---|---|---|
| 1 | `efb2fffae1f` → `bef57062226` → **`9626ef87f42` revert** → **`20921922fa4` re-land (take 2)**, 2026-06-11 … 07-06 | `LOCK_KEYS` discipline: deadlock, then data race, on `insert_split_key` | **YES — 9 days old at ref** |
| 2 | `3dd2157` (2020 removal) → `2cc9b8486dd9` (2020 restructure) → **`72789592a34` revert (2021)** → `b8e925b4f8f6` crash fix (2026-01) | restoring removed code into a body restructured underneath it; freeslot staleness | **YES — P3-F1/P3-F4 live here** |
| 3 | `c9782a9ac031` → **`9632a7d7350` revert (2015)** "pending a stronger test for mutation during iteration" | raw `setentry *` cursor held across a resize; the stronger test was never written, revert is permanent | latent |
| 4 | `b0fcc2c47a3` → **`899fdb213db` revert** (1 day) → `023b7d21414` re-land → **`690fe077f6b` revert** ("introduced a refleak"), 2024-11/12 | dict GC-tracking invariants; never re-landed | closed |
| 5 | **`e414a2d81c3` → `eecafc33800` revert → `fbbbc10055e` re-land**, 2025-04/05, gh-127266 | type-slot update data races — **NEW, not in preflight**; the re-land is what added the `PyErr_FormatUnraisable is potentially re-entrant` comment | live |
| 6 | `843d28f59d2` → **`d3c82b9cced` revert**, 2024-10-14/15, gh-125512 | context watcher enter/exit → "switched" events — **NEW**; touches the `notify_context_watchers` family in §4.1 | closed |
| 7 | `e82cf8675ba` (2017-04-01, bpo-29949 "Fix set memory usage regression") | carries `This reverts commit` in its body — **NEW**, a revert disguised as a fix subject; touches `set_add_entry` | closed |
| 8 | `f9bd05e83e3` (2020-06-23, bpo-40521) | `This reverts commit` in body; empty-frozenset singleton — **NEW** | closed |
| 9 | `05351c1bd8b` (2017-09-05, revert of bpo-30860 runtime globals) | **NEW** | closed |
| 10-14 | `ad4857884b4` (2021), `f8d1a31e706` (2015), `7aa53411647` (2015), `4f2dab5c337` (2011), `e0e89f79201` (2006), `bb999b5925a` (2005 "Restore GC support to set objects") | preflight §2e + 2 new; none bear on live code | closed |

### 5.2 Which of THIS run's findings sit on re-landed or reverted code — **five do**

1. **P3-F15 — directly downstream of 9-day-old re-landed code.** `20921922fa4`'s hunks on
   `dictobject.c` land at `:1954-1985` (`insert_split_key`), `:823-900` (`new_keys_object`,
   `free_keys_object`) and `:7261-7305` (`_PyDict_NewKeysForClass`). P3-F15's crash site is
   `_PyDict_InsertSplitValue:1995`, immediately below, and the Python-running call that makes it
   stale (`_PyType_Modified_Unlocked:1971`) is **inside the re-landed hunk**. Verified by reading
   `git show 20921922fa4 --unified=0` hunk headers against `dictobject.c:1950-2035`.
   Per the preflight's rule 2 ("a revert is not a return to a known-good state"), this is the
   finding with the least soak time in the slice and the one whose upstream report should lead
   with the re-land.
2. **P3-F1 and P3-F4** live in `set_add_entry_takeref`, whose `freeslot` machinery is the
   2021 re-graft (pair #2). `set_add_entry_takeref` has **59 commits, 27 since 2015, 6
   fix-shaped** — the most fix-traversed function in either file.
3. **P3-F8b (`insert_to_emptydict`) and P3-F15 (`insertdict`)** were both touched by the
   2024-11-19 GC revert `899fdb213db` (pair #4).
4. **P3-F9's cross-file twin** (`typeobject.c:1222`) sits on re-landed code from pair #5 — and
   the re-land is what documented the hazard.

**New rule for the campaign, from pair #5:** a re-land is not only a suspicion signal, it is
sometimes the *only* place a hazard is written down. `fbbbc10055e` is where CPython states that
`PyErr_FormatUnraisable` is re-entrant. Searching re-land diffs for newly-added comments is a
cheap way to find upstream's own threat model.

---

## 6. Temporal risk annotation per finding

Ages are to the target ref (2026-07-15). "Touched since" and "fix passed" are
`git log -L:funcname:file`, deduplicated by SHA (D-22-compliant).

| finding | age of defect | fn touched since intro | fix-shaped commits that passed | temporal risk |
|---|---|---|---|---|
| **P3-F14** | **6y 9m** (bound deleted 2019-10-19) | 14 | **1** — `2e3d873d3bd0`, 4 days later, restructured the very test | **HIGHEST.** One fix passed, immediately, and missed it. Then seven years of silence. Ships in 3.12/3.14. |
| **P3-F9** | 3y 9m (2022-10) | 5 | 0 | HIGH. Zero fix traffic through `_PyDict_SendEvent`; the hazard is documented in `typeobject.c` and never cross-applied. |
| **P3-F15** | 2y 3m (2024-04) | 13 | **5** — incl. `547d8daf780` (split-table crash), `02202c117b5` (clear-at-lookup crash), `43c76587c1b` | **HIGHEST by traffic.** Five crash fixes walked through `insertdict` since 2024 without seeing it. Now also 9-day-old re-landed code. |
| **P3-F15c** (`clear_lock_held`) | long-standing | 36 | **3** — incl. `02288bf0225` UAF (2026-03), `16928547063` (2026-05) | HIGH. Two UAF/race fixes in the last 5 months, in this function. |
| **P3-F16a** (`dict_popitem_impl`) | 3y 9m | 11 | **3** — incl. `c00ac578241` "Fix data race in dict.popitem()" (2025-03) | HIGH. A data-race fix landed in this exact function and did not address the notify window. |
| **P3-F16b** (`delitem_common`) | **8y 11m** (2017-08) | 19 | 3 | HIGH. Nine years; CPY-0115 also lives here. |
| **P3-F1 / P3-F4** | 6y 2m / 11y | 27 | **6** — incl. `b8e925b4f8f6` (2026-01, crash fix, 3 lines, same function) | **HIGHEST by fix-adjacency.** A crash fix landed *in this function* 6 months ago, touched the freeslot guard, and left the `cmp > 0` ordering and the `found_unused` ordering untouched. |
| **P3-F8a** (`insert_combined_dict`) | 2y 5m | 7 | 2 — incl. `02202c117b5` (2025-10) | MODERATE-HIGH |
| **P3-F6** | 1y 8m | n/a (regex miss) | — | MODERATE. Introduced *by* a crash fix; unrevisited since. |
| **P3-F5** | **20y 11m** (2005-08) | 10 / 11 | **0** in `remove`; 1 unrelated in `discard` | MODERATE. Longest-lived defect in the slice. `c98182be8d4` (2025-12) modernised the sibling *contains* path and left these two. |
| **P3-F7** | 2y 5m | 0 | 0 | MODERATE. `dictiter_iternext_threadsafe` has **never been touched** since birth. |
| **P3-F10** | 7y 8m (body) / 2y 5m (latch) | 2 | 0 | MODERATE. Reverse iterator got no FT arm when `92abb0124037` swept 6 files adding critical sections — a sweep that skipped a structural sibling. |
| **P3-F11** | 4y 9m | 3 | 0 | MODERATE |
| **P3-F12** | 22y | 10 | 3 (none relevant) | LOW-MODERATE |
| **P3-F13** | 2y 3m / 1y 5m | 1 each | 0 | LOW-MODERATE. Both single-revision lines. |
| **P3-F3** | **22y 8m** | 17 | 1 (signature cleanup) | LOW. `set_clear_internal` re-signed in 2024, semantics untouched. |
| **P3-F2** | 5m (2026-02) | — | 0 | LOW-MODERATE. Newest defect in the run; latent. |

**Pattern worth stating to the campaign:** the two findings with the *most* fix traffic
(P3-F15, 5 fixes passed; P3-F1/F4, 6 fixes passed) and the one with the *least* (P3-F7, zero
touches) are both high-risk, for opposite reasons. Fix traffic is not protective — in this
slice it is anti-correlated with correctness at the sites the fixes walked past, which mirrors
the file-level finding in the toolkit's own `notes[]` that raw churn is anti-correlated with
defect density on `Objects/`.

---

## 7. Calibration answer: how many of the 16 were introduced by a commit that was itself fixing a bug?

**4 of 16 — 25%.**

Objective criterion used: the upstream tracker's own `type-bug` label (or, pre-GitHub, an
explicit "Fix <tracker-id>" subject), not the commit-subject heuristic.

| finding | introducing commit | issue | why it counts |
|---|---|---|---|
| P3-F14 | `24dc2f8c566` (removal) + `2e3d873d3bd0` (restructure) | bpo-38525 `type=crash`; bpo-38555 | a crash fix deleted the bound; a UB fix rewrote past it |
| P3-F5 | `b02c35e2081` | SF #1257731 | the fix *is* the over-broad narrowing |
| P3-F6 | `bf542f8bb9f1` | gh-124470 `type-bug` | a crash fix added an assert that aborts |
| P3-F8 | `5592399313c9` | gh-122208 `type-bug` | a notify-ordering fix moved the notify into a worse window |

**Answer to the campaign's real question — does fix-adjacent code deserve its own priority
signal? Yes, but it is not the strongest available signal, and here is the number that says so.**

- **Fix-introduced: 4/16 (25%).**
- **FT-migration-introduced: 6/16 (37.5%)** — gh-112075 (5 commits) + gh-130313, all
  `type-feature`, all one author, all 2024-02 → 2025-02.
- Feature/refactor/original-import: 6/16 (37.5%).

So a `fix_introduced` flag would surface 25% of this slice's findings, and a
`large_migration_epic` flag would surface 37.5%. **Recommendation: build the priority signal on
"introduced by a commit belonging to a multi-commit migration epic", with fix-adjacency as a
second, complementary flag.** They overlap on zero findings here, so together they cover
**10/16 (62.5%)**.

Two qualifications, stated so the number is not over-read:

1. **The 4 fix-introduced findings are disproportionately severe.** They include the slice's
   headline (P3-F14, live SIGSEGV in released 3.12 and 3.14), P3-F6 (SIGABRT 10/10), P3-F8
   (folded into the slice's other headline P3-F9), and P3-F5. By severity rather than count,
   fix-adjacency is the better signal.
2. **A second, distinct fix-adjacency measure is stronger than the introduction one.** Counting
   findings where *a later fix commit passed through the enclosing function without noticing*:
   **P3-F1/F4 (6 fixes passed), P3-F15 (5), P3-F16a (3), P3-F16b (3), P3-F15c (3), P3-F8a (2),
   P3-F14 (1), P3-F5 (sibling path modernised 2025-12)** = **8 of 16 (50%)**. That is the
   highest-yield temporal signal in this slice, and D-22 is exactly what blocks the toolkit
   from computing it automatically.

---

## 8. Toolkit feedback

**Recall gap 1 (highest value) — D-22 confirmed and quantified.** §7's best signal
("a fix commit passed through this function and missed it", 8/16 = 50%) is not computable from
the current envelope. The preflight already proposed `function_watchlist`; I add the concrete
requirement that it must expose, per function, **the list of fix-shaped commits *since* a given
anchor date**, not just a count — the anchor being the `--introduced-by` result. My
`objmap_funchist.py` (~55 lines) computes this over 19 functions in both files in ~40 s using
`git log -L:funcname:file`, which sidesteps the line-drift problem entirely.

**Recall gap 2 — no revert/re-land detection.** Preflight proposed `reland_chains[]`. This pass
confirms it and adds two detector requirements the naive version would miss:
(a) reverts whose subject does **not** start with `Revert` (`72789592a34`, `e82cf8675ba`,
`f9bd05e83e3` — 3 of 14 here, 21%); (b) `This reverts commit <sha>` in the **body** under a
plain fix subject. Also: **scan re-land diffs for newly-added comments** — pair #5 is where
CPython documents the `PyErr_FormatUnraisable` re-entrancy that P3-F9 depends on.

**Precision defect — the subject-line classifier over-calls migration commits as fixes.**
`176df09adbb4` and `8b541c017ea9` are scored `fix` / `high` / `data-race`. Their parent issue
gh-112075 is labelled `type-feature` by CPython itself. Both *introduced* finding sites. A
subject containing "thread-safe" or "thread safety" is a migration marker, not a fix marker.
**Proposal:** demote `fix_confidence` when the subject matches a migration lexicon
(`make .* thread.?safe`, `add critical sections`, `avoid locking`, `use atomics`), and — much
better — resolve `gh-NNNNN` in the subject to the issue's `type-*` label via `gh api`, cached.
That single lookup was decisive for 8 of the 16 origin classifications in §3.

**Recall gap 3 — `--introduced-by` needs a `--blame-region` mode.** For P3-F14 the defect is an
*absence*. `--introduced-by dictobject.c:6283` correctly returned the 2018 feature commit and
the 2019 fix, but the actual origin (`bf61754fdf62`, a PR-branch commit that **deletes** a line
and is not in the local clone) is only reachable through the GitHub review API. **Proposal:**
when `line_history[]` contains a commit that is a squash-merge of a PR (`(GH-\d+)` /
`\(#\d+\)` in the subject), emit the PR number in the envelope so an agent knows to fetch
`repos/…/pulls/N/comments`. That one hop produced this run's single best result. Deleted-line
archaeology is invisible to `git log -L` by construction.

**Precision note, positive:** `--introduced-by` handled 29/29 sites with no failures, correctly
followed renames, and its `files_touched` field flagged the two mechanical-sweep commits
(`970e719a7a82`, 18 files; `c32dc47aca6e`, 35 files) that the guidance predicts are risky. The
`fix_confidence`-sorted `recent_fixes` ordering was not needed here because the ledger supplied
the findings, but the primitive itself is sound.

**Report-structure note:** three of my four highest-value outputs (§2.2, §4.1 NULL-callback,
§4.2 gh-148660) came from *upstream review records and commit messages*, not from diffs. The
agent prompt's Phase 2 tells the agent to "read the fix diff and commit message". For CPython
specifically, the **PR review thread** is a distinct and richer source — it is where a check gets
deleted. Worth promoting to a first-class step in the agent definition.

---

## 9. Noticed outside slice (one line each)

- `Objects/odictobject.c` — 8 stale-index sites out of 18; gh-148660 (2026-06-28) fixed one and gh-119004 (2024-09-24) fixed one, both single-function; `_odict_resize:585` is an OOB write. **Hand to the `obj-odict` slice with the in-file fix template.**
- `Objects/codeobject.c:52-54`, `Objects/funcobject.c:39-41`, `Python/context.c:131-133` — unguarded `cb(...)` where dict/type write `if (cb && ...)`; NULL call in release builds on re-entrant `ClearWatcher`. CONSIDER, one line each.
- `Doc/c-api/type.rst:138-155` — `PyType_WatchCallback` documents no error protocol, yet `typeobject.c:1222` treats `< 0` as "exception set" and reports it via `PyErr_FormatUnraisable`. Doc gap adjacent to P3-F9.
- `Objects/typeobject.c:6712` `type_update_dict` — comment at `:6697-6702` states "we know that this will all complete without releasing the locks"; the watcher unraisable at `:1223` is exactly that re-entrancy. **Swept, not verified-by-me.**
- `Objects/listobject.c:3415` `list_remove_impl` — stale index after a mutating `__eq__`; memory-safe (callee clamps) but deletes the wrong element. Guarded twin `_collectionsmodule.c:1477-1481` `deque_remove_impl`. Correctness gap only.
- `Objects/weakrefobject.c:1000` and its copy-pasted twin `Python/gc.c:921-924` — duplicated dispatcher, both hold raw pointers across the unraisable; mitigated but the duplication is a propagation hazard.
- `Modules/posixmodule.c:694` `PyOS_BeforeFork` — `run_at_forkers` dispatch (and therefore `sys.unraisablehook`) runs immediately before `_PyEval_StopTheWorldAll`. Adjacent to P3-F2.
