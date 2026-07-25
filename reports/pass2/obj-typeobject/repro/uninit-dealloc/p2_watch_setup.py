# Unarmed setup for the watcher/version payload.
import _testcapi


class W1:
    pass


class W2(W1):
    pass


wid = _testcapi.add_type_watcher(0)
_testcapi.watch_type(wid, W1)
_testcapi.watch_type(wid, W2)
W1.a = 1  # warm the callback path unarmed
