# CPY-0015 setup phase -- runs UNARMED, before set_nomemory.
# Nothing here may warm the path under test; the 2-tuple freelist is drained
# from inside the armed payload instead (see CPY-0015_dictiter_new_payload.py
# for why draining here does not survive the arming call).
d = {"a": 1, "b": 2}
v = d.items()
