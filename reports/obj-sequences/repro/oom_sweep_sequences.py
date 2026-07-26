#!/usr/bin/env python3
"""Allocation-failure sweep over every Python-reachable constructor and
resize/realloc path in the `obj-sequences` slice.

Slice: Objects/{listobject,bytesobject,bytearrayobject,bytes_methods}.c
Ref:   /home/danzin/projects/cpython @ 4f3be1b5777

Two payload shapes, deliberately distinct:

  * "raise"  -- the operation is allowed to propagate MemoryError.  This is the
    shape that catches the *uninitialized-dealloc* class, because the crash
    happens inside the constructor's own Py_DECREF, before MemoryError ever
    reaches Python.

  * "survive" -- the operation is wrapped in `except MemoryError: pass` and the
    receiver is then USED (len / bytes() / append / iterate / repr).  This is
    the shape that catches the *"the error return left the object describing
    memory that is gone"* class -- the bytearrayobject.c:1609 template.  A
    sweep that only lets MemoryError propagate scores those paths as clean:
    exit 1 == "memory_error" == safe.

Usage:
    python oom_sweep_sequences.py --python <build>/python [--max-n 80]
                                  [--only NAME] [--out results.json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SWEEP = (
    Path(__file__).resolve().parents[4]
    / "cpython-review-toolkit/plugins/cpython-review-toolkit/scripts/run_oom_sweep.py"
)

# Fallback: the toolkit checkout is a sibling of the reports tree in this campaign.
if not SWEEP.exists():
    SWEEP = Path(
        "/home/danzin/projects/cpython-review-toolkit/plugins/"
        "cpython-review-toolkit/scripts/run_oom_sweep.py"
    )

# name -> (file, setup, code, max_n)
#
# `setup` runs UNARMED (before set_nomemory), so warm-up / freelist draining /
# imports do not burn the injection budget.
SCENARIOS: dict[str, tuple[str, str, str, int]] = {
    # ---------------- Objects/listobject.c : constructors -----------------
    "list_new_sized": ("listobject.c", "x = [None] * 300; del x", "l = [None] * 300", 60),
    "list_new_empty": ("listobject.c", "x = []; del x", "l = []", 40),
    "list_literal": ("listobject.c", "x = [1, 2, 3]; del x", "l = [1, 2, 3, 4, 5]", 40),
    "list_prealloc_tuple": (
        "listobject.c",
        "t = tuple(range(300)); x = list(t); del x",
        "l = list(t)",
        60,
    ),
    "list_from_range": ("listobject.c", "x = list(range(300)); del x", "l = list(range(300))", 60),
    "list_from_iter": (
        "listobject.c",
        "x = list(iter(range(300))); del x",
        "l = list(iter(range(300)))",
        80,
    ),
    "list_from_set": (
        "listobject.c",
        "s = set(range(300)); x = list(s); del x",
        "l = list(s)",
        60,
    ),
    "list_from_dict": (
        "listobject.c",
        "d = {i: i for i in range(300)}; x = list(d); del x",
        "l = list(d)",
        60,
    ),
    "list_iter_ctor": ("listobject.c", "l = list(range(50)); x = iter(l); del x", "it = iter(l)", 30),
    "list_reversed_ctor": (
        "listobject.c",
        "l = list(range(50)); x = reversed(l); del x",
        "it = reversed(l)",
        30,
    ),
    # ---------------- Objects/listobject.c : resize paths -----------------
    "list_append_grow": (
        "listobject.c",
        "l = []\nfor i in range(600): l.append(i)\nl = []",
        "l = []\nfor i in range(600): l.append(i)",
        80,
    ),
    "list_append_survive": (
        "listobject.c",
        "l = []\nfor i in range(600): l.append(i)\nl = []",
        "l = []\n"
        "try:\n"
        "    for i in range(600): l.append(i)\n"
        "except MemoryError: _c = 1\n"
        "L = len(l); r = repr(l)[:40]; l.append(1); l.pop(); l.clear()",
        80,
    ),
    "list_extend_survive": (
        "listobject.c",
        "l = []; l.extend(range(600)); l = []",
        "l = []\n"
        "try:\n    l.extend(range(600))\n"
        "except MemoryError: _c = 1\n"
        "L = len(l); l.append(1); l.reverse(); l.clear()",
        80,
    ),
    "list_insert_survive": (
        "listobject.c",
        "l = list(range(300))\nfor i in range(200): l.insert(0, i)\nl = []",
        "l = list(range(300))\n"
        "try:\n    \n"
        "    for i in range(200): l.insert(0, i)\n"
        "except MemoryError: _c = 1\n"
        "L = len(l); l.append(1); l.clear()",
        80,
    ),
    "list_slice_copy": ("listobject.c", "l = list(range(300)); x = l[:]; del x", "m = l[:]", 60),
    "list_repeat": ("listobject.c", "l = list(range(30)); x = l * 20; del x", "m = l * 20", 60),
    "list_irepeat_survive": (
        "listobject.c",
        "l = list(range(30)); l2 = list(range(30)); l2 *= 20",
        "l2 = list(range(30))\n"
        "try:\n    l2 *= 40\n"
        "except MemoryError: _c = 1\n"
        "L = len(l2); l2.append(1); l2.clear()",
        80,
    ),
    "list_concat": ("listobject.c", "l = list(range(200)); x = l + l; del x", "m = l + l", 60),
    "list_slice_assign_survive": (
        "listobject.c",
        "l = list(range(300)); l[10:20] = list(range(200))",
        "l = list(range(300))\n"
        "try:\n    l[10:20] = list(range(400))\n"
        "except MemoryError: _c = 1\n"
        "L = len(l); l.append(1); l.clear()",
        80,
    ),
    "list_del_slice_survive": (
        "listobject.c",
        "l = list(range(400)); del l[::2]",
        "l = list(range(400))\n"
        "try:\n    del l[::2]\n"
        "except MemoryError: _c = 1\n"
        "L = len(l); l.append(1); l.clear()",
        80,
    ),
    "list_sort": (
        "listobject.c",
        "import random; base = [random.random() for _ in range(600)]; sorted(base)",
        "s = sorted(base)",
        90,
    ),
    "list_sort_key": (
        "listobject.c",
        "import random; base = [random.random() for _ in range(600)]; sorted(base, key=abs)",
        "s = sorted(base, key=abs)",
        90,
    ),
    "list_sort_survive": (
        "listobject.c",
        "import random; base = [random.random() for _ in range(600)]; b2 = list(base); b2.sort()",
        "b2 = list(base)\n"
        "try:\n    b2.sort()\n"
        "except MemoryError: _c = 1\n"
        "L = len(b2); b2.append(1.0); b2.clear()",
        90,
    ),
    "list_repr": ("listobject.c", "l = list(range(300)); x = repr(l); del x", "r = repr(l)", 60),
    # ---------------- Objects/bytesobject.c : constructors ----------------
    "bytes_new_sized": ("bytesobject.c", "x = bytes(3000); del x", "b = bytes(3000)", 40),
    "bytes_repeat": ("bytesobject.c", "x = b'ab' * 900; del x", "b = b'ab' * 900", 40),
    "bytes_concat": ("bytesobject.c", "x = b'a' * 100 + b'b' * 900; del x", "b = b'a' * 100 + b'b' * 900", 40),
    "bytes_iconcat_survive": (
        "bytesobject.c",
        "b = b'a'\nfor i in range(200): b += b'xyz'",
        "b = b'a'\n"
        "try:\n"
        "    for i in range(400): b += b'xyz'\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); h = b[:8]; r = b.hex()[:8]",
        90,
    ),
    "bytes_from_list": ("bytesobject.c", "x = bytes([65] * 900); del x", "b = bytes([65] * 900)", 60),
    "bytes_from_tuple": (
        "bytesobject.c",
        "t = tuple([65] * 900); x = bytes(t); del x",
        "b = bytes(t)",
        60,
    ),
    "bytes_from_iter": (
        "bytesobject.c",
        "x = bytes(iter([65] * 900)); del x",
        "b = bytes(iter([65] * 900))",
        80,
    ),
    "bytes_fromhex": (
        "bytesobject.c",
        "h = '41' * 900; x = bytes.fromhex(h); del x",
        "b = bytes.fromhex(h)",
        60,
    ),
    "bytes_hex": ("bytesobject.c", "b = b'A' * 900; x = b.hex(); del x", "s = b.hex()", 40),
    "bytes_hex_sep": (
        "bytesobject.c",
        "b = b'A' * 900; x = b.hex(':'); del x",
        "s = b.hex(':')",
        40,
    ),
    "bytes_format": (
        "bytesobject.c",
        "x = b'%d %s %f %r' % (12345, b'y' * 400, 1.5, b'z'); del x",
        "s = b'%d %s %f %r' % (12345, b'y' * 400, 1.5, b'z')",
        90,
    ),
    "bytes_format_dict": (
        "bytesobject.c",
        "d = {b'k': b'v' * 400}; x = b'%(k)s' % d; del x",
        "s = b'%(k)s' % d",
        60,
    ),
    "bytes_translate": (
        "bytesobject.c",
        "tbl = bytes.maketrans(b'A', b'B'); b = b'A' * 900; x = b.translate(tbl, b'Z'); del x",
        "s = b.translate(tbl, b'Z')",
        40,
    ),
    "bytes_replace": (
        "bytesobject.c",
        "b = b'A' * 900; x = b.replace(b'A', b'BBB'); del x",
        "s = b.replace(b'A', b'BBB')",
        40,
    ),
    "bytes_split_join": (
        "bytesobject.c",
        "b = b'a,' * 400; x = b.split(b','); y = b','.join(x); del x, y",
        "parts = b.split(b','); j = b','.join(parts)",
        90,
    ),
    "bytes_encode": (
        "bytesobject.c",
        "s = 'x' * 900; x = s.encode(); del x",
        "b = s.encode()",
        40,
    ),
    "bytes_decode": (
        "bytesobject.c",
        "b = b'x' * 900; x = b.decode(); del x",
        "s = b.decode()",
        40,
    ),
    "bytes_repr": ("bytesobject.c", "b = b'\\x00\\xff' * 400; x = repr(b); del x", "r = repr(b)", 40),
    "bytes_iter_ctor": ("bytesobject.c", "b = b'x' * 50; x = iter(b); del x", "it = iter(b)", 30),
    "bytes_int_tobytes": (
        "bytesobject.c",
        "n = 2 ** 4000; x = n.to_bytes(900, 'big'); del x",
        "b = n.to_bytes(900, 'big')",
        40,
    ),
    "bytes_writer_format": (
        "bytesobject.c",
        "x = b'%b' % (b'q' * 900,); del x",
        "s = b'%b' % (b'q' * 900,)",
        60,
    ),
    # ---------------- Objects/bytearrayobject.c : constructors ------------
    "bytearray_new_sized": (
        "bytearrayobject.c",
        "x = bytearray(3000); del x",
        "b = bytearray(3000)",
        40,
    ),
    "bytearray_from_bytes": (
        "bytearrayobject.c",
        "src = b'x' * 3000; x = bytearray(src); del x",
        "b = bytearray(src)",
        40,
    ),
    "bytearray_from_list": (
        "bytearrayobject.c",
        "x = bytearray([65] * 900); del x",
        "b = bytearray([65] * 900)",
        60,
    ),
    "bytearray_from_iter": (
        "bytearrayobject.c",
        "x = bytearray(iter([65] * 900)); del x",
        "b = bytearray(iter([65] * 900))",
        80,
    ),
    "bytearray_from_str": (
        "bytearrayobject.c",
        "x = bytearray('y' * 900, 'ascii'); del x",
        "b = bytearray('y' * 900, 'ascii')",
        60,
    ),
    "bytearray_iter_ctor": (
        "bytearrayobject.c",
        "b = bytearray(50); x = iter(b); del x",
        "it = iter(b)",
        30,
    ),
    # ---------------- Objects/bytearrayobject.c : resize paths ------------
    "bytearray_append_survive": (
        "bytearrayobject.c",
        "b = bytearray()\nfor i in range(900): b.append(65)\nb = bytearray()",
        "b = bytearray()\n"
        "try:\n"
        "    for i in range(900): b.append(65)\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:8]; b.append(66); b.pop(); b.clear()",
        90,
    ),
    "bytearray_extend_survive": (
        "bytearrayobject.c",
        "b = bytearray(); b.extend(b'z' * 4000); b = bytearray()",
        "b = bytearray()\n"
        "try:\n    b.extend(b'z' * 4000)\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:8]; b.append(66); b.clear()",
        90,
    ),
    "bytearray_iadd_survive": (
        "bytearrayobject.c",
        "b = bytearray(b'a'); b += b'z' * 4000; b = bytearray()",
        "b = bytearray(b'a')\n"
        "try:\n    b += b'z' * 4000\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:8]; b.append(66); b.clear()",
        90,
    ),
    "bytearray_imul_survive": (
        "bytearrayobject.c",
        "b = bytearray(b'abcd'); b *= 900; b = bytearray()",
        "b = bytearray(b'abcd')\n"
        "try:\n    b *= 900\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:8]; b.append(66); b.clear()",
        60,
    ),
    "bytearray_resize_survive": (
        "bytearrayobject.c",
        "b = bytearray(b'IMPORTANT'); b.resize(4000); b = bytearray()",
        "b = bytearray(b'IMPORTANT')\n"
        "try:\n    b.resize(4000)\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:9]; b.append(66); b.clear()",
        60,
    ),
    "bytearray_take_bytes_survive": (
        "bytearrayobject.c",
        "b = bytearray(b'A' * 4096); b.take_bytes(2048); b = bytearray()",
        "b = bytearray(b'A' * 4096)\n"
        "try:\n    t = b.take_bytes(2048)\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:8]; b.append(66); b.clear()",
        60,
    ),
    "bytearray_insert_survive": (
        "bytearrayobject.c",
        "b = bytearray(b'x' * 500)\nfor i in range(400): b.insert(0, 65)\nb = bytearray()",
        "b = bytearray(b'x' * 500)\n"
        "try:\n"
        "    for i in range(400): b.insert(0, 65)\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:8]; b.append(66); b.clear()",
        90,
    ),
    "bytearray_setslice_survive": (
        "bytearrayobject.c",
        "b = bytearray(b'x' * 500); b[10:20] = b'y' * 3000",
        "b = bytearray(b'x' * 500)\n"
        "try:\n    b[10:20] = b'y' * 3000\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:8]; b.append(66); b.clear()",
        90,
    ),
    "bytearray_delslice_survive": (
        "bytearrayobject.c",
        "b = bytearray(b'x' * 800); del b[::2]",
        "b = bytearray(b'x' * 800)\n"
        "try:\n    del b[::2]\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:8]; b.append(66); b.clear()",
        90,
    ),
    "bytearray_reinit_survive": (
        "bytearrayobject.c",
        "b = bytearray(b'x' * 500); b.__init__(b'y' * 3000)",
        "b = bytearray(b'x' * 500)\n"
        "try:\n    b.__init__(b'y' * 3000)\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:8]; b.append(66); b.clear()",
        90,
    ),
    "bytearray_pop_remove_survive": (
        "bytearrayobject.c",
        "b = bytearray(b'x' * 800)\nfor i in range(700): b.pop()\nb = bytearray()",
        "b = bytearray(b'x' * 800)\n"
        "try:\n"
        "    for i in range(700): b.pop()\n"
        "except MemoryError: _c = 1\n"
        "L = len(b); V = bytes(b)[:8]; b.append(66); b.clear()",
        90,
    ),
    "bytearray_hex": (
        "bytearrayobject.c",
        "b = bytearray(b'A' * 900); x = b.hex(); del x",
        "s = b.hex()",
        40,
    ),
    "bytearray_mod": (
        "bytearrayobject.c",
        "b = bytearray(b'%d %s'); x = b % (12345, b'y' * 400); del x",
        "s = b % (12345, b'y' * 400)",
        90,
    ),
    "bytearray_split_join": (
        "bytearrayobject.c",
        "b = bytearray(b'a,' * 400); x = b.split(b','); y = bytearray(b',').join(x); del x, y",
        "parts = b.split(b','); j = bytearray(b',').join(parts)",
        90,
    ),
    "bytearray_decode": (
        "bytearrayobject.c",
        "b = bytearray(b'x' * 900); x = b.decode(); del x",
        "s = b.decode()",
        40,
    ),
    "bytearray_repr": (
        "bytearrayobject.c",
        "b = bytearray(b'\\x00\\xff' * 400); x = repr(b); del x",
        "r = repr(b)",
        40,
    ),
    "bytearray_replace": (
        "bytearrayobject.c",
        "b = bytearray(b'A' * 900); x = b.replace(b'A', b'BBB'); del x",
        "s = b.replace(b'A', b'BBB')",
        40,
    ),
    # ---------------- Objects/bytes_methods.c -----------------------------
    "maketrans": (
        "bytes_methods.c",
        "x = bytes.maketrans(b'abc', b'xyz'); del x",
        "t = bytes.maketrans(b'abc', b'xyz')",
        30,
    ),
    "maketrans_bytearray": (
        "bytes_methods.c",
        "x = bytearray.maketrans(b'abc', b'xyz'); del x",
        "t = bytearray.maketrans(b'abc', b'xyz')",
        30,
    ),
}


def run_one(python: str, name: str, max_n: int | None, timeout: float) -> dict:
    src_file, setup, code, default_n = SCENARIOS[name]
    n = max_n if max_n else default_n
    # "survive" payloads swallow MemoryError so the object can be USED after the
    # failure.  Left alone they exit 0 and run_oom_sweep scores them
    # "completed", which makes `allocation_failure_points` count zero and the
    # verdict read "too thin" -- a harness artifact, not a property of the code.
    # Re-raising AFTER the use restores the honest denominator while keeping
    # the crash detection: a fault during the use still returns 139/134.
    if "_c = 1" in code:
        code = "_c = 0\n" + code + "\nif _c: raise MemoryError('injected')"
    cmd = [
        sys.executable,
        str(SWEEP),
        "--python",
        python,
        "--setup",
        setup,
        "--code",
        code,
        "--max-n",
        str(n),
        "--timeout",
        str(timeout),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "name": name,
            "file": src_file,
            "error": "sweep did not emit JSON",
            "stdout": proc.stdout[-2000:],
            "stderr": proc.stderr[-2000:],
        }
    return {
        "name": name,
        "file": src_file,
        "max_n": n,
        "allocation_failure_points": data.get("allocation_failure_points"),
        "outcome_counts": data.get("outcome_counts"),
        "crashes": data.get("crashes", []),
        "reproduced": data.get("reproduced"),
        "dry_run_ok": data.get("dry_run", {}).get("ok"),
        "verdict": data.get("summary", {}).get("verdict"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True)
    ap.add_argument("--max-n", type=int, default=0, help="override every scenario's max-n")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--out", default="")
    ap.add_argument("--timeout", type=float, default=25.0)
    args = ap.parse_args()

    names = args.only or list(SCENARIOS)
    results = []
    for name in names:
        if name not in SCENARIOS:
            print(f"!! unknown scenario {name}", file=sys.stderr)
            continue
        res = run_one(args.python, name, args.max_n or None, args.timeout)
        results.append(res)
        pts = res.get("allocation_failure_points")
        crash_idx = [c["n"] for c in res.get("crashes", [])]
        flag = "CRASH" if crash_idx else ("thin" if (pts or 0) < 20 else "ok")
        print(
            f"{name:34s} {res.get('file','?'):20s} points={pts!s:>4} "
            f"{flag:6s} crashes={crash_idx}",
            flush=True,
        )
        if res.get("error"):
            print(f"    ERROR: {res['error']}\n    {res.get('stderr','')[:400]}", flush=True)

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.out}")

    n_crash = sum(1 for r in results if r.get("crashes"))
    total_points = sum((r.get("allocation_failure_points") or 0) for r in results)
    print(
        f"\n== {len(results)} scenarios, {total_points} real allocation-failure points, "
        f"{n_crash} scenarios with a crash"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
