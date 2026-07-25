# Git History Context (preflight) — slice `obj-mappings`

Agent: `git-history-context` · Target: `/home/danzin/projects/cpython` @ `4f3be1b5777` (2026-07-15)
Scope: `Objects/dictobject.c` (8,597 lines) + `Objects/setobject.c` (3,228 lines)

---

## Clone health

- **Shallow clone: NO.** `git rev-parse --is-shallow-repository` → `false`. Verified directly, not
  inferred. History is complete and the temporal analysis below is trustworthy.
- **Repo depth:** 132,320 commits, back to **1990-08-09** (`7f777ed95a19`).
- **`analyze_history.py` envelope:** `is_shallow_clone: false`, `timeout_hit: false`,
  `commit_cap_applied: false` (762 / 407 commits vs. a 50,000 cap), window `--days 13000`
  `--density-days 13000` — i.e. the **entire** history of both files, not a window.
- **Envelope `notes[]`, reproduced verbatim** (identical for both runs, one entry each):
  > "Prefer `watchlist` (recent crash-fix commits per KLOC) over `file_churn[].churn_rate` for
  > prioritization: on CPython's Objects/ raw churn was anti-correlated with defect density."

  No shallow warning, no commit cap, no timeout. Nothing was truncated.
- File first-commits: `dictobject.c` 1993-03-27 (`4b1302bd1d2`), `setobject.c` 2003-11-16
  (`a690a9967e7`). Full-history commit counts (`git log --follow --no-merges`): **829** / **478**.

**Denominator for everything below:** 829 dict commits + 478 set commits = 1,307. Of these,
**176** (dict) and **72** (set) are crash-shaped fix commits — a commit whose subject+body matches
both a fix marker and at least one crash/concurrency lexicon term. Per-function attribution is by
git's `diff=cpp` hunk funcname context over `git show -U0`, which resolves the enclosing function
at the commit's own point in history (so it survives the line-number drift that a
current-file-line-range mapping would not).

**Caveat, stated once:** commits from the 2006–2008 svn-merge era carry megabodies that match
every lexicon term at once. Any cluster count below that leans on pre-2010 data is inflated. All
the *rankings* and all the *active* clusters are driven by 2015+ data, where subjects are one
change each.

### Both files, by crash class (script's own classifier, full history)

| class | dictobject.c | setobject.c |
|---|---|---|
| data-race | 23 | 9 |
| crash | 16 | 7 |
| refcount | 9 | 8 |
| memory-leak | 7 | 2 |
| use-after-free | 3 | — |
| overflow | 2 | 4 |
| assertion | 2 | — |
| uninitialized | 1 | — |
| null-deref | — | 1 |

`watchlist` crash-fix density: **dictobject.c 7.91/KLOC**, **setobject.c 10.53/KLOC**. Both are
far above the `Objects/` norm; setobject.c is *denser* despite being a third the size. Raw churn
would have ranked them the other way (`churn_rate` 4.78 vs 6.39 — closer than the density gap).

---

## 1. Per-function bug-fix density — **this is the reading order**

