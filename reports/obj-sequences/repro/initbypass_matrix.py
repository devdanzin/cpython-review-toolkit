"""Per-method matrix for __init__-bypassed sequence objects.

Usage:  python initbypass_matrix.py <ctor> <op>

<ctor> selects how the receiver is built:
    ba_new       bytearray.__new__(bytearray)          -- tp_init never ran
    ba_sub       S(); class S(bytearray) with an __init__ that skips super()
    ba_normal    bytearray()                           -- control
    list_new     list.__new__(list)
    list_sub     L(); class L(list) with an __init__ that skips super()
    list_normal  list()                                -- control
    bytes_new    bytes.__new__(bytes)
    bytes_sub    B(); class B(bytes) with an __init__ that skips super()
    bytes_normal b''                                   -- control

<op> is one of the OPS keys below.  Exactly one op runs per process, so a
SIGSEGV on one op cannot hide the outcome of the others.

Prints exactly one line:  RESULT:<ctor>:<op>=<outcome>
A missing line means the process died before it could print (check rc).
"""

import sys


def make(ctor):
    if ctor == "ba_new":
        return bytearray.__new__(bytearray)
    if ctor == "ba_sub":
        class S(bytearray):
            def __init__(self, *a, **k):
                pass
        return S()
    if ctor == "ba_normal":
        return bytearray()
    if ctor == "list_new":
        return list.__new__(list)
    if ctor == "list_sub":
        class L(list):
            def __init__(self, *a, **k):
                pass
        return L()
    if ctor == "list_normal":
        return list()
    if ctor == "bytes_new":
        return bytes.__new__(bytes)
    if ctor == "bytes_sub":
        class B(bytes):
            def __init__(self, *a, **k):
                pass
        return B()
    if ctor == "bytes_normal":
        return b""
    raise SystemExit("unknown ctor " + ctor)


def _setslice(b):
    b[0:0] = b"XY"
    return b


def _setslice_empty(b):
    b[0:0] = b""
    return b


def _extslice_del(b):
    del b[::2]
    return b


def _imul(b):
    b *= 3
    return b


def _iadd(b):
    b += b"AB"
    return b


def _iadd_empty(b):
    b += b""
    return b


def _init_reentry(b):
    b.__init__(b"hello")
    return b


def _buffer(b):
    with memoryview(b) as mv:
        return bytes(mv)


# Ops are (callable, applies-to) where applies-to is a set of type tags.
BA = "ba"
LI = "li"
BY = "by"
ALL = "ba li by"

