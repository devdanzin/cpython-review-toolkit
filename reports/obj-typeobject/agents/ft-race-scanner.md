# ft-race-scanner — `Objects/typeobject.c` (pass 1, construction / slot wiring)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope:** `Objects/typeobject.c` only, 13,068 lines, 417 functions scanned.
**Mode:** INFORMED (briefing FP taxonomy applied; see "FP classes obeyed").
**Repro builds:** `~/projects/python_build_matrix/builds/debug-ft-nojit-tsan` and
`debug-ft-nojit`, both at `a1d580430c8`. **`Objects/typeobject.c` is byte-identical
between `a1d580430c8` and the target ref `4f3be1b5777`** (`git diff --stat` empty),
so every line number quoted from a TSan stack transfers to the target ref unchanged.

---

## Summary

| class | count | verdict |
|---|---|---|
| T1 `guarded_writer_unguarded_reader` | 1 | **1 FALSE POSITIVE** |
| T2 `lazy_init_no_critical_section` | 1 | **1 FALSE POSITIVE** |
| **Found by reading, not by the scanner** | **2** | **1 FIX (reproduced, crash), 1 CONSIDER** |

Both scanner candidates are false positives. Both fall into FP classes the briefing
already names; neither required novel reasoning to dismiss. The scanner produced
**0 true positives out of 2** in this slice.

The value in this pass came from reading the free-threading structures the scanner
does not model at all: the deferred slot-update queue (3809–3941) and its
interaction with `fixup_slot_dispatchers` (12132). That reading produced a
**reproduced, free-threading-only heap corruption + SIGSEGV** from a nine-line pure
Python script, on the plain (non-sanitizer) FT debug build.

---

## Candidate 1 — `is_readying:518` (`state->readying`) — **ACCEPTABLE / FALSE POSITIVE**

**Scanner claim:** `managed_static_type_state.readying` is written under a critical
section at `start_readying:489` and read plainly at `is_readying:518`.

**Verdict: false positive, on two independent grounds.**

### (a) The writers *are* synchronised, but not the way the scanner thinks

`start_readying:489` and `stop_readying:504` contain **no critical section at all**
in their bodies — the scanner's `guarded_twin` citation is not literally true. What
*is* true is that both are called only from `type_ready` (`:9533`, `:9600`, `:9606`),
whose first statement is:

```c
static int
type_ready(PyTypeObject *type, int initial)
{
    ASSERT_TYPE_LOCK_HELD();          // :9530
```

and whose two entry points both wrap it:

- `_PyStaticType_InitBuiltin` / `_PyStaticType_InitForExtension` shared helper,
  `:9669` — `BEGIN_TYPE_LOCK(); res = type_ready(self, initial); END_TYPE_LOCK();`
- `PyType_Ready`, `:9627` — same shape.

`BEGIN_TYPE_LOCK()` is `Py_BEGIN_CRITICAL_SECTION_MUTEX(&interp->types.mutex)`
(`:78–80`). So the writers are genuinely serialised — the scanner reached the right
conclusion about the *writer* side by an inference it cannot actually justify from
the tokens in `start_readying`'s body (see "Toolkit assessment" below).

### (b) The unguarded read at :518 is not concurrently reachable

`is_readying` has exactly three callers:

| site | context | status |
|---|---|---|
| `:881` `_PyType_CheckConsistency` | debug-assert only | briefing FP class: *"assert-only paths"* |
| `:9532` `type_ready` | `ASSERT_TYPE_LOCK_HELD()` | lock held |
| `:6156` `find_name_in_mro` | one of two call paths is unlocked (`:6365`) | analysed below |

Line 518 is reached only on the `_Py_TPFLAGS_STATIC_BUILTIN` arm. For the `:6156`
reader to hit it, a **static builtin** type must have `lookup_tp_mro(type) == NULL`,
i.e. be mid-`type_ready` (mro is set at `type_ready_mro:9561`). That state exists only:

- inside `_PyStaticType_InitBuiltin`, which runs during `Py_Initialize` /
  `Py_NewInterpreter` before any other thread has a tstate for that interpreter; or
- inside `_PyStaticType_InitForExtension`, whose single call site in the whole tree
  is `Modules/_datetimemodule.c:7531`, reached under the per-module import lock, on
  a type not yet reachable from Python.