Ranked by **2023+ crash-fix commits** (recency-weighted, per the toolkit's own guidance), with
full-history totals as a tiebreak. Read top-down.

### `Objects/dictobject.c`

| # | function | 2023+ fixes | total | last touched | dominant classes |
|---|---|---|---|---|---|
| 1 | `dictresize` | **12** | 31 | 2026-02-18 | data-race, resize, refcount |
| 2 | `insertdict` | **10** | 21 | 2026-02-18 | data-race, crash, resize |
| 3 | `delitem_common` | **9** | 11 | 2026-02-18 | data-race, stale-borrowed |
| 4 | `new_keys_object` | **7** | 13 | **2026-07-06** | data-race, refcount, overflow |
| 5 | `dict_popitem_impl` | **7** | 7 | 2026-02-18 | data-race |
| 6 | `dict_setdefault_ref_lock_held` | **7** | 7 | 2026-02-18 | data-race, crash |
| 7 | `dictkeys_decref` | 7 | 8 | 2024-05-24 | data-race, refcount |
| 8 | `clear_lock_held` | **6** | 6 | 2026-05-18 | data-race, **use-after-free** |
| 9 | `dict_get_impl` | 6 | 7 | 2025-01-28 | data-race, stale-borrowed |
| 10 | `_PyDict_DetachFromObject` | 6 | 6 | 2025-02-20 | data-race, assertion |
| 11 | `free_keys_object` | 5 | 7 | **2026-07-06** | data-race, **deadlock**, crash |
| 12 | `dictiter_new` | 5 | 11 | 2025-10-13 | iterator, data-race |
| 13 | `dict_dealloc` | 5 | 13 | 2025-03-04 | refcount, data-race, crash |
| 14 | `_Py_dict_lookup` | 5 | 5 | 2024-12-19 | data-race, **stale-borrowed** |
| 15 | `insert_to_emptydict` | 5 | 5 | 2026-02-18 | data-race |
| 16 | `dict_dict_merge` | 5 | 5 | 2026-02-18 | data-race |
| 17 | `split_keys_entry_added` | 5 | 5 | 2024-07-11 | data-race |
| 18 | `clone_combined_dict_keys` | 5 | 6 | 2024-04-08 | data-race, uninitialized |
| 19 | `_PyDict_CheckConsistency` | 4 | 7 | **2026-07-06** | data-race, deadlock |
| 20 | `dictiter_iternextitem` | 4 | 12 | 2025-03-27 | iterator, crash, refcount |

`insert_split_key` does not appear by hunk-count because it is young, but it is the **hottest
single site in the file by iteration count** — see §3. Read it first regardless of this table.

### `Objects/setobject.c`

Note: `set_add_entry` → `set_add_entry_takeref` and the `set_lookkey` rewrite split each
function's history at `c98182be8d4` (2025-12-13). Merged totals are given.

| # | function | 2023+ fixes | total | last touched | dominant classes |
|---|---|---|---|---|---|
| 1 | `set_add_entry` + `set_add_entry_takeref` | **4** | **15** | **2026-01-14** | crash, data-race, resize |
| 2 | `set_swap_bodies` | **5** | 6 | 2025-12-24 | data-race, refcount, iterator |
| 3 | `PySet_Add` | **4** | 6 | 2025-11-11 | data-race, resize, crash |
| 4 | `set_lookkey` | 2 | 9 | 2025-12-13 | refcount, **re-entrancy**, memory-leak |
| 5 | `set_pop_impl` | 3 | 3 | 2025-12-13 | data-race, refcount, iterator |
| 6 | `set_empty_to_minsize` | 3 | 3 | 2025-12-13 | data-race, refcount |
| 7 | `_PySet_Contains` | 3 | 3 | 2025-12-13 | data-race, refcount |
| 8 | `set_richcompare` | 3 | 3 | 2025-04-07 | data-race, uninitialized |
| 9 | `set_table_resize` | 1 | 9 | 2025-12-13 | resize, data-race, overflow |
| 10 | `setiter_iternext` | 2 | 6 | 2025-12-13 | data-race, refcount, iterator |
| 11 | `frozenset_hash` | 2 | 5 | 2025-12-13 | data-race, uninitialized |
| 12 | `set_discard_entry` | 2 | 3 | 2025-12-13 | data-race, iterator |
| 13 | `set_merge_lock_held` | 2 | 2 | 2025-12-13 | data-race, refcount, iterator |
| 14 | `set_symmetric_difference_update` | 2 | 13 | 2024-03-08 | crash, data-race, overflow |
| 15 | `set_merge` | 1 | 10 | 2024-03-08 | crash, resize |

Structurally new in the last 7 months and therefore **least settled, zero fix history of their
own**: `set_do_lookup`, `set_compare_entry_lock_held`, `set_compare_threadsafe`,
`set_compare_frozenset`, `set_lookkey_threadsafe`, `ensure_shared_on_read`, `set_zero_table`,
`copy_small_table` — all born `c98182be8d4` (2025-12-13) or `e8e044eda34` (2025-12-24). A zero
fix-count on a 7-month-old function is not evidence of correctness.

---

## 2. Reverts and re-lands — **the recurring hazard**

Every revert / re-land pair in the two files, with what recurred.

### 2a. `insert_split_key` — 4 iterations, 2 reverts. **This is CPY-0096.**

| # | date | commit | what |
|---|---|---|---|
| 1 | 2026-06-11 | `efb2fffae1f` | gh-150490 / #150489 — raise `PyType_Modified` for insertion into split dict. **Landed.** |
| 2 | 2026-06-25 | `bef57062226` | gh-151593 / #152200 — "Fix dead lock in PyDict insert_split_key()". Attempted fix. Also **wrote the prohibition comment**. |
| 3 | 2026-06-26 | `9626ef87f42` | gh-152238 / #152232 — **Revert both.** |
| 4 | 2026-07-06 | `20921922fa4` | gh-150490 **"(take 2)"** / #152914 — **re-landed**, and is what ships at the target ref. |

The revert's own stated reason (`9626ef87f42` body, verbatim):
> "For gh-150489, it violates locking discipline and results in deadlocks, gh-151593 is an example
> of it being hit in CI. The attempted fix gh-152200 avoids the deadlock but introduces a
> data-race. The race window is small but can be triggered with pure Python code."

**Two distinct failure modes on the same path in 15 days: deadlock, then data race.** The
re-land is 10 days old at the target ref and has had no soak time.

**The sharpest fact in this report:** the revert threw away the *code* of `bef57062226` but
**kept its comment**. `git blame` puts `dictobject.c:218-226` at `bef570622263`, still present at
HEAD:

```c
// We are not allowed to acquire other locks within LOCK_KEYS(). For example,
// PyType_Modified() must not be called within LOCK_KEYS() since it acquires
// the type lock.
#define LOCK_KEYS(keys) PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)
```

and the re-land `20921922fa4` put this at `dictobject.c:1962-1981`, inside `LOCK_KEYS(keys)`:

```c
    LOCK_KEYS(keys);
    ix = unicodekeys_lookup_unicode(keys, key, hash);
    if (ix == DKIX_EMPTY && keys->dk_usable > 0) {
        ...
        if (type) {
            // we acquired the type lock above
            _PyType_Modified_Unlocked(type);
        }
        Py_ssize_t hashpos = find_empty_slot(keys, hash);
        ...
        STORE_SHARED_KEY(ep->me_key, Py_NewRef(key));
```

Take-2 dodges the *literal* deadlock by acquiring the interpreter types mutex **before**
`LOCK_KEYS` and calling the `_Unlocked` variant. But the same comment's other two constraints —
"`LOCK_KEYS()` is essentially the inner-most code" and "we don't call `Py_DECREF()` or similar
while holding the keys lock" — are still violated by `_PyType_Modified_Unlocked` (watcher
callbacks, `sys.unraisablehook`) and by `Py_NewRef` / `find_empty_slot` under the mutex. The
comment forbidding this is **10 lines above the code doing it**.

