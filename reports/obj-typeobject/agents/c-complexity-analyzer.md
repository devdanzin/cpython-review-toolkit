# C Complexity Analysis — Objects/typeobject.c

INFORMED-EXPLORE, slice `obj-typeobject`. CPython main @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0).
Scope: `/home/danzin/projects/cpython/Objects/typeobject.c` only — 13,068 lines.
Regions cited as `R<n>` refer to the 37-region section map in `preflight/include_map.md` §3.

---

## Summary

- Functions analyzed: **423** (extraction coverage: **99.1%** — 427 brace blocks seen, 423 parsed,
  4 signatures unparsed, 29 multi-line signatures)
- Hotspot threshold: **1.7** (top 2% by score) — max score observed: **6.8**
- Hotspots: **9**
- Average cyclomatic complexity: **4.9**; average function length **17.9** lines; max nesting **5**
- Max manual cleanup ladder: **27** (`reduce_newobj`)

> **Read this list as a reading order, not a risk score.** On a measured 14-file `Objects/`
> sample the top 10 by score held 5 of 25 defect-bearing functions (~10x enrichment), but
> **20 of those 25 sat at the score floor**. A complexity threshold discards 80% of real
> defects. Nothing below may be read as "low score, therefore lower risk."
>
> **The score is not a valid severity input for recursion or free-threading findings** — for
> that class the metric inverts, because a recursion guard is itself a branch, so the correct
> guarded twin outscores the defective one. Several regions of this file are free-threading
> dense (R2 world-stop helpers, R13 deferred slot-update queue, R6 version tags, R19 method
> cache). **Do not let this document's rankings influence FT triage in either direction.**

The 4 unparsed signatures are almost certainly the `SLOT0`/`SLOT1`/`SLOT1BIN` macro-generated
dispatchers in R32 (10521–10618), which are not brace-delimited functions in source form. They
are short by construction; the gap does not distort the ranking.

---

## Top hotspots (by complexity score)

`exits` = distinct `return` statements after comment stripping. `labels` = goto label targets.

| Rank | Function | Lines | Region | Score | Len | Nest | Cyc | Gotos | Switch | Exits | Labels | Pass 1? |
|------|----------|-------|--------|-------|-----|------|-----|-------|--------|-------|--------|---------|
| 1 | `type_from_slots_or_spec` | 5247–5799 | R17 C-API type creation | **6.8** | 429 | 5 | 92 | 36 | 18 | 1 | `finally` ×36 | **YES** |
| 2 | `inherit_slots` | 8821–9015 | R28 slot inheritance | 3.7 | 158 | 3 | 38 | 0 | 0 | 2 | — | **YES** |
| 3 | `update_one_slot` | 11906–12068 | R34 slot-wiring engine | 3.4 | 113 | 5 | 37 | 0 | 0 | 3 | — | **YES** |
| 4 | `subtype_dealloc` | 2718–2885 | R9 subtype GC/dealloc | 2.4 | 93 | 3 | 30 | 0 | 0 | 5 | — | no |
| 5 | `_Py_type_getattro_stackref` | 6570–6689 | R21 type getattro | 2.1 | 80 | 4 | 17 | 6 | 0 | 3 | `done` ×6 | no |
| 6 | `type_ready` | 9527–9608 | R29 PyType_Ready | 2.0 | 63 | 2 | 18 | 14 | 0 | 2 | `error` ×14 | **YES** |
| 7 | `type_setattro` | 6749–6845 | R21 type setattro | 1.7 | 78 | 2 | 18 | 2 | 0 | 5 | `done` ×2 | no |
| 8 | `object_getstate_default` | 7922–8045 | R26 pickle | 1.7 | 101 | 4 | 24 | 3 | 0 | 8 | `error` ×3 | no |
| 9 | `reduce_newobj` | 8221–8316 | R26 pickle | 1.7 | 86 | 2 | 17 | 0 | 0 | 11 | — | no |
| 10 | `_PyType_LookupStackRefAndVersion` | 6305–6386 | R19 method cache | 1.6 | 66 | 4 | 14 | 0 | 0 | 5 | — | no |
| 11 | `check_duplicates` | 3269–3298 | R11 MRO C3 | 1.5 | 23 | 5 | 6 | 0 | 0 | 2 | — | no |
| 12 | `inherit_special` | 8741–8806 | R28 slot inheritance | 1.5 | 51 | 1 | 19 | 0 | 0 | 0 | — | **YES** |
| 13 | `type_new_slots_bases` | 4318–4354 | R16 `type_new` | 1.4 | 30 | 3 | 18 | 0 | 0 | 0 | — | **YES** |
| 14 | `type_new_descriptors` | 4650–4727 | R16 `type_new` | 1.4 | 66 | 3 | 14 | 0 | 0 | 5 | — | **YES** |
| 15 | `_PyObject_GetNewArguments` | 8089–8179 | R26 pickle | 1.4 | 73 | 2 | 14 | 0 | 0 | 16 | — | no |
| 16 | `type_ready_set_new` | 9414–9463 | R29 PyType_Ready | 1.4 | 31 | 4 | 13 | 0 | 0 | 2 | — | **YES** |

