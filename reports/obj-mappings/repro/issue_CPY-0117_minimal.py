import sys, _testcapi

d = {}
for i in range(200):
    d["k%d" % i] = i

fired = []
def hook(unraisable):
    if fired:
        return
    fired.append(1)
    d.clear()                       # re-enter the dict being mutated

sys.unraisablehook = hook
wid = _testcapi.add_dict_watcher(1) # installs dict_watch_callback_error
_testcapi.watch_dict(wid, d)

del d["k100"]                       # notify -> -1 -> unraisable -> hook -> clear()

print("len now %d" % len(d))
