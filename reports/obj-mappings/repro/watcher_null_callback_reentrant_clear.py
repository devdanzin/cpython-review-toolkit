"""Unguarded watcher dispatch: a re-entrant ClearWatcher makes CPython call NULL.

Objects/funcobject.c:29-50   notify_func_watchers
    uint8_t bits = interp->active_func_watchers;   // :33  SNAPSHOT
    while (bits) {
        if (bits & 1) {
            PyFunction_WatchCallback cb = interp->func_watchers[i];  // :38
            // callback must be non-null if the watcher bit is set
            assert(cb != NULL);                                     // :40
            if (cb(event, func, new_value) < 0) {                   // :41
    ...
`bits` is snapshotted before the loop.  PyFunction_ClearWatcher (:111-112)
NULLs interp->func_watchers[id] and clears the interp bit -- but not the
loop's local copy.  So a callback that clears a HIGHER-numbered watcher makes
the next iteration load NULL and call it.

Objects/codeobject.c:46-54 (notify_code_watchers) and Python/context.c:126-133
(notify_context_watchers) have the identical shape, with the identical comment.
Objects/dictobject.c:8309 and Objects/typeobject.c:1222 write `if (cb && ...)`.

Reachable from pure Python here only because _testcapi's function-watcher
callback calls a Python function; in the wild the same window is opened by
PyErr_FormatUnraisable -> sys.unraisablehook (arbitrary Python) on the
callback's own -1 return, and by any other thread on a free-threaded build.
"""

import sys
import _testcapi

fired = []


def clearer(event, func, new_value):
    # Runs as watcher #0.  Clear watcher #1 from inside the dispatch loop.
    if not fired:
        fired.append(event)
        _testcapi.clear_func_watcher(1)


def noop(event, func, new_value):
    pass


w0 = _testcapi.add_func_watcher(clearer)
w1 = _testcapi.add_func_watcher(noop)
print("watchers: %r %r" % (w0, w1), flush=True)
assert w0 == 0 and w1 == 1, "expected ids 0 and 1"

print("creating a function to emit PyFunction_EVENT_CREATE", flush=True)
code = compile("def _trigger():\n    pass\n", "<trigger>", "exec")
exec(code, {})
print("SURVIVED fired=%r" % (fired,), flush=True)
sys.exit(0)