Nine of the sixteen are on the Pass-1 construction surface. The concentration is real, not an
artifact: R16+R17+R28+R29+R34 are 2,900 lines (22% of the file) but supply 9 of the top 16.

### Top manual cleanup ladders (goto-free cleanup burden)

`manual_cleanup_ladder = owned_locals × returns_with_cleanup`, reported only when `goto_count == 0`.
In CPython a goto ladder is a **positive** signal — the cleanup sequence is written once. Its
*absence* with several owned locals means the cleanup was hand-copied at every exit.

| Rank | Function | Lines | Region | Ladder | Owned | Returns w/ cleanup | Score |
|------|----------|-------|--------|--------|-------|--------------------|-------|
| 1 | `reduce_newobj` | 8221–8316 | R26 pickle | **27** | 3 | 9 | 1.7 |
| 2 | `object_new` | 7405–7473 | **R24 `object.__new__`** | **24** | 4 | 6 | 1.2 |
| 3 | `type_get_annotations` | 2167–2236 | R7 getsets | 18 | 3 | 6 | 1.3 |
| 4 | `_PyObject_GetNewArguments` | 8089–8179 | R26 pickle | 14 | 2 | 7 | 1.4 |
| 5 | `merge_class_dict` | 7077–7119 | R23 type methods | 12 | 3 | 4 | 1.2 |
| 6 | `type_add_method` | 8600–8660 | **R28 method install** | 12 | 4 | 3 | 1.2 |
| 7 | `type_new_init_subclass` | 12321–12339 | R35 `__init_subclass__` | 9 | 3 | 3 | 1.0 |
| 8 | `super_init_without_args` | 12813–12893 | R37 `super` | 8 | 2 | 4 | 1.4 |
| 9 | `type_set_annotations` | 2238–2298 | R7 getsets | 8 | 1 | 8 | 1.2 |
| 10 | `type_get_annotate` | 2088–2121 | R7 getsets | 8 | 2 | 4 | 1.0 |

**The two metrics disagree, and the disagreement is the point.** Every one of the top-3
complexity hotspots scores **0** on the ladder metric (`type_from_slots_or_spec` has 36 gotos;
`inherit_slots` and `update_one_slot` own no locals at all). Conversely `object_new` — squarely
on the Pass-1 surface, the canonical `object.__new__` — ranks **2nd by ladder at score 1.2**,
nowhere near the complexity hotspot list. If this run only followed the complexity ranking it
would never look at `object_new`. That is exactly the 20-of-25-at-the-floor failure mode.

---

## Inherent vs. reducible

### INHERENT — do not propose refactors

