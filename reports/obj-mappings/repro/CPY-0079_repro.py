"""CPY-0079 standalone reproducer.

    <build>/python CPY-0079_repro.py [N]

Fails exactly allocation N (default 1) after arming, then runs `{}.copy()`.
The empty-dict arm of copy_lock_held_untracked (Objects/dictobject.c:4485-4496)
forwards the result of dict_new_untracked / frozendict_new_untracked with no
NULL check; anydict_new_untracked:5362 is the guarded twin one frame down.
"""

import sys
import faulthandler

import _testcapi

n = int(sys.argv[1]) if len(sys.argv) > 1 else 1

empty_dict = {}
empty_frozen = frozendict()


def exercise():
    empty_dict.copy()
    empty_frozen.copy()


# Warm: compile, specialise, fill freelists -- unarmed.
for _ in range(3):
    exercise()

faulthandler.enable()
_testcapi.set_nomemory(n, n + 1)
try:
    exercise()
except MemoryError:
    print("clean MemoryError at n=%d" % n)
else:
    print("completed at n=%d" % n)