Timeline note confirming RUN_CONTEXT: the prohibition was written 2026-06-25; the code it
prohibits (`d5df25268b0`, gh-112075 "`_Py_dict_lookup` needs to lock shared keys") landed
**2024-04-25** — 26 months earlier — and was never revisited. That is CPY-0107's lock-order
inversion.

### 2b. `set_add_entry` freeslot tracking — a revert that *introduced* a 5-year-latent crash

| # | date | commit | what |
|---|---|---|---|
| 1 | 2020-05-03 | `3dd2157` | GH-19881 "Simplify set entry insertion logic" — **removed** freeslot tracking. |
| 2 | 2020-05-10 | `2cc9b8486dd9` | GH-20028 "Improve code clarity for the set lookup logic" — **restructured the whole probe loop**, reader and writer. |
| 3 | 2021-03-24 | `72789592a34` | bpo-43198 "**Revert 3dd2157** that removed freeslot tracking" — re-grafted freeslot onto a body that step 2 had changed underneath it. |
| 4 | 2026-01-14 | `b8e925b4f8f6` | gh-141805 "Fix **crash** after concurrent addition objects with the same hash to set" — added `if (freeslot->hash != -1) goto restart;`. |

**The recurring hazard: a revert that restores removed code into a body that has since been
restructured.** The 2021 revert restored `freeslot` but added no revalidation of it across the
`PyObject_RichCompareBool` re-entry that step 2 had moved. It took **4 years 10 months** for that
to surface as a reported crash. The fix is **6 months old** at the target ref.

### 2c. `setiter` cursor — the raw-pointer-cursor revert (2015)

`9632a7d7350` (2015-07-07) reverted `c9782a9ac031` with the message *"pending a stronger test for
mutation during iteration"*. The reverted change had swapped `setiterobject`'s cursor from an
**index** (`si_pos`) to a **raw `setentry *`**; the revert restored the index, because a raw
pointer into `so->table` does not survive a resize during iteration. The "stronger test" was never
written; the revert is permanent. **The hazard class this revert exists to prevent is a raw
`setentry*` held across anything that can mutate the table.**

### 2d. Dict GC-tracking — landed and reverted twice in 22 days

| date | commit | what |
|---|---|---|
| 2024-11-18 | `b0fcc2c47a3` | GH-126491 "Mark objects reachable from roots before cycle collection" |
| 2024-11-19 | `899fdb213db` | **Revert** (1 day later) |
| 2024-12-06 | `023b7d21414` | GH-126491 "Lower heap size limit with faster marking" — re-land |
| 2024-12-10 | `690fe077f6b` | **Revert** — body: *"which introduced a refleak"* |

Never re-landed. Both attempts touched dict GC-track/untrack. Recurring hazard: **dict
GC-tracking invariants** (`_PyDict_MaybeUntrack`, `split_keys_entry_added`). Note
`aea0c586d18` (2024-11-20, "Don't lazily track and untrack dicts") landed between the two.

### 2e. Older reverts, catalogued for completeness