Crucially, `managed_static_type_state` is **per-interpreter**
(`interp->types.builtins.initialized[index]`, `interp->types.for_extensions.initialized[index]`),
so two interpreters readying the same static type touch two different `state` objects
and cannot race at all.

Even granting the race, `readying` is an `int` in
`Include/internal/pycore_interp_structs.h:583` — no tearing on any supported
platform — and the worst observable consequence is one spurious extra
`PyType_Ready()` call from `find_name_in_mro`, which is idempotent
(`PyType_Ready` returns 0 immediately if `Py_TPFLAGS_READY` is set, `:9613`).

**Guarded twin (the real one):** `type_ready:9530`'s `ASSERT_TYPE_LOCK_HELD()` +
the `BEGIN_TYPE_LOCK()` at `:9627` / `:9669`. Nothing to fix.

**Sibling hunt:** the non-static-builtin arm at `:520` reads
`type->tp_flags & Py_TPFLAGS_READYING`. That is a plain `tp_flags` read, which the
file's own architecture comment sanctions explicitly (`:73–77`): *"The slots and
flags are read in many places without holding a lock and without atomics."*
Same class. ACCEPTABLE.

---

## Candidate 2 — `type_from_slots_or_spec:5701` (`type->tp_dealloc`) — **ACCEPTABLE / FALSE POSITIVE**

**Scanner claim:** lazy init of `type->tp_dealloc` guarded only by a NULL check;
two threads calling `PyType_FromSpec` concurrently both observe NULL and both store.

**Verdict: false positive — textbook pre-publication write.** This is the scanner's
*own* documented suppression class (`Pre-publication plain writes — a store into an
object this thread just allocated`) failing to fire.

```c
res = (PyHeapTypeObject*)metaclass->tp_alloc(metaclass, nmembers);   // :5623
...
type = &res->ht_type;                                                // :5628
...
if (type->tp_dealloc == NULL) {                                      // :5701
    type->tp_dealloc = subtype_dealloc;                              // :5705
}
...
if (PyType_Ready(type) < 0) { ... }                                  // :5725
...
((PyObject*)type)->ob_flags |= _Py_TYPE_REVEALED_FLAG;               // :5787
```

The premise in the task brief — *"two threads calling `PyType_FromSpec` concurrently
on the same spec"* — does not produce sharing. `spec` is **read-only input**; each
call allocates its own `PyHeapTypeObject` at `:5623` and returns a **distinct** type
object. There is no shared `tp_dealloc` for the two threads to contend over. The
field being lazily initialised belongs to memory this thread allocated 78 lines
earlier and has not published to anything. `PyType_Ready` has not even run yet.

Consequence of the hypothetical race: none. Duplicate work: no (different objects).
Torn pointer: no. Leak: no.

**Why the scanner's own suppression missed it:** the allocation result is bound to
`res`, and the flagged field is reached through a *different* local,
`type = &res->ht_type` (`:5628`) — an interior-pointer alias the pre-publication
heuristic cannot follow. Concrete tuning proposal below.

**Guarded twin:** none needed. The correct comparison is the *file's* own convention,
`ASSERT_WORLD_STOPPED_OR_NEW_TYPE(tp)` (`:112–113`), which encodes exactly this rule:
a slot write is legal iff the type is not yet revealed **or** the world is stopped.
`:5701` satisfies the first disjunct.

---

## Reading pass — the deferred slot-update queue (3809–3941)

This structure is the highest-quality free-threading code in the file and it is
**correct** for the two paths it covers. The design, from the architecture comment
at `:65–77` and `:3795–3807`:

> the global type lock protects `tp_version_tag`, `_spec_cache`, `tp_mro`, `tp_bases`,
> `tp_base`. **It does not protect updates of other type slots or `tp_flags`.**
> Instead, we either ensure those updates are done **before the type has been revealed
> to other threads**, or we only do those updates **while the stop-the-world mechanism
> is active**. The slots and flags are read in many places without holding a lock and
> without atomics.

So the answer to *"what synchronises a slot rewrite against a concurrent dispatch?"*
is **stop-the-world**, not a lock — and *"is a torn read of a slot pointer possible?"*
is **no** on any supported platform: `*ptr = slot_value` (`:12056`, `:3891`) is a
naturally-aligned pointer store. The hazard is a **stale / half-applied slot table**,
not a garbage pointer.

