import _testcapi, gc, sys
for variant, what in ((0, "repeated Py_tp_doc"), (1, "repeated Py_tp_members")):
    try:
        cls = _testcapi.create_type_from_repeated_slots(variant)
        print("variant %d (%s): ACCEPTED -> %r" % (variant, what, cls))
        o = cls(); gc.collect(); del o, cls; gc.collect()
    except BaseException as e:
        print("variant %d (%s): %s: %s" % (variant, what, type(e).__name__, e))
gc.collect()
print("DONE")