`ad4857884b4` (2021-12-26, bpo-43413, revert of `set.__init__` changes);
`f8d1a31e706` (2015-01-26, "the `key==dummy` test wasn't supposed to change");
`7aa53411647` (2015-05-30); `4f2dab5c337` (2011-05-27); `39e32a7e816` (2010-11-24);
`dee3f65d986` (2008-01-26, "Revert `PySet_Add()` changes"); `a37430a0cec` (2008-02-12,
"`dict.copy()` rises from the ashes"); `e6e383f4984` (2007-04-16, `dict.update()` /
`__getitem__` semantics). None of these bear on live code; listed so the search is closed.

---

## 3. The free-threading era (2023 → now)

**Structural fact that frames everything else: since 2023-01-01, not one crash-or-race fix commit
touched both files.** 84 commits in history touch both; the 8 since 2023 are all cosmetic or
cross-cutting (PEP 585 docs, docstring length, `_PyObject_HashFast` → `PyObject_Hash`, trashcan
move into `Py_Dealloc`, unhashable-type error messages, `frozendict` hash). The two files' FT
migrations were carried out **independently, by different authors, ~20 months apart**.

### `dictobject.c` — 48 FT commits since 2023

`LOCK_KEYS` was introduced **2024-02-20** (`176df09adbb`, gh-112075) and has been touched by
exactly **six** commits ever:

| date | commit | what |
|---|---|---|
| 2024-02-20 | `176df09adbb` | gh-112075 — **introduces** `LOCK_KEYS` / `dk_mutex` / `_Py_LOCK_DONT_DETACH` |
| 2024-04-25 | `d5df25268b0` | gh-112075 — `_Py_dict_lookup` locks shared keys ← **CPY-0107's origin** |
| 2024-07-10 | `3ec719fabf9` | gh-117657 — TSan race in `_PyDict_CheckConsistency` |
| 2025-01-14 | `b5ee0258bf5` | gh-115999 — `LOAD_ATTR` specialization |
| 2026-06-25 | `bef57062226` | gh-151593 — deadlock fix + **the prohibition comment** |
| 2026-06-26 | `9626ef87f42` | gh-152238 — **revert** |