Two of the three slot-mutation paths implement that contract correctly:

| path | entry | mechanism | status |
|---|---|---|---|
| `type.__setattr__` | `update_slot_after_setattr:6726` | queue → `apply_type_slot_updates:3902` → `types_stop_world()` | correct |
| `__bases__` reassign | `update_all_slots:12144` | queue → `apply_type_slot_updates` | correct |
| **type creation** | **`fixup_slot_dispatchers:12132`** | **`update_one_slot(type, p, &p, NULL)` → immediate `*ptr = slot_value`** | **BROKEN** |

`apply_type_slot_updates` is genuinely careful — `type_lock_prevent_release()`
(`:3922`) stops the TYPE_LOCK critical section from being suspended while blocking on
the STW mutex, with the lock-ordering argument spelled out at `:3917–3921`. No defect
found there.

---

## FINDING 1 — [FIX] `fixup_slot_dispatchers` writes slots on an already-published type (`Objects/typeobject.c:12136`, via `type_new_impl:4958`)

**Prior art: gh-151377 (OPEN, unfixed at the target ref), PR gh-151394 (OPEN, unmerged
since 2026-06-12).** Confirming, not relitigating. **But the severity recorded upstream
is understated — see "escalation" below, which is this pass's net-new contribution.**

### What

`type_new_impl` calls `PyType_Ready(type)` at `:4953`. Inside it,
`type_ready_add_subclasses` (`:9581` → `add_subclass:9719`) inserts a weakref to the
new type into **each base's `tp_subclasses` dict**. From that instant the type is
reachable from any other thread via `Base.__subclasses__()`.

`fixup_slot_dispatchers(type)` then runs at `:4958` — *after* that linkage — and
rewrites the whole slot table with plain stores:

```c
static void
fixup_slot_dispatchers(PyTypeObject *type)
{
    assert(!PyErr_Occurred());
    for (pytype_slotdef *p = slotdefs; p->name; ) {
        update_one_slot(type, p, &p, NULL);     // :12136 — NULL == "write immediately"
    }
}
```

```c
#ifdef Py_GIL_DISABLED
    if (queued_updates != NULL) {
        if (queue_slot_update(queued_updates, type, ptr, slot_value) < 0) { ... }
    } else {
        *ptr = slot_value;                      // :12056 — no STW, no lock, no atomic
    }
#endif
```

`_Py_TYPE_REVEALED_FLAG` is not set until `:4982`, 24 lines later — so the file's own
`ASSERT_WORLD_STOPPED_OR_NEW_TYPE` debug assertion is **satisfied and yet wrong**: the
flag records the author's belief about when the type becomes visible, and the
`tp_subclasses` linkage at `:9581` falsifies it.

### Guarded twin

`update_slot_after_setattr:6726` and `update_all_slots:12144` — the two sibling
mutation paths, both of which pass a non-NULL `queued_updates` and apply under
`types_stop_world()`. `fixup_slot_dispatchers` is the one path that does not. This is
the canonical partial-guard shape: two of three accessors guarded, one not.

### Reproduction — TSan (gh-151377's own script)

`PYTHON_GIL=0`, `debug-ft-nojit-tsan`, exit 66, **29 data races**. The pair that
matters, at the target ref's exact line numbers:

```
WARNING: ThreadSanitizer: data race
  Read of size 8 at 0x7fffc0180078 by thread T1:
    #0 queue_slot_update      Objects/typeobject.c:3863:9
    #1 update_one_slot        Objects/typeobject.c:12051:13
    #2 update_slots_callback  Objects/typeobject.c:12080:13
    #3 update_subclasses      Objects/typeobject.c:12356:9
    #4 recurse_down_subclasses Objects/typeobject.c:12397:13
    #6 update_slot            Objects/typeobject.c:12124:12
    #7 update_slot_after_setattr Objects/typeobject.c:6734:9
    #8 type_setattro          Objects/typeobject.c:6835:19
  Previous write of size 8 at 0x7fffc0180078 by thread T3:
    #0 update_one_slot        Objects/typeobject.c:12056:14
    #1 fixup_slot_dispatchers Objects/typeobject.c:12136:9
    #2 type_new_impl          Objects/typeobject.c:4958:5
```

