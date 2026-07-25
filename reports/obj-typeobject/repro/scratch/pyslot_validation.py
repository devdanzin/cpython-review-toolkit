# Prove handle_first_run's validation is LIVE on the new PyType_FromSlots path
# (not just on the legacy PyType_FromSpec path).
import warnings, gc
import _testlimitedcapi as T

IDS = {"Py_tp_doc": 56, "Py_tp_members": 72, "Py_tp_getset": 73,
       "Py_tp_methods": 64, "Py_tp_clear": 51, "Py_tp_alloc": 47}

print("--- new PySlot path: PyType_FromSlots, NULL value per slot ---")
for name, sid in IDS.items():
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            t = T.type_from_slots_null_probe(sid) if hasattr(
                T, "type_from_slots_null_probe") else T.type_from_null_slot(sid)
        print("  %-16s(NULL) -> ACCEPTED %r" % (name, t))
    except BaseException as e:
        print("  %-16s(NULL) -> %s: %s" % (name, type(e).__name__, e))

print("--- legacy PyType_Spec path: same slots, NULL value ---")
for name, sid in IDS.items():
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            t = T.type_from_null_spec_slot(sid)
        print("  %-16s(NULL) -> ACCEPTED %r" % (name, t))
    except BaseException as e:
        print("  %-16s(NULL) -> %s: %s" % (name, type(e).__name__, e))
gc.collect()
print("DONE")