Current `LOCK_KEYS` scopes: `dictobject.c:1283, 1317, 1385, 1962, 2230, 7317` plus the
`LOCK_KEYS_IF_SPLIT` pair at 738/794. **Six scopes; two of them (1385 `_Py_dict_lookup`, 1962
`insert_split_key`) already carry recorded findings.** The other four have never been audited:
1283, 1317, 2230 (inside `dictresize` — the #1 function in the reading order), 7317.

Densest recent FT correctness fixes (all dict-only):
`6112d70abee` (2026-06-11, race clearing embedded values) · `16928547063` (2026-05-18,
`dict.clear()` race on split table) · `8a4895985f4` (2026-05-11, watcher API thread-safety) ·
`3ab94d68428` (2026-04-12, atomic `_ma_watcher_tag`) · `02288bf0225` (2026-03-02, **use-after-free
in `dict.clear()` with embedded values**) · `e666a01ef42` (2026-01-30, race in method lookup) ·
`547d8daf780` (2025-12-03, split-table crash) · `02202c117b5` (2025-10-29, **crash if `clear` is
called at the `lookup` stage**).

### `setobject.c` — 12 FT commits since 2023, but 5 of them in the last 8 months

| date | commit | what |
|---|---|---|
| 2024-03-08 | `c951e25c249` | gh-112069 — **first** set critical sections |
| 2024-04-17/19/26 | `a734fd5cf71`, `710c01be94c`, `94444ea45a8`, `eb20a7d12c4` | iterator / `PySet_GET_SIZE` / `_PySet_NextEntryRef` / never-exposed fast path |
| 2024-07-09 | `9c08f40a613` | gh-117657 — TSAN races |
| 2025-04-02/07 | `76f6b5e64a8`, `ed99e28d5b5` | relaxed atomics for set + frozenset hash |
| 2025-08-30 | `f58a7c71758` | gh-132657 — avoid locks/refcounting in frozenset lookups |
| **2025-12-13** | **`c98182be8d4`** | gh-132657 — **lock-free set contains**: introduces `set_do_lookup` + 3 compare callbacks. The architectural rewrite. |
| 2025-12-14 | `19c72d23fd9` | stronger memory ordering for `so->mask` — **next-day follow-up fix** |
| 2025-12-24 | `e8e044eda34` | gh-143100 — memcpy data race → `copy_small_table` |
| 2026-01-14 | `b8e925b4f8f6` | gh-141805 — freeslot revalidation crash fix |
| 2026-04-02 | `9d087d6d41e` | GH-147985 — lock-free lookup in `PySet_Contains` |

`setobject.c` has **no `LOCK_KEYS` equivalent** — it uses per-object `Py_BEGIN_CRITICAL_SECTION`
only. Its newest code is 3½ months old and has already needed two follow-up fixes.

---

## 4. **Blame origin of the `set_add_entry` restart loop, and the siblings left behind**

This is the item the brief called the highest-value pattern in the campaign. It resolves into a
concrete, mechanically-verifiable asymmetry.

### The restart protection was added in three separate acts

| line(s) | added by | date | what |
|---|---|---|---|
| `setobject.c:264` `restart:` | `ff9e18a863aa` | 2015-07-20 | the label, Issue #24583 consolidation |
| `setobject.c:286-296` mutation guard | `2cc9b8486dd9` | 2020-05-10 | `table = so->table; … if (table != so->table \|\| entry->key != startkey) goto restart;` |
| `setobject.c:311-313` freeslot guard | `b8e925b4f8f6` | **2026-01-14** | `if (freeslot->hash != -1) { goto restart; }` |

### Were siblings updated at the same time?

**2020 — yes, but with a divergence baked in.** `2cc9b8486dd9` rewrote *both* the reader
(`set_lookkey`) and the writer (`set_add_entry`) in one commit — and ordered the guard
differently in each. Verified by reading both functions at that exact commit:

```
reader  set_lookkey  @2cc9b8486dd9:      writer  set_add_entry  @2cc9b8486dd9:
  cmp = RichCompareBool(...)               cmp = RichCompareBool(...)
  if (cmp < 0)  return NULL;               if (cmp > 0)  goto found_active;   <-- returns FIRST
  if (table != so->table ||                if (cmp < 0)  goto comparison_error;
      entry->key != startkey)              if (table != so->table ||
      return set_lookkey(...);   <-- wins      entry->key != startkey)
  if (cmp > 0)  return entry;                  goto restart;
```

In the **reader**, revalidation runs *before* the positive-match return, so a mutation during the
user `__eq__` forces a restart. In the **writer**, `cmp > 0 → goto found_active` returns *before*
the revalidation is ever reached. `found_active` is `Py_DECREF(key); return 0;` — "already
present, drop the new key."

**This ordering has been unchanged for 6 years and survived the 2025 architectural rewrite.**
`c98182be8d4` (2025-12-13) extracted the reader's compare into
`set_compare_entry_lock_held` (`setobject.c:138-165`) and **preserved the reader ordering**
(`CHANGED` returned before `FOUND`), with `set_lookkey` (`:414`) restarting on it via
`do { … } while (status == SET_LOOKKEY_CHANGED);`. The writer's hand-rolled loop was left with
its own copy of the compare and its own ordering.

**2026 — no.** `b8e925b4f8f6` is a **3-line, single-function** change touching only
`set_add_entry_takeref`. It did not look at any sibling. (For the freeslot guard specifically this
is structurally defensible — only the writer tracks a `freeslot`; `grep -n freeslot
Objects/setobject.c` returns 7 hits, all inside `set_add_entry_takeref`. That is a **structural**
zero for the siblings, not an evidential one.)

### The refactor left the writer behind

`c98182be8d4` unified **four read paths** onto one `set_do_lookup` probe loop with pluggable
compare callbacks (`set_compare_entry_lock_held`, `set_compare_threadsafe`,
`set_compare_frozenset`). `set_add_entry_takeref` — the **only writer** — kept a **fifth,
hand-copied** probe loop and a **fourth, hand-copied** compare body. The 2026 crash fix landed in
that left-behind copy, which is direct evidence that this loop is reached by real re-entrancy in
the field.

> **Hand-off:** `setobject.c:277-296` (`set_add_entry_takeref` inline compare) versus its guarded
> twin `setobject.c:138-165` (`set_compare_entry_lock_held`). The twin's guard defends against
> *the set being mutated by a re-entrant `__eq__` during the compare*. The writer has the same
> guard but is unreachable on the `cmp > 0` path. — **ft-race-scanner + recursion/re-entrancy
> owner.** Do not assume "same guard present" means "same protection"; check the order.

### Also newly-born and unguarded by construction

`set_compare_frozenset` (`setobject.c:172-192`) carries a comment stating it *"doesn't need to
check if the set has changed"*. Born 2025-12-13, zero fixes since. It still calls
`PyObject_RichCompareBool` on user objects, without the `Py_INCREF(startkey)` the other two
compare callbacks take. The claim rests on frozenset immutability — but `set_swap_bodies`
(`:1512`), `_PySet_Freeze` (`:1571`) and `frozenset_hash` caching all write to frozenset bodies,
and `set_swap_bodies` needed its own race fix 11 days later. **Verify the immutability premise
before accepting this as a guarded twin.**

---

## 5. Fixes whose sibling file was not updated

Mechanically: **zero** crash-or-race fixes since 2023 touched both files (§3). The list below is
the subset where the *shape* is plausibly shared, for the safety agents to check — each is a
pointer, not a verdict.

| fixed in | commit | shape | sibling site that was never touched |
|---|---|---|---|
| set | `a7427f2db93` 2025-02-11 gh-129967 | `set_repr_lock_held` replaced `PySequence_List(so)` with a manual `set_next` walk because *"PySequence_List … might re-lock the object lock or the GIL and allow something to clear the set from underneath us"* | `dict_repr_lock_held` / `anydict_repr_impl` (`dictobject.c:3685, 3770`) — does the dict repr path build its key list through anything that can re-enter? |
| set | `e8e044eda34` 2025-12-24 gh-143100 | raw `memcpy` of the small table → per-entry `_Py_atomic_store_*` (`copy_small_table`), because lock-free readers can observe a torn table | dict raw `memcpy` of entries at `dictobject.c:1042` (`clone_combined_dict_keys`), `2281`/`2298` (inside `dictresize` — **#1 in the reading order**), `4465`. Dict lookup went lock-free in `54071460d76` (2024-02-20); the same reader/writer relationship exists. |
| dict | `c00ac578241` 2025-03-11 gh-131113 | `dict_popitem_impl` plain stores → `STORE_KEY`/`STORE_VALUE`/`STORE_HASH` atomics when clearing an entry | `set_pop_impl` (`setobject.c:911-934`) writes via `FT_ATOMIC_*` but **reads** `entry->key` plain in its scan loop and in `key = entry->key`. Same for `set_dealloc` (`:734`) and `set_next` (`:710`). This is the `atomic_plain_asymmetry` population the pre-run scan already counted (17 in slice). |
| dict | `02202c117b5` 2025-10-29 gh-140551 | dict cleared *during* `_Py_dict_lookup` (user `__eq__`) left `insertdict` inserting into a key-kind that no longer matched; fix re-checks and re-resizes in `insert_combined_dict` | `set_clear_internal` (`setobject.c:646`) racing a re-entrant compare in `set_do_lookup`. `set_lookkey` restarts on `CHANGED`, but see §4 for the writer path that does not. |
| dict | `80e6d3ec497` 2025-03-04 gh-130547 | race between `dict_dealloc` and `split_keys_entry_added` | `set_dealloc` (`setobject.c:734`) — plain reads of `entry->key`, `so->used`, `so->mask` while `SET_IS_SHARED` readers may still be in flight |
| dict | `375b723d587` 2024-06-24 gh-120858 | `PyDict_Next` deliberately made *not* to lock the dict | `set_next` (`setobject.c:710`) — its own comment says *"In general, it isn't safe to use `set_next` in a loop that mutates the table"*, and `set_repr_lock_held` was rewritten in 2025 to call it. CPY-0115 is the dict half of this pair. |
| set | `b8e925b4f8f6` 2026-01-14 gh-141805 | freeslot cached across a re-entrant compare goes stale | dict's `find_empty_slot` (`dictobject.c`, called at `:1974` from `insert_split_key` **under `LOCK_KEYS`**) — dict recomputes rather than caching, so likely structurally different; confirm rather than assume. |

### One concrete lock-shape pointer that fell out of this pass

`set_discard_entry` (`setobject.c:580-597`) receives a raw `setentry *entry` from `set_lookkey`
and then writes `entry->hash` / `entry->key` through it. `set_lookkey` (`:414`) takes and
**releases** `Py_BEGIN_CRITICAL_SECTION(so)` internally, so the pointer is used after that section
ends. Several callers (`:2251`, `:2278`) already sit inside functions asserting
`_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(so)` (`:2239`, `:2270`), which makes `set_lookkey`'s
inner acquisition a **same-object nested critical section**. Both halves — the raw-pointer
handoff across a lock boundary (the exact class §2c's 2015 revert exists to prevent) and the
nesting — are ≤ 7 months old and have no fix history. **→ lock-discipline-checker + ft-race-scanner.**

---

## 6. Recurring fix clusters, year by year

Counts are commits matching the cluster lexicon; a commit can match several. 2015+ only for the
active determination. `**` marks clusters still landing fixes in 2025–2026.

### `dictobject.c`

- **data-race — 63 commits ** ** — 16:1 17:1 18:1 23:1 **24:27 25:15 26:10**.
  The dominant story of the file. 52 of 63 are 2024+; the FT migration *is* this file's history
  now. Exemplars: `20921922fa4` (2026-07-06), `6112d70abee` (2026-06-11), `02288bf0225`
  (2026-03-02 UAF). → **ft-race-scanner, lock-discipline-checker**.
- **refcount — 36 ** ** — 18:2 19:1 20:1 22:3 23:1 24:8 25:4 26:1. Still active.
  → **refcount-auditor**.
- **crash — 33 ** ** — 16:1 17:3 19:5 21:1 24:3 25:2. `547d8daf780` (split-table crash,
  2025-12-03), `02202c117b5` (clear-during-lookup, 2025-10-29). → **null-safety-scanner,
  memory-pattern-analyzer**.
- **iterator — 27 ** ** — 16:2 18:2 19:5 22:1 24:2 25:1. `4c3d187d9f1` (2025-04-18, key-iterator
  thread safety), `375b723d587` (2024-06-24, `PyDict_Next` unlocked). **CPY-0115 lives here.**
  → **ft-race-scanner + the iterator-invalidation owner**.
- **resize — 26 ** ** — 16:7 20:1 22:1 23:1 24:1 26:1. Long tail, never fully closed.
  `dictresize` is #1 in the reading order. → **memory-pattern-analyzer**.
- **deadlock — 5, but 2 of them in June 2026 ** ** — the entire live deadlock history of this file
  is `bef57062226` + `9626ef87f42`, both about `LOCK_KEYS`. **Newest cluster in the file, and the
  one with an un-soaked re-land on top of it.** → **lock-discipline-checker (highest priority)**.
- **use-after-free — 3, one in 2026 ** ** — `02288bf0225` (2026-03-02, `dict.clear()` with
  embedded values), `297f2e093ec` (2024-08-22), `2294f3aee14` (2017-02-12, key-sharing dict). All
  three are **split/shared-keys** bugs. The class recurs on the same substructure roughly every
  4 years and just fired. → **memory-pattern-analyzer, ft-race-scanner**.
- **stale-borrowed — 7 — 21:1 24:4** — dormant since 2024-12 but concentrated in
  `_Py_dict_lookup` / `dict_get_impl` / `PyDict_Next`, all of which sit high in the reading order.
- **reentrancy — 11 — last clear instance 2024-05-06** (`5a1618a2c8c`). Reads dormant, but the
  brief's whole thesis is that this class is *under-reported*, not absent. Treat "dormant" here as
  a detection gap, not a safety claim.
- **memory-leak — 13, all 2016** — genuinely dormant. **assertion — 15, last 2024.**
  **overflow — 11, last 2023-03** (`65fb7c4055f`).

### `setobject.c`

- **data-race — 22 ** ** — 18:1 22:1 **24:8 25:7** 26:1. Whole cluster is 2024+.
  → **ft-race-scanner**.
- **refcount — 19 ** ** — 15:2 18:1 22:3 23:2 24:1 25:2. `f58a7c71758` (2025-08-30, avoid
  refcounting in frozenset lookups) removed refcounting from a hot path — a refcount cluster whose
  most recent entry *deletes* protection. → **refcount-auditor**.
- **crash — 18 ** ** — 15:2 16:1 17:1 22:1 **26:1**. `b8e925b4f8f6` (2026-01-14) and
  `4a66615ba73` (2022-02-11, *"Don't crash when set operations mutate the sets"*). Both are
  mutation-during-operation. **This is the file's signature bug and it is 6 months from its last
  firing.** → **ft-race-scanner + re-entrancy owner**.
- **iterator — 14 — 15:1 16:1 18:2 24:1.** Reads dormant, but §2c's permanent revert and
  `set_next`'s own caution comment mean the class is *avoided by convention*, not eliminated. The
  2025 `set_repr_lock_held` rewrite newly routes repr through `set_next`.
- **resize — 12 — last 2022.** `set_table_resize` has 9 lifetime fixes; the newest FT code
  (`set_lookkey_threadsafe`) reads `so->table` / `so->mask` lock-free while resize swaps them.
- **re-entrancy — 6 — 15:1 22:1.** Small count, but `4a66615ba73` and `9632a7d7350` are both in
  it, and it is the exact class §4 is about.
- **overflow — 8, last 2018** (`6c7d67ce83a`, signed overflow in `set_table_resize`).
  **memory-leak — 6, last 2015.** **deadlock — 4, all pre-2008 svn noise; effectively zero.**

---

## 7. Priority hand-off

**Read in this order.**

| rank | site | why (temporal) | agent |
|---|---|---|---|
| 1 | `dictobject.c:1962` `insert_split_key` + `:218-226` comment | **4th iteration, 2 reverts (deadlock, then race), re-landed 10 days before the target ref, contradicts the comment 10 lines above it.** CPY-0096. | lock-discipline-checker, ft-race-scanner, stw-safety-checker |
| 2 | `setobject.c:277-296` `set_add_entry_takeref` vs `:138-165` twin | Guard present but **ordered so the positive-match path skips it**, 6 years, survived the 2025 rewrite. Left out of the `set_do_lookup` unification. | ft-race-scanner, re-entrancy owner |
| 3 | `dictobject.c:2230` `dictresize` `LOCK_KEYS` scope + `:2281/2298` memcpy | #1 by fix count (12 since 2023); never-audited `LOCK_KEYS` scope; the raw-memcpy shape that setobject fixed in Dec 2025 and dict never did | ft-race-scanner, memory-pattern-analyzer |
| 4 | `dictobject.c:1385` `_Py_dict_lookup` `LOCK_KEYS_IF_SPLIT` | CPY-0107; origin `d5df25268b0` (2024-04-25) predates its own prohibition by 26 months | lock-discipline-checker |
| 5 | `setobject.c:580` `set_discard_entry` → `:414` `set_lookkey` | Raw `setentry*` used after the CS is released; nested same-object CS from locked callers. 7 months old, zero fix history. The class §2c's permanent 2015 revert exists to prevent. | lock-discipline-checker, ft-race-scanner |
| 6 | `dictobject.c` `insertdict` / `insert_combined_dict` / `clear_lock_held` | #2, #8 by recency; `clear_lock_held` carries the 2026-03 use-after-free | ft-race-scanner, memory-pattern-analyzer |
| 7 | `setobject.c:172` `set_compare_frozenset` | Newest compare callback, ships an explicit *"don't need to check"* comment, no `Py_INCREF(startkey)`, zero fix history, and `set_swap_bodies` (which mutates frozenset bodies) needed a race fix 11 days after it landed | ft-race-scanner, refcount-auditor |
| 8 | `dictobject.c:2987` `delitem_common` + `PyDict_Next` cursor | #3 by recency (9 since 2023); CPY-0115 | iterator/ft owner |
| 9 | `setobject.c:911` `set_pop_impl`, `:734` `set_dealloc`, `:710` `set_next` | Atomic writes / plain reads asymmetry; dict's twin was fixed 2025-03 (`c00ac578241`), set's never was | ft-race-scanner |
| 10 | `dictobject.c:1283, 1317, 7317` | The three `LOCK_KEYS` scopes with **no** recorded finding and no audit | lock-discipline-checker |
| 11 | `dictobject.c:3685` `anydict_repr_impl` | Sibling of the 2025-02 `repr(set)` re-lock fix; never checked | ft-race-scanner |
| 12 | `setobject.c:484` `set_table_resize` | 9 lifetime fixes; lock-free readers now race the table/mask swap | ft-race-scanner |

### Three rules for the downstream agents, from this history

1. **"The sibling has the same guard" is not enough — check the order.** §4 is a live case where
   both functions contain the identical revalidation and only one of them can reach it.
2. **A revert is not a return to a known-good state.** §2b: restoring 2020-removed code in 2021
   into a body restructured in-between planted a crash that took 4 years 10 months to surface.
   Any code re-landed after a revert (`20921922fa4`, `insert_split_key`) deserves the same
   suspicion.
3. **A zero fix-count on `setobject.c`'s newest functions is age, not safety.**
   `set_do_lookup`, the three compare callbacks, `set_lookkey_threadsafe`, `copy_small_table`,
   `ensure_shared_on_read` are all ≤ 7 months old. Two of them already needed a follow-up fix
   within 11 days and 32 days of landing.

---

### Method / reproducibility

```bash
~/venvs/cpython-review-toolkit/bin/python \
  plugins/cpython-review-toolkit/scripts/analyze_history.py Objects/dictobject.c \
  --days 13000 --density-days 13000 --density-top 50 --workers 8   # and again for setobject.c
```
Working files: `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/hist/`
(`dict_full.json`, `set_full.json`, `perfunc.py`/`.json`, `siblings.py`/`.json`, `clusters.py`).

**Toolkit feedback (recall gap).** `analyze_history.py`'s `function_churn` reports commits per
function but does **not** fix-classify them, so it cannot produce the per-function *bug-fix*
density that is the actual priority signal — it ranked `_PyDict_CheckConsistency` (77 commits,
mostly mechanical) first while `delitem_common` (9 crash fixes since 2023) does not surface at all.
The whole-file `watchlist` is right about which *files* to read; it has no equivalent for
*functions*. Proposal: add a `function_watchlist` that intersects `function_churn` with the
existing `recent_fixes[].crash_class` by hunk-funcname attribution — the implementation is
`perfunc.py` above, ~120 lines, and it runs over both files' full history in under 90 seconds.
Second gap: the envelope has no revert/re-land detection at all, yet §2 was the single richest
vein in this pass. Proposal: a `reland_chains[]` field keying on `Revert "…"` / `(take N)` subjects
plus `This reverts commit <sha>` bodies.