| Function | Why the complexity is the problem domain |
|----------|------------------------------------------|
| `update_one_slot` (11906) | Cyclomatic 37 / nesting 5 comes from a `do { … } while ((++p)->offset == offset)` walk over **multiple `slotdefs[]` rows sharing one struct offset**, resolving a 3-way lattice (`specific`, `generic`, `use_generic`) with four descriptor-shape special cases (wrapper descr / `tp_new_wrapper` CFunction / `__hash__ = None` / method descr). The 62-line design comment at 11844–11906 exists because this cannot be made simpler without changing the slot model. Extracting helpers would move the state, not remove it. **No recommendation.** |
| `inherit_slots` (8821) | 77 `COPYSLOT`-family macro invocations. Cyclomatic 38 is entirely `if (base->tp_X && !type->tp_X) type->tp_X = base->tp_X;` repeated per slot. Linear, flat, zero owned locals, 2 exits. The "complexity" is the size of `PyTypeObject`. **No recommendation.** |
| `inherit_special` (8741) | Same shape, flag-inheritance variant. Nesting 1. |
| `subtype_dealloc` (2718) | The trashcan / GC-untrack / base-walk / weakref / finalizer dance. Every one of the 5 exits is a documented resurrection or trashcan case. Famously delicate and famously non-refactorable. |
| `slotdefs[]` table + `slotptr`/`slotdefs_dups` (R33/R34) | Table-driven by design; the table *is* the specification. |
| `type_new_slots_bases` (4318) | Cyclomatic 18 in 30 lines is a dense flag scan over base classes; nesting 3, no allocation. |

### REDUCIBLE (but low priority — see caveat)

| Function | Shape | Note |
|----------|-------|------|
| `object_new` (7405) | Ladder 24: the abstract-methods error branch (7421–7467) hand-copies `Py_DECREF(sorted_methods)` on 4 paths and `Py_DECREF(joined)` on 2. A `goto error` ladder would write it once. | Correct as written. Rarely changed, heavily exercised. **CONSIDER at most.** |
| `reduce_newobj` (8221) | Ladder 27, 11 exits, 3 owned locals, no gotos. Highest hand-copied cleanup burden in the file. | R26 pickle, off the Pass-1 surface. |
| `type_get_annotations` / `type_set_annotations` / `type_get_annotate` (R7) | Three adjacent getsets, ladders 18/8/8. Same hand-copied shape three times. | Recently-churned region (PEP 649 era) — the one place where "reducible" and "actually worth doing" plausibly coincide. |

**Caveat on all of the above:** this is a risk map, not a cleanup plan. None of these ladders is
a defect; they are places where *verifying* the absence of a defect costs N path-checks instead
of one. Report them to the refcount agent as verification targets, not to anyone as refactors.

---

## The correlation that matters: complexity ∩ Pass-1 construction surface

### 1. `type_from_slots_or_spec` (5247–5799) — score 6.8, R17. The file's structural centre of gravity.

**553 source lines / 429 counted, cyclomatic 92, 18 switch cases, nesting 5, 36 gotos, 1 return.**
Now carries **three** public entry points: `PyType_FromSlots` (new in 3.16, 5801),
`PyType_FromMetaclass` (5807), and the `PyType_FromSpec*` family (5816/5822/5828).

**The naive reading is wrong and it matters.** "36 goto-driven error paths" sounds like the
worst possible unwind. It is structurally the *best* available shape: all 36 gotos target a
single `finally:` label (5790) and the function has exactly **one** `return` (5798). There is no
path enumeration problem. The cleanup sequence is written once:

```c
finally:
    if (PyErr_Occurred()) {
        Py_CLEAR(res);
    }
    Py_XDECREF(bases);
    PyMem_Free(tp_doc);
    Py_XDECREF(ht_name);
    PyMem_Free(_ht_tpname);
    return (PyObject*)res;
```

The five owned resources are declared together at 5256–5261 under an explicit invariant comment
(5252–5255). Ownership transfer to the type is a **single burst at 5644–5657** containing no
fallible calls, with each local NULLed immediately after handoff (`bases = NULL` 5646,
`tp_doc = NULL` 5649, `ht_name = NULL` 5653, `_ht_tpname = NULL` 5657). That burst collapses what
looks like 36 distinct states into **two**:

