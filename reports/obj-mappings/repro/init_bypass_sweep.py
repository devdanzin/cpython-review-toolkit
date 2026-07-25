"""Exhaustive init-bypass sweep for Objects/dictobject.c + Objects/setobject.c.

For every type defined in those two files, try every construction route that can
skip the C-level constructor, then call every method / operator on the result.

Routes tried per type:
  R1  T.__new__(T)                       -- direct bypass
  R2  class S(T): __init__ = no-op ; S() -- subclass that forgets super().__init__()
  R3  object.__new__(T)                  -- generic allocator bypass
  R4  T.__new__(T) then T.__init__ skipped, methods called
  R5  subclassing T at all               -- is T a permitted base?

Any SIGSEGV kills the process, so each (type, route, method) probe runs in a
child process via fork so one crash does not hide the rest.
"""

import os
import sys
import traceback

d = {"a": 1, "b": 2}
s = {1, 2}

TYPES = {
    "dict": dict,
    "dict_keys": type(d.keys()),
    "dict_values": type(d.values()),
    "dict_items": type(d.items()),
    "dict_keyiterator": type(iter(d.keys())),
    "dict_valueiterator": type(iter(d.values())),
    "dict_itemiterator": type(iter(d.items())),
    "dict_reversekeyiterator": type(reversed(d.keys())),
    "dict_reversevalueiterator": type(reversed(d.values())),
    "dict_reverseitemiterator": type(reversed(d.items())),
    "set": set,
    "frozenset": frozenset,
    "set_iterator": type(iter(s)),
}
_b = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
if "frozendict" in _b:
    TYPES["frozendict"] = _b["frozendict"]


def probe_names(T):
    """Every public/dunder attribute worth calling on an instance."""
    names = []
    for n in dir(T):
        try:
            a = getattr(T, n)
        except Exception:  # noqa: BLE001
            continue
        if callable(a) or isinstance(a, (property, type(dict.__dict__.get("__len__")))):
            names.append(n)
        elif type(a).__name__ in ("getset_descriptor", "member_descriptor"):
            names.append(n)
    return names


ZERO_ARG_OPS = [
    ("len", len),
    ("repr", repr),
    ("str", str),
    ("iter", iter),
    ("list", list),
    ("bool", bool),
    ("hash", lambda o: hash(o)),
    ("reversed", reversed),
    ("dir", dir),
    ("next", next),
    ("copy_module", lambda o: __import__("copy").copy(o)),
    ("pickle", lambda o: __import__("pickle").dumps(o)),
]


def run_child(fn):
    """Run fn() in a forked child; return (tag, detail)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            fn()
            msg = b"OK"
        except BaseException as exc:  # noqa: BLE001
            msg = f"EXC {type(exc).__name__}: {exc}".encode()[:200]
        try:
            os.write(w, msg)
        except OSError:
            pass
        os._exit(0)
    os.close(w)
    out = b""
    try:
        while True:
            chunk = os.read(r, 4096)
            if not chunk:
                break
            out += chunk
    finally:
        os.close(r)
    _, status = os.waitpid(pid, 0)
    if os.WIFSIGNALED(status):
        return ("CRASH", f"signal {os.WTERMSIG(status)}")
    code = os.WEXITSTATUS(status)
    if code != 0:
        return ("EXIT", f"exit {code} {out.decode(errors='replace')}")
    txt = out.decode(errors="replace")
    if txt.startswith("EXC "):
        return ("EXC", txt[4:])
    return ("OK", txt)


def make_r1(T):
    def f():
        return T.__new__(T)
    return f


def make_r2(T):
    def f():
        ns = {"__init__": lambda self, *a, **k: None}
        S = type("S_" + T.__name__, (T,), ns)
        return S()
    return f


def make_r3(T):
    def f():
        return object.__new__(T)
    return f


def make_r5(T):
    def f():
        return type("S_" + T.__name__, (T,), {})
    return f


ROUTES = [("R1 T.__new__(T)", make_r1),
          ("R2 subclass no-super-init", make_r2),
          ("R3 object.__new__(T)", make_r3),
          ("R5 subclassable?", make_r5)]


def main():
    print("=" * 78)
    print("build:", sys.version.replace("\n", " "))
    print("exe  :", sys.executable)
    print("=" * 78)

    crashes = []
    total_probes = 0

    for tname, T in TYPES.items():
        print(f"\n### {tname}")
        constructible = {}
        for rname, maker in ROUTES:
            tag, detail = run_child(maker(T))
            total_probes += 1
            if tag == "CRASH":
                crashes.append((tname, rname, "<construction>", detail))
            mark = {"OK": "yes", "EXC": "no ", "CRASH": "CRASH", "EXIT": "EXIT"}[tag]
            print(f"  {rname:28s} {mark:5s} {detail if tag != 'OK' else ''}")
            constructible[rname] = (tag == "OK")

        # For every route that produced an object, hammer its methods.
        for rname, maker in ROUTES:
            if rname == "R5 subclassable?" or not constructible.get(rname):
                continue
            names = probe_names(T)
            for opname, op in ZERO_ARG_OPS:
                def probe(maker=maker, op=op):
                    o = maker()
                    op(o)
                tag, detail = run_child(probe)
                total_probes += 1
                if tag == "CRASH":
                    crashes.append((tname, rname, opname, detail))
                    print(f"    !! CRASH {rname} {opname}: {detail}")
            for n in names:
                def probe(maker=maker, n=n):
                    o = maker()
                    a = getattr(o, n)
                    if callable(a):
                        a()
                tag, detail = run_child(probe)
                total_probes += 1
                if tag == "CRASH":
                    crashes.append((tname, rname, n, detail))
                    print(f"    !! CRASH {rname} .{n}(): {detail}")
            print(f"  probed {len(names) + len(ZERO_ARG_OPS)} ops on {rname}: "
                  f"{sum(1 for c in crashes if c[0] == tname and c[1] == rname)} crashes")

    print("\n" + "=" * 78)
    print(f"TOTAL PROBES: {total_probes}")
    print(f"CRASHES:      {len(crashes)}")
    for c in crashes:
        print("  CRASH", c)
    print("=" * 78)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
