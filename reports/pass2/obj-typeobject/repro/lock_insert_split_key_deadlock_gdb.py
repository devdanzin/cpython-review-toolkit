"""Same as lock_insert_split_key_deadlock.py but arms SIGALRM before the
trigger so that gdb can stop the inferior while it is parked on dk_mutex
and print the native backtrace (ptrace attach-to-running is blocked here).
"""

import signal
import sys

import _testcapi


class C:
    pass


def main():
    wid = _testcapi.add_type_watcher(1)
    _testcapi.watch_type(wid, C)

    victim = C()
    reentered = []

    def hook(unraisable):
        if reentered:
            return
        reentered.append(1)
        print("  [hook] re-entering insert_split_key with dk_mutex held", flush=True)
        signal.alarm(8)          # fire while parked on the mutex
        victim.second_attribute = 2
        print("  [hook] returned (NO deadlock)", flush=True)

    sys.unraisablehook = hook
    obj = C()
    obj.first_attribute = 1
    print("[main] completed without deadlock", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