- **25 pre-allocation sites** (5311, 5318, 5346, 5356, 5366, 5384, 5409, 5426, 5446, 5457, 5465,
  5471, 5488, 5504, 5528, 5537, 5544, 5554, 5560, 5566, 5589, 5602, 5607, 5612, 5625) — `res` is
  NULL, the locals still own everything, cleanup is the four `XDECREF`/`Free` lines.
- **11 post-transfer sites** (5672, 5726, 5730, 5737, 5742, 5748, 5753, 5760, 5768, 5773, 5780) —
  the type owns all four, every local is NULL, and cleanup reduces to `Py_CLEAR(res)`.

I walked all 36. **The partition is clean and the ownership bookkeeping is correct.** State that
positively so downstream agents do not re-derive it.

**What is genuinely hard to verify here — two properties, both global rather than per-path:**

- **(P1) Every one of the 36 sites must leave a live exception.** The cleanup is gated on
  `if (PyErr_Occurred())` (5791), *not* on a status variable. A goto that jumps without setting
  an exception returns a partially-constructed `res` to the caller **as if it succeeded**. Six
  sites delegate the exception to a callee and are the ones to check: 5318 and 5672
  (`case Py_slot_invalid` — relies on `_PySlotIterator_Next` in `Python/slots.c` having set it),
  5488 (`PyUnicode_FromString`), 5528 (`PyTuple_Pack`), 5544 (`find_best_base`), 5602/5607/5612
  (`special_offset_from_member`). I read each and each does set an exception on its failure path
  — but this is a 36-site convention with no compiler or assert enforcing it, and the two
  `Py_slot_invalid` sites depend on a **different translation unit**. Symmetrically: a stray
  pending exception reaching 5790 on the *success* fall-through silently discards a fully valid
  type. **Point the error-path and null-safety agents at P1.**
- **(P2) The 11 post-transfer sites hand a half-built heap type to `Py_CLEAR(res)`.** That
  invokes the **metatype's** `tp_dealloc` (`type_dealloc`, 6978), which unconditionally reads
  `type->tp_base/tp_dict/tp_bases/tp_mro/tp_cache`, `et->ht_name/ht_qualname/ht_slots/
  ht_cached_keys`, `PyMem_Free((char *)type->tp_doc)` and `et->_ht_tpname`. Safety rests entirely
  on `metaclass->tp_alloc` having **zero-filled** the object at 5623. For `PyType_Type` that is
  `PyType_GenericAlloc` → `_PyType_AllocNoTrack`, which does zero. But the function **rejects a
  custom metaclass `tp_new`** (5562–5567, explicit `TypeError`) **and places no constraint
  whatsoever on a custom metaclass `tp_alloc`.** A metaclass supplying a non-zeroing `tp_alloc`
  makes all 11 post-transfer paths read uninitialized member pointers in `type_dealloc`. Note
  further that the goto at **5672** fires from inside the second-pass slot loop — *before*
  5701's `if (type->tp_dealloc == NULL) type->tp_dealloc = subtype_dealloc;` and before
  `PyType_Ready` — so it clears a type that has had arbitrary user slots applied via
  `_PySlot_heaptype_apply_field_slot` (5697) but has never been readied.
  **This is the single most specific lead in this report. Point the uninitialized-dealloc
  auditor and the refcount auditor at P2, and specifically at the asymmetry between the
  `tp_new` check at 5562 and the absent `tp_alloc` check at 5623.**

A third, narrower lead in the same function, for the memory-pattern agent: the `Py_tp_doc`
handler at 5414–5430 frees the previous buffer only in the `sl_ptr == NULL` branch (5418); the
allocating branch (5421–5428) **overwrites `tp_doc` without freeing** the prior allocation. That
leaks iff a duplicate `Py_tp_doc` slot can reach the handler. `Python/slots.c:367–402` runs a
`_PySlot_get_duplicate_handling` check and can `_PySlot_PROBLEM_REJECT`, so the leak is likely
unreachable — **but the reachability depends on `Py_tp_doc`'s per-slot duplicate policy in the
sibling file, which is outside this slice.** Worth 10 minutes from whoever owns R17.

