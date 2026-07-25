"""T2 lazy-init race repros.

  ga_getitem      Objects/genericaliasobject.c:583  (sibling gh-153298 missed)
  descr_qualname  Objects/descrobject.c:624         (TSAN-0043)
"""

import sys
import threading
from typing import TypeVar

T = TypeVar("T")


def case_ga_getitem() -> None:
    """Race alias->parameters lazy init via `alias[int]` (mp_subscript path).

    gh-153298 guarded ga_parameters (the __parameters__ getset) with a
    critical section but left ga_getitem's identical inline lazy init alone,
    so the two accessors race each other.
    """
    slot = [list[T]]

    def subscript():
        for _ in range(4000):
            try:
                _ = slot[0][int]
            except Exception:
                pass

    def getparams():
        for _ in range(4000):
            try:
                _ = slot[0].__parameters__
            except Exception:
                pass

    def refresh():
        for _ in range(4000):
            slot[0] = list[T]

    ts = [threading.Thread(target=subscript) for _ in range(4)]
    ts += [threading.Thread(target=getparams) for _ in range(2)]
    ts += [threading.Thread(target=refresh) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print("ga_getitem: completed")


def case_descr_qualname() -> None:
    """Race descr->d_qualname lazy init (TSAN-0043)."""
    ns = {}
    exec("class K:\n    def m(self): pass\n", ns)
    slot = [ns["K"].__dict__["m"]]

    def read():
        for _ in range(4000):
            try:
                _ = slot[0].__qualname__
            except Exception:
                pass

    def refresh():
        for _ in range(4000):
            ns2 = {}
            exec("class K:\n    def m(self): pass\n", ns2)
            slot[0] = ns2["K"].__dict__["m"]

    ts = [threading.Thread(target=read) for _ in range(6)]
    ts += [threading.Thread(target=refresh) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print("descr_qualname: completed")


if __name__ == "__main__":
    print("gil_enabled =", sys._is_gil_enabled())
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "ga"):
        case_ga_getitem()
    if which in ("all", "descr"):
        case_descr_qualname()