Also reported in the same run: `typeobject.c:4982:33 in type_new_impl` (the
`_Py_TYPE_REVEALED_FLAG` debug marker itself, ×2), and 12 races in
`_PyDict_Next` / `delitem_common` on the `tp_subclasses` dict.

### ESCALATION — net-new: a *dispatching* thread, and a real crash

gh-151377 reports the race as **update-vs-update** (`type_setattro` vs `type_new`) and
its evidence is a TSan warning. That understates it. The type is visible to *any*
consumer, not just to another slot-updater — and a consumer that **dispatches through
the half-written slot table** corrupts the heap.

Minimal reproducer — **no `type_setattro` anywhere**, so this is a distinct race pair
from the one in the issue:

```python
import threading
class Base: pass
STOP = False
def subclasser():
    for _ in range(20000):
        type('Sub', (Base,), {'__init__': lambda s: None})
def dispatcher():
    while not STOP:
        for c in Base.__subclasses__():      # visible after PyType_Ready, before fixup
            try: c()                          # dispatch tp_new/tp_init mid-rewrite
            except Exception: pass
```

Results (`repro_call_only.py` in the run scratchpad):

| build | flags | runs | outcome |
|---|---|---|---|
| `debug-ft-nojit` | `PYTHON_GIL=0` | 4 | **4/4 crash** — 2× SIGSEGV (139), 2× SIGABRT (134) |
| `debug-ft-nojit` (full repro incl. `repr`/`__call__`) | `PYTHON_GIL=0` | 6 | **5/6 crash** — 1× SIGSEGV, 4× SIGABRT |
| **`debug-gil-nojit`** (control) | — | 3 | **0/3 — clean** |
| `debug-ft-nojit`, dispatcher iterates `__subclasses__()` but **does not instantiate** (control) | `PYTHON_GIL=0` | 4 | **0/4 — clean** |

The two controls are the attribution: it is free-threading-only, and it requires the
*dispatch*, not merely the `tp_subclasses` iteration.

The SIGABRT is the debug allocator catching a heap **buffer overwrite**:

```
Debug memory block at address p=0x20004170060: API 'o'
    48 bytes originally requested
    The 7 pad bytes at p-7 are FORBIDDENBYTE, as expected.
    The 8 pad bytes at tail=0x20004170090 are not all FORBIDDENBYTE (0xfd):
        at tail+0: 0x1d *** OUCH
        at tail+1: 0x00 *** OUCH
        at tail+2: 0x01 *** OUCH
        at tail+3: 0x00 *** OUCH
Fatal Python error: _PyMem_DebugRawFree: bad trailing pad byte
  File ".../repro_call_only.py", line 24 in dispatcher
```

and TSan on the same script produced a hard SIGSEGV inside the destructor chain:

```
ERROR: ThreadSanitizer: SEGV on unknown address 0x000027458c59
    #2 Py_DECREF                ./Include/refcount.h:345:22
    #3 clear_inline_values      Objects/dictobject.c:7778:13
    #4 PyObject_ClearManagedDict Objects/dictobject.c:7997:13
    #5 subtype_dealloc          Objects/typeobject.c:2847:9
```

**Honest limit on attribution.** I can prove (i) the crash is FT-only, (ii) it needs
the dispatch through the freshly-visible type, and (iii) TSan flags unsynchronised
slot writes on exactly that type in exactly that window. I have **not** isolated which
individual slot store produces the out-of-bounds write, and the same script also races
`tp_subclasses` (`_PyDict_Next`, `delitem_common`, `init_tp_subclasses:712`) and
`type_set_flags:458`, any of which could contribute. What is established is that the
consequence class is *memory corruption*, not the *"stale slot value"* the upstream
issue implies.

### Fix

PR gh-151394 already has the right shape — make `fixup_slot_dispatchers` take
`BEGIN_TYPE_LOCK()`, queue into a `slot_update_t`, and apply under
`apply_type_slot_updates()`; return `int` so the OOM path propagates. It has been open
and unmerged for six weeks. **Recommended action: add this crash evidence to
gh-151377 to move the PR** — a reproduced FT-only heap corruption from nine lines of
Python is a materially stronger case than the TSan warning currently on the issue.