### 2. `type_ready` (9527–9608) — score 2.0, R29. 14 gotos, and *no unwind at all*.

This is the inverse of the previous function and the more interesting structural fact. Fourteen
`goto error` sites converge on:

```c
error:
    stop_readying(type);
    return -1;
```

The label does **not** unwind. `type_ready` drives twelve sub-steps
(`type_ready_pre_checks` → `set_dict` → `set_base` → `set_type` → `set_bases` → `mro` →
`set_new` → `fill_dict` → `inherit` → `preheader` → `set_hash` → `add_subclasses` →
`managed_dict` → `post_checks`) each of which **mutates `type` in place**. On failure at step N
the type retains the effects of steps 1..N−1 with zero rollback. The real exit-path cost here is
not 14 — it is **12 partial-construction states**, and the property to verify is not "does the
label clean up" (it plainly does not, by design) but "**is every one of the 12 partial states
safely deallocatable, and by whom**".

Two answers, and they differ:

- **Heap types** (the `type_from_slots_or_spec` caller, and `type_new_impl`): rollback is
  delegated to the caller's `Py_CLEAR(res)` → `type_dealloc` → `type_dealloc_common` (6849),
  which **does** call `remove_all_subclasses(type, bases)` — so the step-12
  `type_ready_add_subclasses` side effect *is* undone. I verified this; record it as checked so
  nobody re-chases it. This is only sound under P2 above (zero-filled allocation).
- **Static types via public `PyType_Ready`** (9610): there is no dealloc. A static type failing
  at step 12 stays permanently registered in its bases' `tp_subclasses` **without**
  `Py_TPFLAGS_READY`. `PyType_Ready` re-entry (9628 tests the READY flag, which is unset) re-runs
  the whole pipeline. Re-registration is benign — `add_subclass` (9692) keys the weakref dict by
  `PyLong_FromVoidPtr((void *)type)`, so `PyDict_SetItem` overwrites idempotently — and
  `add_all_subclasses` (9725) deliberately continues past a failed base (`res = -1`, no break).
  So this is durable-but-benign residue, not a defect. **Recorded as verified-benign; do not
  spend agent budget here.**

### 3. `update_one_slot` (11906–12068) — score 3.4, R34. Inherent; one narrow property to check.

Zero gotos, zero owned locals, 3 exits — the ladder metric correctly scores it 0. The only
resource is `descr_ref`, a `_PyStackRef` acquired at 11942 and closed at 12038. I traced every
path: the `continue` at 11947 fires only when `res <= 0` (no ref acquired) and, being inside
`do { … } while ((++p)->offset == offset)`, still advances `p`; the `break` at 11963 exits only
the inner `for`. **No stackref leak.** Verified — record it.

The property that *is* hard here, and which no scanner will catch, is the interaction between
`slotdefs_dups[index]` (11954–11967) and the `specific`/`generic`/`use_generic` lattice when
several `slotdefs[]` rows share one struct offset. The blast radius is a wrong C slot silently
installed — a behavioral bug, not a crash — and the include map confirms this engine has **no
in-tree structural twin**: its siblings are *other rows of the table*, not other functions.
Nothing in the complexity ranking helps with that. **ACCEPTABLE / no action.**

Note the free-threading fork at 12048–12062 (`queue_slot_update` vs. direct `*ptr = slot_value`).
Per the calibration at the top of this report, **the complexity score must not be used as a
severity input for anything found in that fork.**

### 4. `type_new_impl` (4940–4990) — score 1.1, R16. Below every threshold; still on the surface.

