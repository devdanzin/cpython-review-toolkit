# Reply draft — gh-154710, responding to BHUVANSH855

Status: draft for review, not yet posted. Paragraphs are deliberately unwrapped (one line each) so GitHub reflows them.

---

@BHUVANSH855 Thank you! This is a really good piece of investigation, and I want to say why before I get to the details: you checked how the *sibling* watcher implementations behave before writing a patch, and you built a prototype to test whether your explanation of the cause was actually true. That order (understand, then verify, then patch) is the habit that matters most in this codebase, and plenty of experienced contributors skip it. You also found something I had not stated clearly, which I'll come to below.

Let me go through your findings, then answer your question directly.

## Your two factual points both check out

**Immediate `PyErr_FormatUnraisable()` is intended.** You're right, and the code says so in as many words — `Objects/typeobject.c:1219-1220`:

```c
// Note that PyErr_FormatUnraisable is potentially re-entrant
// and the watcher callback might be too.
```

**Your localization of the immediate cause is sharper than my original report.** I framed this around the watcher error-reporting mechanism; you've narrowed it to a specific stale-state consumption, which is more useful. In `_PyDict_DelItem_KnownHash_LockHeld`:

```c
ix = _Py_dict_lookup(mp, key, hash, &old_value);           /* :3030  compute  */
...
_PyDict_NotifyEvent(PyDict_EVENT_DELETED, mp, key, NULL);  /* :3038  runs user Python */
delitem_common(mp, hash, ix, old_value);                   /* :3039  consume the stale pair */
```

That's the defect in three lines, and it's a shape worth learning to recognise: read some state, call into user Python, then keep using the state as if nothing happened. It's the same shape as gh-154709, and I've been finding it repeatedly elsewhere in the C code. Once you've seen it a few times you start spotting it by eye.

## One nuance I'd add to your conclusion

You concluded the issue is dict-specific "rather than the generic watcher error-reporting mechanism." I'd put it slightly differently: the other four watchers get away with immediate reporting because **none of them are holding a stale index across it**. Dict is. So it isn't quite that the error mechanism is unrelated, it's that dict is the one caller for which that mechanism's (documented, intended) re-entrancy is unsafe.

That distinction matters because it means there are two places you could fix this, and both work.

## The documented constraint that answers half your question

You asked whether the preferred direction is to rework the dict notification/mutation sequence. I think that one is actually off the table, and it's worth knowing why — `Doc/c-api/dict.rst` makes it a promise:

> Callbacks occur before the notified modification to *dict* takes place, so the prior state of *dict* can be inspected.

Moving the notify after the mutation would break that guarantee, so it can't be done as a bugfix. Good news for you: that's one of three options eliminated for free.

While you're in that file, it's worth reading the whole callback contract, because there's a real tension in it that I think is the root of this issue. The same document says callbacks

> may not modify *dict* or otherwise cause code execution in the callback, as it could modify the dict as a side effect

and also says that if the callback sets an exception,

> it must return `-1`; this exception will be printed as an unraisable exception

So a callback that does exactly what it's told, i.e. returns `-1` on failure, causes CPython itself to execute arbitrary Python through `sys.unraisablehook`, at precisely the moment the contract says code must not run. A conforming callback can't avoid it. That's why I don't think this is only a dict-ordering bug: the contract as written can't be satisfied by anyone.

## Two suggestions on your prototype

**The `KeyError` may not be forced.** If the key has already been removed by the time you revalidate, then the postcondition of `del d[k]` is satisfied, the key is gone. Returning `0` rather than raising is arguably more defensible and avoids the visible behaviour change you were rightly worried about. (The case where the watcher *replaced* the value instead of deleting it is genuinely murkier, and probably does need to raise or re-delete.)

**There's a third option worth considering.** Rather than revalidating, you could defer the report: save the exception, let `delitem_common` finish, then call `PyErr_FormatUnraisable`. That preserves both documented guarantees and changes no Python-visible behaviour at all, which makes it the narrowest possible fix. The `typeobject.c` comment you found documents that `PyErr_FormatUnraisable` *is* re-entrant. I'd read that as a warning to callers to be careful around it, rather than evidence that every existing caller is already safe.

I'm not certain that's the right answer either. It's just the option I'd want on the table.

## Why I'm not going to pick a direction, and why that's good news for you

You asked which direction is preferred, and the honest answer is that I can't authorise one. All of the remaining options change either a documented C-API guarantee or Python-visible behaviour, and the real question underneath is *which of two documented guarantees should yield*. That's a call for the people who own the watcher API, not for me as the reporter, and not for either of us to settle in an issue thread. Both of those doc sentences trace back to the original dict watcher work (`a4b77948879`, gh-91052) and its follow-up (`1e703a47334`, gh-102381), by Carl Meyer.

So I'd suggest we get core-dev input before more code gets written. I'm happy to open a post on [discuss.python.org](https://discuss.python.org/) in the C API category laying out the three options and their tradeoffs (the doc contradiction, the ordering guarantee, the revalidate-vs-defer choice, and the `KeyError`-vs-`0` question) so the API owners and other interested folks can weigh in somewhere more visible than this thread. I'll link it here when I do, and I'd like to credit your investigation in it, since the localization and the prototype result are both yours.

**Concretely, I'd hold off on extending the prototype to the other notification sites for now.** Not because the work isn't good — because that's exactly the effort that gets thrown away if the chosen direction turns out to be deferring the report rather than revalidating. Better to spend a week waiting than a week rewriting.

If you want something to dig into while that settles: the same read-then-call-user-code-then-use pattern is worth grepping for around the other 13 `_PyDict_NotifyEvent` call sites. Knowing which of them hold state across the notify — and which don't — would be genuinely useful input to the discussion, whichever direction wins, and it's analysis rather than code, so none of it is wasted.

Thanks again for picking this up and for asking before charging ahead. That instinct is the right one.

*Investigation and draft assisted by Claude Code.*
