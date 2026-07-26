# Discourse post draft — C API category

Status: draft for review, not yet posted. Paragraphs unwrapped (one line each).

**Suggested title:** Dict watchers: the documented callback contract cannot be satisfied (gh-154710)

**Category:** C API

---

`Doc/c-api/dict.rst` places three requirements on a `PyDict_WatchCallback`, and they are jointly unsatisfiable. I would like input on which one should yield, because every available fix changes either a documented guarantee or Python-visible behaviour, and that is not a call I can make as the reporter.

The three, all from the `PyDict_WatchCallback` entry:

1. Callbacks "may not modify *dict* or otherwise cause code execution in the callback, as it could modify the dict as a side effect".
2. "Callbacks occur before the notified modification to *dict* takes place, so the prior state of *dict* can be inspected."
3. If the callback sets an exception "it must return `-1`; this exception will be printed as an unraisable exception".

A callback that obeys (1) and signals failure as (3) instructs causes CPython itself to run arbitrary Python: `_PyDict_SendEvent` calls `PyErr_FormatUnraisable`, which reaches `sys.unraisablehook`. By (2) that happens while the dict is mid-mutation. The callback has done nothing wrong; the interpreter supplies the code execution that (1) prohibits.

## This is not a single-site bug

Because of (2), every notify site computes state, then calls out, then consumes that state. There are 14 `_PyDict_NotifyEvent` sites in `Objects/dictobject.c`. Five have distinct, reproduced memory-safety consequences on `main`, from pure Python with no `_testcapi`:

| site | state captured before the notify | consequence |
|---|---|---|
| `insert_combined_dict:1917` | the resize decision at `:1910` (`dk_usable <= 0`) | `dk_usable` consumed by the callback; the entry write at `:1927` runs past the end. ASan heap-buffer-overflow WRITE |
| `dict_popitem_impl:5051` | `ep0` at `:5043`, index `i` at `:5044-5047`, `key` at `:5050` | callback clears the entry; `value = ep0[i].me_value` at `:5053` is a raw C `NULL` that goes into the returned tuple |
| `_PyDict_DelItem_KnownHash_LockHeld:3038` | `ix` and `old_value` at `:3030` | `delitem_common` consumes the stale pair; `Py_DECREF(NULL)` |
| `clear_lock_held:3136` | the keys object | ASan use-after-free at `dictkeys_decref:496` |
| `insert_to_emptydict:2103` | emptiness | `ma_used` desync; a C `NULL` reaches a Python list and segfaults in `list_sort_impl` |

The remaining nine either hold nothing across the call or were not reproduced. I am happy to publish the full per-site breakdown if it is useful.

## Three options, and what each costs

**A. Notify after the mutation.** Removes the stale-state class outright. Breaks (2), which is a documented guarantee that the callback can inspect prior state, so it cannot land as a bugfix.

**B. Revalidate after the notify.** Keeps (1)-(3) as written. Costs a lookup per mutation on watched dicts and changes Python-visible behaviour at each site: after a callback has already removed the key, what should `del d[k]` do? A prototype in gh-154710 raises `KeyError`.

**C. Defer the unraisable report.** Save the exception, complete the mutation, then call `PyErr_FormatUnraisable`. Preserves all three requirements and changes no Python-visible behaviour, so it is the narrowest option. It does contradict the apparent convention that watchers report immediately, and `Objects/typeobject.c:1219-1220` documents that intent:

```c
// Note that PyErr_FormatUnraisable is potentially re-entrant
// and the watcher callback might be too.
```

Worth noting that the type, code, function and context watchers are safe under immediate reporting only because none of them hold a stale index across it. Dict does. So C is not obviously wrong for dict just because immediate reporting is right elsewhere; the comment reads to me as a warning to callers rather than a guarantee about them.

C does not close everything: a callback that deliberately violates (1) still corrupts state. It closes the case where a *conforming* callback does.

## The sub-question, if B

If the entry is already gone when we revalidate, the postcondition of `del d[k]` is satisfied. Returning `0` avoids a visible behaviour change; `KeyError` reports a mutation that did happen, just not by us. The value-replaced case is murkier than the key-removed case. I lean toward `0`, without confidence.

## What I am asking

Which of (1), (2) or (3) should yield, and is C acceptable as the bugfix with a doc clarification, or is B wanted despite the behaviour change? Once that is settled the implementation is mechanical across the affected sites, and there is a contributor on gh-154710 ready to do it. I have asked them to hold off until there is a direction, so as not to write the wrong patch fourteen times.

Secondary: (1) is currently unenforceable and, as shown above, unsatisfiable via the mandated error path. If the intent is that violating it is undefined behaviour, saying so explicitly would help, and would reframe several of these crashes as documented consequences rather than bugs. I would rather know that than keep filing them.

Prior art: the guarantees in (2) and (1) come from `a4b77948879` (gh-91052, the original watcher API) and `1e703a47334` (gh-102381). gh-154710 has the reproducer and the ASan traces. gh-154709 is a structurally identical stale-index bug in `reversed(dict)` with no watcher involved, which suggests the read-then-call-user-code-then-use shape is worth a look beyond the watcher API.

*Investigation and draft assisted by Claude Code.*