5 gotos to one `error:` label, 3 returns, 1 owned local. Scores **1.1** — 6th decile. But it is
the `class` statement's construction driver and it calls `fixup_slot_dispatchers` at 4958. Its
own `type_new_*` helpers (`copy_slots` 4249 with 4 gotos, `descriptors` 4650 with 5 exits,
`set_attrs` 4801 with 11 exits, `get_bases` 4993) spread the construction across R16 in pieces
each individually too small to rank. **The `type_new` subsystem is invisible to this metric by
construction — it was decomposed into 20 small helpers, which is good engineering and which
zeroes its complexity signal.** Any agent that prioritizes off this document alone will
under-weight R16. Say so explicitly to the refcount / uninit-dealloc / null-safety agents:
**R16 (4191–5135) deserves attention proportional to its role, not to its score.**

### 5. `object_new` (7405–7473) — ladder 24, score 1.2, R24. The metric disagreement, concretely.

Rank 2 by cleanup ladder, unranked by complexity. Four owned locals (`abstract_methods`,
`sorted_methods`, `comma_w_quotes_sep`, `joined`), 11 exits, no gotos, cleanup hand-copied at
every one. I read it: **it is correct.** But it is the canonical `object.__new__`, it is the
Pass-1 excess-args / bypass rule (R24, 7338–7520), and the *cost of confirming* it is correct is
11 path-checks against 4 locals rather than one label. That cost is what the ladder metric
prices, and it is why the ladder table belongs in this report next to the complexity table
rather than beneath it.

---

## Error-path fan-out — the construction surface, tabulated

The requested number: distinct exit paths and goto labels for the Pass-1 construction functions.
This is the verification cost of the unwind.

| Function | Lines | Exits | Gotos | Labels | Owned | Partial-construction states to verify | Verdict on unwind cost |
|----------|-------|-------|-------|--------|-------|----------------------------------------|------------------------|
| `type_from_slots_or_spec` | 5247–5799 | **1** | **36** | 1 (`finally`) | 5 | **2** (pre-transfer / post-transfer, split at 5644–5657) | **Low structural cost, two global invariants (P1, P2).** Single-exit; the 36 gotos are a strength. |
| `type_ready` | 9527–9608 | 2 | 14 | 1 (`error`) | 0 | **12** (one per sub-step; no rollback) | **Cost lives in the 12 mutations, not the 14 gotos.** Rollback delegated to caller. |
| `type_new_impl` | 4940–4990 | 3 | 5 | 1 (`error`) | 1 | 5 | Low. Well-decomposed. |
| `type_new` | 5064–5116 | 5 | 0 | — | 0 | 5 | Low — thin wrapper. |
| `type_new_set_attrs` | 4801–4863 | **11** | 0 | — | 0 | 11 | Borrowed refs only; no owned locals. Low. |
| `type_new_copy_slots` | 4249–4315 | 4 | 4 | 1 (`error`) | 1 | 4 | Has a ladder. Good shape. |
| `type_new_descriptors` | 4650–4727 | 5 | 0 | — | 0 | 5 | Low. |
| `object_new` | 7405–7473 | **11** | 0 | — | **4** | 11 | **Highest hand-copied unwind cost on the Pass-1 surface (ladder 24).** |
| `tp_new_wrapper` | 10412–10489 | 9 | 0 | — | 2 | 9 | Ladder 4. The `__new__`-bypass safety check. Moderate. |
| `add_operators` | 12456–12502 | 5 | 0 | — | 1 | 5 | Ladder 1. Low. |
| `type_add_method` | 8600–8660 | ~3 | 0 | — | **4** | 3 | Ladder 12 — 4 owned locals, no label. |
| `type_ready_set_new` | 9414–9463 | 2 | 0 | — | 0 | 2 | Low. |
| `update_one_slot` | 11906–12068 | 3 | 0 | — | 0 | 3 | Low — 1 stackref, verified closed on all paths. |
| `fixup_slot_dispatchers` | 12131–12138 | 0 | 0 | — | 0 | — | 4 lines. Trivial. |
| `slot_tp_new` / `slot_tp_init` | 11196 / 11179 | 2 / 2 | 0 | — | 0 / 1 | 2 | Trivial. |
| `PyType_Ready` | 9610–9636 | 2 | 0 | — | 0 | 2 | Trivial wrapper over the lock. |

