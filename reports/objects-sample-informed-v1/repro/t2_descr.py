"""TSAN-0043: race descr->d_qualname's one-shot lazy init.

d_qualname is computed once per C descriptor and never reset, so the window is
the FIRST concurrent __qualname__ read on a given descriptor.  Harvest many
never-touched C descriptors, then hit each from N threads at once.
"""

import importlib
import sys
import threading
import types

MODS = [
    "_socket", "_ssl", "_sqlite3", "_json", "_pickle", "_datetime", "_decimal",
    "_lzma", "_bz2", "zlib", "_hashlib", "_csv", "_elementtree", "select",
    "_struct", "array", "mmap", "_random", "_heapq", "_collections", "itertools",
    "_functools", "_io", "_thread", "unicodedata", "binascii", "math", "cmath",
    "_multibytecodec", "_bisect", "_asyncio", "_queue", "_statistics", "_curses",
]

DescrTypes = (
    type(str.join),                      # method_descriptor
    type(str.__add__),                   # wrapper_descriptor
    type(type.__dict__["__dict__"]),     # getset_descriptor
)


def harvest() -> list[object]:
    out = []
    for name in MODS:
        try:
            m = importlib.import_module(name)
        except Exception:
            continue
        for obj in vars(m).values():
            if isinstance(obj, type):
                for v in vars(obj).values():
                    if isinstance(v, DescrTypes):
                        out.append(v)
    return out


def main() -> None:
    print("gil_enabled =", sys._is_gil_enabled())
    descrs = harvest()
    print("harvested descriptors:", len(descrs))
    NT = 8
    barrier = threading.Barrier(NT)

    def worker():
        barrier.wait()
        for d in descrs:
            try:
                _ = d.__qualname__
            except Exception:
                pass

    ts = [threading.Thread(target=worker) for _ in range(NT)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print("done")


if __name__ == "__main__":
    main()
