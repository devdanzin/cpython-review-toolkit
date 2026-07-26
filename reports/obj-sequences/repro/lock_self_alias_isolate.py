#!/usr/bin/env python3
"""Single-threaded isolation for the `self_alias` hang.

`self_alias` hung on all four builds.  A hang under concurrency looks exactly
like a deadlock; running each operation ALONE separates "the lock cannot be
acquired" from "this Python expression simply does not terminate".

Usage: python lock_self_alias_isolate.py <op>
"""

import faulthandler
import operator
import sys

faulthandler.enable()
faulthandler.dump_traceback_later(10.0, exit=True)

N = 2000


def op_ba_iadd():
    b = bytearray(b"A" * 64)
    for _ in range(N):
        operator.iadd(b, b)
        del b[64:]
    return len(b)


def op_ba_slice():
    b = bytearray(b"A" * 64)
    for _ in range(N):
        b[0:0] = b
        del b[64:]
    return len(b)


def op_li_slice():
    l = list(range(64))
    for _ in range(N):
        l[0:0] = l
        del l[64:]
    return len(l)


def op_li_extend_list():
    """The CS2 arm: extend(self) with an exact list."""
    l = list(range(64))
    for _ in range(N):
        l.extend(l)
        del l[64:]
    return len(l)


def op_li_extend_iter():
    """The `else` arm: extend(iter(self)).  Suspected non-terminating."""
    l = list(range(64))
    for _ in range(N):
        l.extend(iter(l))
        del l[64:]
    return len(l)


def op_ba_extend_iter():
    b = bytearray(b"A" * 64)
    for _ in range(N):
        b.extend(iter(b))
        del b[64:]
    return len(b)


OPS = {k[3:]: v for k, v in sorted(globals().items()) if k.startswith("op_")}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in OPS:
        print("ops:", " ".join(sorted(OPS)))
        return 2
    name = sys.argv[1]
    r = OPS[name]()
    faulthandler.cancel_dump_traceback_later()
    print(f"PROBE:{name}=COMPLETED len={r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
