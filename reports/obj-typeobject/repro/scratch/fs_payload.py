# Armed payload: drive type_from_slots_or_spec (PyType_FromSpec /
# PyType_FromMetaclass / PyType_FromSpecWithBases) through its `goto finally`
# unwind at every allocation index.
_testcapi.test_type_from_ephemeral_spec()
_testcapi.create_type_from_repeated_slots(0)
_testcapi.create_type_from_repeated_slots(1)
_testcapi.pytype_fromspec_meta(type)
_testcapi.create_type_with_token("_testcapi.Tok2", 0)
_testcapi.make_type_with_base(object)
