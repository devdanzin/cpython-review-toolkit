import _testcapi

# Warm everything unarmed so the injection budget lands inside
# type_from_slots_or_spec, not in import machinery.
_testcapi.test_type_from_ephemeral_spec()
_testcapi.create_type_from_repeated_slots(0)
_testcapi.create_type_from_repeated_slots(1)
_m = _testcapi.pytype_fromspec_meta(type)
_t = _testcapi.create_type_with_token("_testcapi.Tok", 0)
_b = _testcapi.make_type_with_base(object)
del _m, _t, _b
