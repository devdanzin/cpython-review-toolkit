"""Prove that scan_init_bypass's zero on the obj-mappings slice is STRUCTURAL,
and that toolkit defect D-8 (the "_impl naming" heuristic) is NOT the cause.

Method
------
1. Run the scanner's own internals over the real Objects/dictobject.c and
   Objects/setobject.c and print `_positional_bypassable_inits` +
   `_collect_nullable_fields`.  Both are empty: the tp_init/tp_new PAIRING
   filter rejects dict_init (paired with dict_new) and set_init (paired with
   set_new) before any function-name lookup happens.  D-8 is never reached.

2. Counterfactual: copy both files, force `tp_new` to `0` in PyDict_Type and
   PySet_Type, rescan.  Now the pairing filter passes and D-8's name heuristic
   DOES run -- and it succeeds, finding `dict_init` and `set_init` by their
   literal names (neither is an Argument Clinic `_impl` rename).  The nullable
   set is still effectively empty: dict_init assigns no struct field at all,
   set_init assigns only the scalar `self->hash`.  Still 0 findings.

Conclusion: the zero survives even with the suppressing filter removed, so it is
structural at two independent levels, and D-8 did not produce it.

Usage:  <interpreter> scanner_zero_is_structural.py [cpython_root]
"""

import pathlib
import shutil
import sys
import tempfile

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)

import scan_init_bypass as m  # noqa: E402

CPYTHON = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "/home/danzin/projects/cpython")

PATCHES = {
    "dictobject.c": ("    dict_new,                                   /* tp_new */",
                     "    0,                                          /* tp_new */"),
    "setobject.c": ("    set_new,                            /* tp_new */",
                    "    0,                                  /* tp_new */"),
}


def report(base, label):
    print(f"### {label}")
    for name in ("dictobject.c", "setobject.c"):
        p = base / "Objects" / name
        data = p.read_bytes()
        src = data.decode("utf-8", errors="replace")
        funcs = m.extract_functions(m.parse_bytes(data), data)
        clean = m.strip_comments(src)
        print(f"  {name}")
        print(f"    positional bypassable tp_inits: "
              f"{m._positional_bypassable_inits(src)}")
        print(f"    spec bypassable tp_inits      : "
              f"{m._spec_bypassable_inits(clean)}")
        print(f"    nullable fields               : "
              f"{dict(m._collect_nullable_fields(src, funcs))}")


def main():
    report(CPYTHON, "REAL SOURCE (target ref)")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="init_bypass_cf_"))
    (tmp / "Objects").mkdir()
    for name, (old, new) in PATCHES.items():
        src = CPYTHON / "Objects" / name
        dst = tmp / "Objects" / name
        shutil.copy(src, dst)
        text = dst.read_text()
        if old not in text:
            raise SystemExit(f"patch anchor not found in {name}: {old!r}")
        dst.write_text(text.replace(old, new))
    print()
    report(tmp, "COUNTERFACTUAL (tp_new forced to 0)")
    shutil.rmtree(tmp)

    print("\nExpected:")
    print("  real          -> [] / [] / {}   for both files")
    print("  counterfactual-> ['dict_init'] {} ; ['set_init'] {'hash'} -> "
          "still 0 findings (no sink consumes a Py_hash_t)")


if __name__ == "__main__":
    main()
