"""Reachable set of `bytearray.__new__(bytearray)` OUTSIDE Objects/bytearrayobject.c.

`bytearray` wires tp_init but leaves tp_new = PyType_GenericNew
(`Objects/bytearrayobject.c:2938/2940`), so `bytearray.__new__(bytearray)` --
and any Python subclass whose __init__ skips super().__init__() -- yields a
zeroed instance with

    ob_bytes_object == NULL, ob_bytes == NULL, ob_start == NULL, ob_size == 0

`PyByteArray_AS_STRING()` in this tree returns `ob_start` with **no** empty-string
fallback (Include/cpython/bytearrayobject.h:24-28), so the object's data pointer
is NULL while the type's documented C-API contract
(`PyByteArray_AsString`, Include/bytearrayobject.h:32) promises a NUL-terminated
buffer.  Every consumer that trusts that contract instead of the buffer protocol
is in the reachable set.

Usage:  python initbypass_bytearray_consumers.py <probe>
        python initbypass_bytearray_consumers.py --list

One probe per process.  No PROBE: line == the process died first.
"""

import sys


def mk():
    return bytearray.__new__(bytearray)


def mk_sub():
    class S(bytearray):
        def __init__(self, *a, **k):
            pass
    return S()


def p(name, val):
    print("PROBE:%s=%s" % (name, val))
    sys.stdout.flush()


# ---- numeric / builtin coercions ------------------------------------------

def c_int():
    return int(mk())


def c_int_subclass():
    return int(mk_sub())


def c_int_base():
    return int(mk(), 10)


def c_float():
    return float(mk())


def c_complex():
    return complex(mk())


def c_ord():
    return ord(mk())


def c_index_via_operator():
    return [0][mk()]


def c_int_base0():
    return int(mk(), 0)


def c_int_base16():
    return int(mk(), 16)


def c_decimal():
    import decimal
    return decimal.Decimal(mk())


def c_fraction():
    import fractions
    return fractions.Fraction(mk())


# ---- compiler / source-text consumers -------------------------------------

def c_compile():
    return compile(mk(), "<probe>", "exec")


def c_exec():
    return exec(mk())


def c_eval():
    return eval(mk())


def c_ast_parse():
    import ast
    return ast.parse(mk())


def c_codeop():
    import codeop
    return codeop.compile_command(mk())


# ---- str / codec ----------------------------------------------------------

def c_str_decode():
    return str(mk(), "ascii")


def c_bytes():
    return bytes(mk())


def c_list():
    return list(mk())


def c_bytes_format_s():
    return b"%s" % (mk(),)


def c_bytes_format_c():
    return b"%c" % (mk(),)


def c_unicode_from_bytearray():
    return mk().decode("utf-8")


# ---- buffer-protocol consumers (should all be safe: len 0) ----------------

def c_memoryview_ops():
    b = mk()
    mv = memoryview(b)
    out = (bytes(mv), mv.tolist(), mv.nbytes, mv.hex(), mv.readonly, len(mv))
    mv.release()
    return out


def c_binascii():
    import binascii
    return binascii.hexlify(mk()), binascii.b2a_base64(mk())


def c_zlib():
    import zlib
    return zlib.compress(mk())


def c_hashlib():
    import hashlib
    return hashlib.sha256(mk()).hexdigest()[:16]


def c_re():
    import re
    return re.match(b"", mk())


def c_array():
    import array
    return array.array("b", mk())


def c_os_write():
    import os
    r, w = os.pipe()
    try:
        return os.write(w, mk())
    finally:
        os.close(r)
        os.close(w)


def c_struct_pack_into():
    import struct
    return struct.pack_into("b", mk(), 0, 1)


def c_struct_unpack():
    import struct
    return struct.unpack("", mk())


def c_socket_sendto():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return s.sendto(mk(), ("127.0.0.1", 9))
    finally:
        s.close()


def c_socket_recv_into():
    import socket
    a, bsock = socket.socketpair()
    try:
        bsock.send(b"")
        return a.recv_into(mk())
    finally:
        a.close()
        bsock.close()


def c_pickle():
    import pickle
    return pickle.loads(pickle.dumps(mk(), 2))


def c_pickle_sub():
    import pickle
    b = mk_sub()
    return pickle.dumps(b, 2)


def c_marshal():
    import marshal
    return marshal.dumps(bytes(mk()))


def c_io_readinto():
    import io
    return io.BytesIO(b"").readinto(mk())


def c_io_readline_bytearray():
    """iobase.c:658-667 accumulate into a bytearray -- our object is the SOURCE."""
    import io
    return io.BytesIO(bytes(mk())).readline()


def c_ctypes_from_buffer():
    import ctypes
    return ctypes.c_char * 0


def c_ctypes_asstring():
    """Call PyByteArray_AsString() through ctypes and report what it returns."""
    import ctypes
    py = ctypes.pythonapi
    py.PyByteArray_AsString.restype = ctypes.c_void_p
    py.PyByteArray_AsString.argtypes = [ctypes.py_object]
    normal = py.PyByteArray_AsString(bytearray())
    bypassed = py.PyByteArray_AsString(mk())
    return "normal=%r bypassed=%r (contract: non-NULL NUL-terminated buffer)" % (
        normal, bypassed)


def c_expat():
    import xml.parsers.expat
    pr = xml.parsers.expat.ParserCreate()
    return pr.Parse(mk(), True)


def c_ssl_password():
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        return ctx.load_cert_chain("/nonexistent", password=mk())
    except Exception as exc:  # noqa: BLE001
        return "%s: %s" % (type(exc).__name__, str(exc)[:60])


def c_sqlite_blob():
    import sqlite3
    con = sqlite3.connect(":memory:")
    try:
        con.execute("create table t(x)")
        con.execute("insert into t values(?)", (mk(),))
        return con.execute("select x from t").fetchone()
    finally:
        con.close()


def c_json():
    import json
    try:
        return json.loads(mk())
    except Exception as exc:  # noqa: BLE001
        return "%s: %s" % (type(exc).__name__, str(exc)[:60])


def c_codecs_decode():
    import codecs
    return codecs.decode(mk(), "hex")


def c_repr_sub():
    return repr(mk_sub())


PROBES = {k[2:]: v for k, v in sorted(globals().items())
          if k.startswith("c_") and callable(v)}


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "--list":
        for k in PROBES:
            print(k)
        return
    name = sys.argv[1]
    fn = PROBES[name]
    try:
        p(name, repr(fn()))
    except BaseException as exc:  # noqa: BLE001
        p(name, "RAISED %s: %s" % (type(exc).__name__, str(exc)[:80]))


if __name__ == "__main__":
    main()