---

## FINDING 2 — [CONSIDER] `_PyType_GetSubclasses` holds a borrowed ref to `tp_subclasses` across iteration (`Objects/typeobject.c:783`)

Not covered by gh-151377's PR (checked the diff of gh-151394: it touches
`TYPE_IS_REVEALED`, `fixup_slot_dispatchers`, and `type_dealloc_common` — **not**
`_PyType_GetSubclasses`).

```c
PyObject *subclasses = lookup_tp_subclasses(self);  // borrowed ref     :783
if (subclasses == NULL) return list;
assert(PyDict_CheckExact(subclasses));
// The loop cannot modify tp_subclasses, there is no need
// to hold a strong reference (use a borrowed reference).                :788-789
Py_ssize_t i = 0;
while (PyDict_Next(subclasses, &i, NULL, &ref)) { ... }                  :793
```

The comment at `:788–789` is a **GIL-era invariant that free-threading invalidates**.
"The loop cannot modify `tp_subclasses`" was true when only this thread could run;
under free-threading another thread's `type_dealloc` reaches `remove_subclass`, which
ends:

```c
if (PyDict_Size(subclasses) == 0) {
    clear_tp_subclasses(base);        // :9790-9791  → Py_CLEAR(self->tp_subclasses)
}
```

That `Py_CLEAR` drops the **last owning reference** to the very dict
`_PyType_GetSubclasses` is iterating with a borrowed pointer. This is the briefing's
borrowed-ref-across-call shape, in FT form.

`type.__subclasses__()` (`type___subclasses___impl:7058`) reaches `:783` with **no
TYPE_LOCK held** — verified: the clinic block for `type.__subclasses__` carries no
`@critical_section`, and the impl body is a bare `return _PyType_GetSubclasses(self);`.

TSan corroboration from the dispatch repro (distinct sites from Finding 1):

```
  Previous read of size 8 by thread T1:
    #0 lookup_tp_subclasses   Objects/typeobject.c
    #1 _PyType_GetSubclasses  Objects/typeobject.c:783:28
    #2 type___subclasses___impl Objects/typeobject.c:7058:12
  Write of size 8 by thread T4:
    #0 init_tp_subclasses     Objects/typeobject.c:712:25
    #1 add_subclass           Objects/typeobject.c:9710:22
```

and a use-after-free flavour, where the `Py_TYPE` read at `:787` races the debug
allocator's poison-fill of freed memory:

```
  Read of size 8 by thread T2:
    #0 _Py_TYPE_impl          ./Include/object.h:234:16
    #2 _PyType_GetSubclasses  Objects/typeobject.c:787:5
  Previous write of size 8 by thread T4:
    #0 __tsan_memset
    #1 fill_mem_debug         Objects/obmalloc.c
```

**Guarded twin:** `_PyType_SetFlagsRecursive:6507` — the *other* consumer of
`_PyType_GetSubclasses` — wraps its call in `BEGIN_TYPE_LOCK()` **and**
`types_stop_world()`. That is the correctly-synchronised sibling and the fix to copy
(the lock alone suffices here; STW is for the flag write).

**Sibling hunt — same borrowed-dict shape, same file:**
- `get_subclasses_key:9754` — `lookup_tp_subclasses(base)` borrowed, then `PyDict_Next`.
- `remove_subclass:9775` — borrowed, then `PyDict_DelItem` + `PyDict_Size`.
- `recurse_down_subclasses` (~`:12380`) — named in gh-151377 already.
- `mro_hierarchy:1846` — reached under TYPE_LOCK; not a sibling.