**Reading of the table.** The construction surface's unwind cost is **not** where the line counts
suggest. `type_from_slots_or_spec` is 553 lines and costs *two* states to verify. `type_ready` is
63 lines and costs *twelve*. `object_new` is 69 lines, scores 1.2, and costs *eleven paths against
four owned locals* — the worst ratio on the surface. Function length and unwind cost are close to
uncorrelated in this file, because CPython's authors used a `goto` ladder wherever the function
got long, and skipped it wherever the function stayed short.

---

## Where to point the six safety agents

Ordered by expected yield, with the specific property each should verify.

1. **uninitialized-dealloc auditor → `type_from_slots_or_spec` 5623 + the 11 post-transfer
   gotos.** Property: `Py_CLEAR(res)` at 5792 runs the metatype `tp_dealloc` on a half-built
   heap type. Its safety depends on `metaclass->tp_alloc` zero-filling. The function rejects a
   custom metaclass `tp_new` (5562) but imposes **no** constraint on a custom `tp_alloc`.
   Sharpest single lead in the slice.
2. **error-path analyzer → the same function, invariant P1.** Property: cleanup is gated on
   `PyErr_Occurred()` rather than a status flag, so all 36 gotos must leave a live exception.
   The two `case Py_slot_invalid` sites (5318, 5672) delegate that obligation to
   `_PySlotIterator_Next` in `Python/slots.c` — **a different translation unit, outside this
   slice.** Cross-file check required.
3. **memory-pattern analyzer → `Py_tp_doc` handler 5414–5430.** Property: the allocating branch
   overwrites `tp_doc` without freeing the prior buffer. Reachable only if a duplicate
   `Py_tp_doc` slot survives `_PySlot_get_duplicate_handling` (`Python/slots.c:367`).
4. **refcount auditor → `object_new` (7405), `reduce_newobj` (8221), `_PyObject_GetNewArguments`
   (8089), `type_add_method` (8600), `type_get_annotations` (2167).** Property: hand-copied
   cleanup at 6–9 exits against 2–4 owned locals, no goto label. Ladders 24 / 27 / 14 / 12 / 18.
   These are the goto-free functions the ladder metric selects and the complexity metric misses.
5. **null-safety scanner → R16 `type_new` subsystem (4191–5135) in full**, notwithstanding its
   low scores. The include map's §4 negative result is the reason: **Argument Clinic covers none
   of the construction surface.** Every argument-count, keyword and type check in `type_new`,
   `type_call`, `type_init`, `object_new`, `object_init`, `tp_new_wrapper` and `super_init` is
   hand-rolled. Score 1.0–1.4 across the board means the complexity metric offers this agent
   **no** guidance; the region map must drive it instead.
6. **FT / lock-discipline agents → R2 (42–227), R13 (3836–3941), R6 (971–1481), R19
   (6140–6452), and the `queue_slot_update` fork at 12048–12062.** **Ignore every number in this
   report when triaging those.** Per the calibration: for the free-threading class the score
   inverts, and `descr_get_qualname` (4 lines, cyclomatic 2, rank 257) carried a confirmed
   free-threading race. Region membership, not score, is the signal here.

---

## Complexity patterns across the file

- **Bimodal by design.** Average function is 17.9 lines with cyclomatic 4.9; the top function is
  429 counted lines with cyclomatic 92. The file is ~400 small functions plus a handful of
  irreducible engines. Only 9 functions clear the top-2% threshold of 1.7, and only 3 clear 2.4.
  This is a well-factored file with three deliberate monoliths, not a sprawling one.
- **Decomposition suppresses the signal exactly where the risk is.** R29 `PyType_Ready` was split
  into 14 named `type_ready_*` steps and R16 `type_new` into ~20 named `type_new_*` helpers.
  Both are excellent engineering and both drive their per-function scores to ~1.0–1.4 while the
  *aggregate* invariant burden is unchanged and simply relocated to the driver. A complexity
  ranking systematically under-weights well-decomposed subsystems. That is a property of the
  metric, not of the code.