OPS = {
    # --- pure reads / queries -------------------------------------------
    "len": (len, ALL),
    "bool": (bool, ALL),
    "repr": (repr, ALL),
    "str": (str, ALL),
    "iter_list": (lambda b: list(iter(b)), ALL),
    "sizeof": (lambda b: b.__sizeof__(), ALL),
    "reduce": (lambda b: b.__reduce__(), "ba by"),
    "reduce_ex2": (lambda b: b.__reduce_ex__(2), ALL),
    "getstate": (lambda b: b.__getstate__(), "ba"),
    "copy": (lambda b: b.copy(), "ba li"),
    "eq_self": (lambda b: b == b, ALL),
    "eq_empty": (lambda b: b == type(b)(), ALL),
    "lt": (lambda b: b < b, ALL),
    "hash": (lambda b: hash(b), "by"),
    "alloc": (lambda b: b.__alloc__(), BA),
    "buffer": (_buffer, "ba by"),
    "count_arg": (lambda b: b.count(b"x"), "ba by"),
    "find": (lambda b: b.find(b"x"), "ba by"),
    "index_missing": (lambda b: b.index(b"x"), "ba by"),
    "startswith": (lambda b: b.startswith(b"x"), "ba by"),
    "endswith": (lambda b: b.endswith(b"x"), "ba by"),
    "contains": (lambda b: b"x" in b, "ba by"),
    "hex": (lambda b: b.hex(), "ba by"),
    "hex_sep": (lambda b: b.hex("_"), "ba by"),
    "decode": (lambda b: b.decode(), "ba by"),
    "split": (lambda b: b.split(), "ba by"),
    "rsplit": (lambda b: b.rsplit(), "ba by"),
    "splitlines": (lambda b: b.splitlines(), "ba by"),
    "partition": (lambda b: b.partition(b"x"), "ba by"),
    "rpartition": (lambda b: b.rpartition(b"x"), "ba by"),
    "strip": (lambda b: b.strip(), "ba by"),
    "lstrip": (lambda b: b.lstrip(), "ba by"),
    "rstrip": (lambda b: b.rstrip(), "ba by"),
    "removeprefix": (lambda b: b.removeprefix(b"x"), "ba by"),
    "removesuffix": (lambda b: b.removesuffix(b"x"), "ba by"),
    "replace": (lambda b: b.replace(b"x", b"yy"), "ba by"),
    "translate": (lambda b: b.translate(None), "ba by"),
    "join": (lambda b: b.join([b"a", b"b"]), "ba by"),
    "center": (lambda b: b.center(8), "ba by"),
    "ljust": (lambda b: b.ljust(8), "ba by"),
    "rjust": (lambda b: b.rjust(8), "ba by"),
    "zfill": (lambda b: b.zfill(8), "ba by"),
    "expandtabs": (lambda b: b.expandtabs(), "ba by"),
    "capitalize": (lambda b: b.capitalize(), "ba by"),
    "lower": (lambda b: b.lower(), "ba by"),
    "upper": (lambda b: b.upper(), "ba by"),
    "title": (lambda b: b.title(), "ba by"),
    "swapcase": (lambda b: b.swapcase(), "ba by"),
    "isalnum": (lambda b: b.isalnum(), "ba by"),
    "isascii": (lambda b: b.isascii(), "ba by"),
    "add": (lambda b: b + b"AB", "ba by"),
    "radd": (lambda b: b"AB" + b, "ba by"),
    "mul0": (lambda b: b * 0, ALL),
    "mul3": (lambda b: b * 3, ALL),
    "mod": (lambda b: b % (), "ba by"),
    "getitem0": (lambda b: b[0], ALL),
    "getslice": (lambda b: b[0:2], ALL),
    "take_bytes_none": (lambda b: b.take_bytes(), BA),
    "take_bytes_0": (lambda b: b.take_bytes(0), BA),
    # list-only reads
    "li_count": (lambda b: b.count(1), LI),
    "li_index": (lambda b: b.index(1), LI),
    "li_contains": (lambda b: 1 in b, LI),
    "li_sort": (lambda b: b.sort(), LI),
    "li_reverse": (lambda b: b.reverse(), LI),
    # --- mutations that do NOT grow --------------------------------------
    "clear": (lambda b: b.clear(), "ba li"),
    "reverse": (lambda b: b.reverse(), "ba li"),
    "resize0": (lambda b: b.resize(0), BA),
    "pop_empty": (lambda b: b.pop(), "ba li"),
    "remove_missing": (lambda b: b.remove(1), "ba li"),
    "extend_empty": (lambda b: b.extend(b""), "ba"),
    "li_extend_empty": (lambda b: b.extend([]), LI),
    "iadd_empty": (_iadd_empty, "ba"),
    "setslice_empty": (_setslice_empty, "ba li"),
    "imul0": (lambda b: b.__imul__(0), "ba li"),
    "delitem": (lambda b: b.__delitem__(0), "ba li"),
    "delslice_ext": (_extslice_del, "ba li"),
    "setitem": (lambda b: b.__setitem__(0, 65), "ba li"),
    # --- mutations that GROW the buffer ----------------------------------
    "append": (lambda b: b.append(1), "ba li"),
    "extend": (lambda b: b.extend(b"AB"), "ba"),
    "li_extend": (lambda b: b.extend([1, 2]), LI),
    "extend_iter": (lambda b: b.extend(iter([1, 2])), "ba li"),
    "insert": (lambda b: b.insert(0, 1), "ba li"),
    "iadd": (_iadd, "ba li"),
    "imul": (_imul, "ba li"),
    "setslice": (_setslice, "ba"),
    "li_setslice": (lambda b: b.__setitem__(slice(0, 0), [1, 2]), LI),
    "resize4": (lambda b: b.resize(4), BA),
    "fromhex_inst": (lambda b: b.fromhex("4142"), BA),
    # --- re-initialisation -----------------------------------------------
    "init_reentry": (_init_reentry, "ba li"),
    "init_empty": (lambda b: b.__init__(), "ba li"),
}


def main():
    ctor, op = sys.argv[1], sys.argv[2]
    fn, tags = OPS[op]
    b = make(ctor)
    try:
        val = fn(b)
    except BaseException as exc:  # noqa: BLE001 -- we are cataloguing outcomes
        print("RESULT:%s:%s=RAISED %s: %s" % (ctor, op, type(exc).__name__, exc))
        sys.stdout.flush()
        return
    print("RESULT:%s:%s=OK %r | self=%r" % (ctor, op, val, b))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