**Classification: CONSIDER**, not FIX. The `Py_CLEAR` requires the dict to reach size 0,
which needs the base's *last* subclass to die exactly during another thread's
`__subclasses__()` — I did not isolate a repro for this specific window (the crashes
above are attributable to Finding 1's dispatch). Reporting it as a static finding with
TSan corroboration but no isolated repro.

---

## Also checked — clean

- **`type_ready_set_new:9415`.** The plain `type->tp_new = base->tp_new` (`:9452`) and
  `type->tp_new = NULL` (`:9459`) are both `initial`-only and run at `type_ready:9564`,
  which is **before** `type_ready_add_subclasses:9581`. Pre-linkage → pre-publication.
  ACCEPTABLE (briefing: *"PyType_Ready construction"*).
- **`inherit_slots` / `inherit_special` (8742–9015).** Sole caller is
  `type_ready_inherit:9321`, invoked from `type_ready:9571` — again before the
  `tp_subclasses` linkage at `:9581`, and `initial`-only. Every slot write in that
  ~270-line block is pre-publication. ACCEPTABLE. This is the single largest block of
  plain slot writes in the file and it is correctly placed.
- **`tp_version_tag` / method-cache invalidation.** Mixed atomic
  (`FT_ATOMIC_LOAD_UINT_RELAXED :54, :1154, :1246, :6356`,
  `_Py_atomic_load_uint32_acquire :6316`) and plain (`:1140, :1189, :1382, :1416,
  :1443, :6339, :6360, :6416, :6435, :6472, :9654`). Spot-checked every plain read:
  `:1140`/`:1154` `set_version_unlocked`, `:1189` `_PyType_Modified_Unlocked`,
  `:1416`/`:1443` `assign_version_tag` (`ASSERT_TYPE_LOCK_HELD` at `:1410`), `:6360`
  inside `BEGIN_TYPE_LOCK`, `:6416` `_PyType_CacheInitForSpecialization` inside
  `BEGIN_TYPE_LOCK`, `:6472` `_PyType_Validate` inside `BEGIN_TYPE_LOCK`, `:9654`
  pre-reveal static init. **All plain reads are TYPE_LOCK-held or pre-publication** —
  which is precisely what the architecture comment at `:65–68` promises the lock
  covers. Clean. Note the scanner's T1 rule reported **zero** findings here despite
  11 plain reads next to 5 atomic ones in one file — see recall gap R2.
- **`apply_type_slot_updates:3902`** lock-ordering / `type_lock_prevent_release`
  reasoning — read closely, no defect found.

---

## FP classes obeyed (briefing, not re-litigated)

- *"`PyType_Ready` construction"* — applied to `inherit_slots`, `type_ready_set_new`,
  `type_from_slots_or_spec:5701`.
- *"Teardown / assert-only paths"* — applied to `_PyType_CheckConsistency:881`.
- *"The lock is a macro"* — `BEGIN_TYPE_LOCK` / `ASSERT_TYPE_LOCK_HELD` are the
  instance here; the `#define`s are in the scanned file at `:78–80` / `:108–109`.
- *"`#ifdef Py_GIL_DISABLED` asymmetry is a promotion signal, not a suppression"* —
  applied at `:12048–12062`: the `Py_GIL_DISABLED` arm has the queue, the default arm
  writes immediately. Correctly read as *the FT arm is the fix*, which made the
  `queued_updates == NULL` caller the thing to look for.
- Catalog cross-reference: CPY-0027 (`descr_get_qualname`), CPY-0029
  (`func_get_annotation_dict`), CPY-0014 (`PyList_New`) — none in this file; the
  lazy-init-under-NULL-check shape they share was checked against `:5701` and does not
  transfer (different object per call). Confirmed in one line, per informed mode.

---

## Toolkit assessment

### Precision, this slice

`guarded_writer_unguarded_reader`: **0/1**. `lazy_init_no_critical_section`: **0/1**.
Overall **0/2**. The T1 rule ships at a documented ~1-in-7 and 0/1 is inside that
envelope — this is not evidence against the rule. The T2 miss is a genuine defect
(D1 below) because the suppression that should have caught it already exists.

### D1 — pre-publication suppression does not follow interior-pointer aliases

`type_from_slots_or_spec:5701` should have been suppressed by the scanner's own
documented *"a store into an object this thread just allocated"* rule. It was not,
because the allocation binds `res` (`:5623`) and the field is reached via
`type = &res->ht_type` (`:5628`).

**Proposal:** in the pre-publication check, when the flagged base variable `X` was
assigned `&Y-><field>` or `(T*)Y` and `Y` is an allocation result in the same
function, treat `X` as carrying `Y`'s pre-publication status. One extra
assignment-chain hop; low risk of over-suppression because it requires a
same-function allocation.

### D2 — file-local `#define` aliases for critical sections are not resolved

`_LOCK_TOKENS = ("Py_BEGIN_CRITICAL_SECTION", "PyMutex_Lock", "_PyCriticalSection")`
(`scan_ft_races.py:99`). `Objects/typeobject.c` routes **every** lock through
`BEGIN_TYPE_LOCK()` / `END_TYPE_LOCK()` (`:78–80`) and marks lock-held callees with
`ASSERT_TYPE_LOCK_HELD()` (`:108–109`). The scanner sees none of them. Consequence
here: it produced the *right* twin for the wrong reason on candidate 1, and it is
structurally unable to tell that ~30 functions in this file are lock-held.

This is the briefing's *"The lock is a macro"* class (recorded for `LOCK_WEAKREFS`),
but with an important difference: **the `#define` is in the file being scanned**, one
screen above the findings. That makes it mechanically fixable rather than a
cross-file limitation.

**Proposal (two parts, both cheap):**
1. Before scanning, resolve object-like `#define NAME <body>` in the same translation
   unit; if `<body>` contains any existing `_LOCK_TOKENS` entry, add `NAME` to the
   token set for that file. Catches `BEGIN_TYPE_LOCK`, `BEGIN_TYPE_DICT_LOCK`,
   `LOCK_WEAKREFS`, and the `Modules/_io` equivalents, in one rule.
2. Treat a function whose body contains `ASSERT_*_LOCK_HELD()` / any
   `_Py_CRITICAL_SECTION_ASSERT_*` as lock-held-by-contract, exactly like the
   `*_lock_held` naming convention already handled at `scan_ft_races.py:232`. In this
   file that is the *dominant* convention — `type_ready`, `update_one_slot`,
   `assign_version_tag`, `find_name_in_mro`'s callers and ~25 others announce their
   contract this way and none of them is named `*_lock_held`.

### R1 — recall gap: no rule models "published before the writes finish"

The real bug in this slice (Finding 1) is not lazy-init, not iterator-drop, not
atomic/plain asymmetry. It is: **an object is linked into a globally reachable
container at line A, and its fields are still being written at line B > A.** No
current `ft_class` expresses that.

**Proposal — new rule `publish_before_init_complete` (T2/high):** within one function,
flag when a call known to publish (`PyDict_SetItem` / `PyList_Append` / `PyType_Ready`
/ any `add_*` on a container reachable from a parameter) is followed by a plain store
into a field of the *same* object. In `type_new_impl` this is
`PyType_Ready(type)` at `:4953` followed by `fixup_slot_dispatchers(type)` at `:4958`.
A seed list of publishing calls keeps this tractable. This rule would have found
gh-151377 statically.

### R2 — recall gap: T1 is silent on `tp_version_tag`

11 plain reads and 5 atomic accesses of `type->tp_version_tag` in one file is the
textbook `atomic_plain_asymmetry` shape, and the scanner reported nothing. (All 11 are
in fact fine — D2 is why they'd need dismissing, not why they weren't found.) Worth
checking whether the T1 rule is skipping `->` chains through a parameter, or whether a
per-field cap suppressed it. Either way, a zero here is silence, not safety.

### What worked

The `#ifdef Py_GIL_DISABLED` promotion heuristic from the briefing did real work: at
`:12048–12062` the FT arm has the deferred queue and the else-arm writes immediately,
and reading that asymmetry as *"the FT arm is the fix, so find the caller that opts
out"* is what led straight to `fixup_slot_dispatchers`. That heuristic should stay.

---

## Artifacts

Scratchpad `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`:

| file | what |
|---|---|
| `repro_slot_race.py` | gh-151377's own script; 29 TSan races |
| `tsan_slot.err` | its TSan output (4,159 lines) |
| `repro_dispatch_race.py` | net-new dispatch-vs-fixup repro; 5/6 crash on plain FT |
| `repro_call_only.py` | **minimal**: instantiation only; 4/4 crash on plain FT |
| `repro_subclasses_only.py` | negative control: iteration without dispatch; 0/4 |
| `tsan_disp.err` | TSan output incl. the `subtype_dealloc` SIGSEGV |
| `gil_run_{1,2,3}.err` | GIL-build control; all empty (0/3 crash) |
| `ft_run_*.err`, `c_*.err`, `b_*.err` | per-run crash logs |