- **`goto` correlates with *lower* risk here, as the counter-metric predicts.** The three
  functions with real goto ladders (`type_from_slots_or_spec` 36, `type_ready` 14,
  `_Py_type_getattro_stackref` 6) each write their cleanup once and target exactly one label.
  The functions with hand-copied cleanup (`object_new` 24, `reduce_newobj` 27) have **zero**
  gotos and sit at the score floor.
- **Nesting is well controlled.** Max nesting across 423 functions is 5, hit by only
  `type_from_slots_or_spec`, `update_one_slot` and `check_duplicates`. No deep-nesting problem
  exists in this file.
- **The R17/R33/R34 axis is the file's true complexity spine**: type creation (6.8) →
  slot inheritance (3.7) → slot wiring (3.4). All three are Pass-1, all three are inherent, and
  the include map confirms the slot-wiring engine has no in-tree twin — a bug there has no
  structural sibling to hunt, only other `slotdefs[]` rows.
- **New-in-3.16 seam.** `type_from_slots_or_spec` gained a third entry point (`PyType_FromSlots`,
  5801) and now drives a slot iterator implemented in `Python/slots.c`. Two of its 36 error paths
  (5318, 5672) delegate their exception-setting obligation across that seam. **The highest-value
  cross-file check in this slice, and it is outside the slice.**

---

## Classification

| Verdict | Item |
|---------|------|
| **ACCEPTABLE** | `update_one_slot`, `inherit_slots`, `inherit_special`, `subtype_dealloc`, `slotdefs[]`/`slotptr`/`slotdefs_dups`, `type_new_slots_bases` — inherent domain complexity. Explicitly **no refactor recommended**. |
| **CONSIDER** | `type_from_slots_or_spec` invariant **P2** (no `tp_alloc` constraint at 5623 while `tp_new` is checked at 5562) — hand to uninitialized-dealloc auditor for a verdict. |
| **CONSIDER** | `type_from_slots_or_spec` invariant **P1** (36-site "must set an exception" convention, unenforced, 2 sites delegating cross-TU) — hand to error-path analyzer. |
| **CONSIDER** | `Py_tp_doc` overwrite-without-free at 5421–5428, gated on duplicate-slot reachability in `Python/slots.c`. |
| **CONSIDER** | Ladder cluster: `object_new` (24), `reduce_newobj` (27), `type_get_annotations` (18), `_PyObject_GetNewArguments` (14), `type_add_method` (12) — verification cost, not confirmed defects. |
| **POLICY** | R16 `type_new` and R29 `PyType_Ready` are invisible to complexity ranking because they are well-decomposed. Agent routing for this slice must be driven by the region map, not by this document's scores. |
| **Verified-benign — do not re-chase** | `update_one_slot` stackref closed on all paths. `type_dealloc_common` does call `remove_all_subclasses`, so `type_ready`'s step-12 side effect is undone for heap types. `add_subclass` is idempotent (`PyDict_SetItem` keyed by type pointer), so re-entrant `PyType_Ready` on a static type does not duplicate registrations. `type_from_slots_or_spec`'s 5-resource ownership transfer at 5644–5657 is correct and fallible-call-free. |

---

## Method and limits

Metrics from `measure_c_complexity.py` at
`/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts/`, run on
`/home/danzin/projects/cpython/Objects/typeobject.c`. Exit-path and goto-label counts were
computed separately over comment-stripped function bodies and are not part of the script's
output. Region attribution is from `preflight/include_map.md` §3.

Coverage is 99.1%; 4 signatures unparsed (probably the `SLOT*` macro dispatchers in R32). The
ranking is over 423 of ~427 functions.

**What this document cannot do.** It cannot tell you a function is safe. On the measured
`Objects/` sample 20 of 25 confirmed defect-bearing functions scored at the floor. Every function
in this file that is *not* named above remains exactly as suspect as it was before this analysis
ran. The value delivered here is a reading order and an error-path fan-out map — nothing in it
is a clearance.
