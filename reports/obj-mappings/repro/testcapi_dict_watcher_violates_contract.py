"""CPython's OWN example dict watcher violates both halves of its contract.

Modules/_testcapi/watchers.c:31-70  dict_watch_callback

  case PyDict_EVENT_ADDED:
      msg = PyUnicode_FromFormat("new:%S:%S", key, new_value);   // :49
  ...
  if (PyList_Append(g_dict_watch_events, msg) < 0) {             // :64

Doc/c-api/dict.rst:582-584
  "The callback may inspect but must not modify *dict* ... Do not trigger
   Python code execution in the callback, as it could modify the dict as a
   side effect."
    -> `%S` is PyObject_Str, which runs a user __str__.  VIOLATED at :49.

Doc/c-api/dict.rst:599-603
  "There may already be a pending exception set on entry to the callback ...
   the callback may not call any other API that can set an exception unless
   it saves and clears the exception state first, and restores it before
   returning."
    -> PyUnicode_FromFormat / PyUnicode_FromString / PyList_Append all set
       exceptions and there is no PyErr_GetRaisedException/SetRaisedException
       anywhere in the function.  VIOLATED at :40-68.

This matters because CPY-0117's classification turned on whether a CONFORMING
watcher can reach the notify-window bugs.  The reference implementation that
ships in CPython's own test-support module is not conforming.
"""

import sys
import _testcapi

ran = []


class Chatty:
    def __str__(self):
        ran.append("__str__")
        return "chatty"

    def __repr__(self):
        return "Chatty"


wid = _testcapi.add_dict_watcher(0)   # kind 0 -> dict_watch_callback
d = {}
_testcapi.watch_dict(wid, d)

# PyDict_EVENT_ADDED -> PyUnicode_FromFormat("new:%S:%S", key, new_value)
d["k"] = Chatty()

events = _testcapi.get_dict_watcher_events()
_testcapi.unwatch_dict(wid, d)
_testcapi.clear_dict_watcher(wid)

print("events   : %r" % (events,))
print("user code ran inside the callback: %s" % ("YES" if ran else "no"))
print("half 1 (Doc/c-api/dict.rst:583-584 'Do not trigger Python code "
      "execution in the callback'): %s" % ("VIOLATED" if ran else "ok"))
sys.exit(0)
