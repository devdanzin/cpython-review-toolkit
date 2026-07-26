"""notify site :7510 -- store_instance_attr_lock_held (the instance-__dict__ path).

Objects/dictobject.c:7437-7533

    7446:  PyDictObject *dict = _PyObject_GetManagedDict(obj);   <-- pre-notify
    7455:  ix = insert_split_key(keys, name, hash);              <-- pre-notify
    7497:  PyObject *old_value = values->values[ix];             <-- pre-notify, borrowed
    ...
    7510:  _PyDict_NotifyEvent(event, dict, name, value);        <-- window
    7513:  FT_ATOMIC_STORE_PTR_RELEASE(values->values[ix], Py_XNewRef(value));
    7516:  _PyDictValues_AddToInsertionOrder(values, ix);
    7518:  assert(dict->ma_values == values);
    7519:  STORE_USED(dict, dict->ma_used + 1);
    7524:  delete_index_from_values(values, ix);   <-- reads values->size, pre-notify state
    7526:  assert(dict->ma_values == values);
    7527:  STORE_USED(dict, dict->ma_used - 1);
    7530:  Py_DECREF(old_value);                   <-- stale borrowed ref

`values` is the caller's `_PyObject_InlineValues(obj)`, latched in
store_instance_attr_dict at :7538 before the critical section.  The
`dict->ma_values == values` identity is asserted only AFTER the window.

This is the inline-values path, so it is adjacent to CPY-0128
(_PyObject_InitInlineValues leaving the insertion-order array uninitialised)
but distinct: here the order array WAS initialised and the hook resets
values->size to 0 behind the caller's back.

Modes:

  mod       o.attr = v on an existing attribute -> MODIFIED.  Hook clears the
            dict; clear_embedded_values() DECREFs old_value to 0, then :7530
            DECREFs freed memory.

  del       del o.attr -> DELETED.  Hook clears the dict, so values->size is 0
            when :7524 runs delete_index_from_values(), whose `size--` writes
            values->size = (uint8_t)-1 = 255 and whose STORE_USED at :7527
            drives dict->ma_used to -1.

  detach    o.attr = v -> MODIFIED, and the hook assigns o.__dict__ = {}, which
            detaches the dict from the inline values.  :7513 then stores into
            an array the dict no longer points at, while :7519/:7527 still
            adjust the dict's ma_used.

Usage:  python notify_site_7510_store_instance_attr.py [mod|del|detach]
"""

import sys

import _testcapi

MODE = sys.argv[1] if len(sys.argv) > 1 else "mod"


class C:
    pass


def main():
    o = C()
    o.a = ["a"]
    o.b = ["b"]
    o.c = ["c"]

    d = o.__dict__  # materialize; d.ma_values still aliases o's inline values

    fired = []

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        if MODE == "detach":
            o.__dict__ = {"x": 1}
        else:
            d.clear()

    sys.unraisablehook = hook
    wid = _testcapi.add_dict_watcher(1)
    _testcapi.watch_dict(wid, d)
    print("[main] armed mode=%s" % MODE, flush=True)

    if MODE.startswith("del"):
        del o.c  # DELETED -> :7510 -> delete_index_from_values at :7524
    else:
        o.c = ["replacement"]  # MODIFIED -> :7510

    if MODE == "del_raw":
        # Skip the invariant probes and instead churn the list freelist and the
        # GC, which is what the doubly-DECREF'd old_value at :7530 corrupts.
        print("[main] returned; churning allocator", flush=True)
        import gc

        keep = []
        for j in range(2000):
            keep.append([j])
            if j % 100 == 0:
                gc.collect()
        del keep
        gc.collect()
        print("[main] survived churn", flush=True)
        return 0

    print("[main] returned from store_instance_attr_lock_held", flush=True)
    _testcapi.unwatch_dict(wid, d)
    sys.unraisablehook = sys.__unraisablehook__

    # len() first: dict_length returns ma_used, and PyObject_Size treats a
    # negative as an error, so a negative ma_used surfaces as
    # "SystemError: len() returned NULL without setting an exception".
    try:
        print("[main] len(d)=%d" % len(d), flush=True)
    except SystemError as exc:
        print("[main] *** len(d) raised %s -> ma_used < 0 ***" % (exc,), flush=True)

    # Iterating a split dict walks get_insertion_order_array(values)[0 ..
    # values->size).  delete_index_from_values' `size--` on a zeroed size wrote
    # values->size = (uint8_t)-1 = 255, so this walks ~255 bytes past the
    # inline-values allocation and then indexes values->values[garbage].
    try:
        print("[main] list(d)=%r" % (list(d),), flush=True)
    except BaseException as exc:
        print("[main] list(d) raised %r" % (exc,), flush=True)
    try:
        print("[main] vars(o)=%r" % (vars(o),), flush=True)
    except BaseException as exc:
        print("[main] vars(o) raised %r" % (exc,), flush=True)
    print("[main] survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
