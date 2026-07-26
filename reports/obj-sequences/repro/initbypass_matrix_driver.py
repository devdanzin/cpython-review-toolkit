"""Driver: run every op in initbypass_matrix.py in its own subprocess.

Usage:  python initbypass_matrix_driver.py <python-binary> <ctor> [<ctor> ...]

Prints one row per op:  <op> <rc> <outcome>
and a trailing summary of the crashing ops.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MATRIX = os.path.join(HERE, "initbypass_matrix.py")

sys.path.insert(0, HERE)
import initbypass_matrix as M  # noqa: E402

TAG = {"ba": "ba", "list": "li", "bytes": "by"}


def tag_for(ctor):
    if ctor.startswith("ba"):
        return "ba"
    if ctor.startswith("list"):
        return "li"
    return "by"


def run(binary, ctor):
    tag = tag_for(ctor)
    rows = []
    for op, (_fn, tags) in M.OPS.items():
        if tag not in tags.split():
            continue
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = "0"
        proc = subprocess.run(
            [binary, MATRIX, ctor, op],
            capture_output=True, text=True, timeout=120, env=env,
        )
        line = ""
        for ln in proc.stdout.splitlines():
            if ln.startswith("RESULT:"):
                line = ln.split("=", 1)[1]
        if not line:
            line = "<no output> " + proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "<no output>"
        rows.append((op, proc.returncode, line))
    return rows


def main():
    binary = sys.argv[1]
    for ctor in sys.argv[2:]:
        print("=" * 78)
        print("ctor=%s  binary=%s" % (ctor, binary))
        print("=" * 78)
        rows = run(binary, ctor)
        crashers = []
        for op, rc, line in rows:
            mark = "  " if rc == 0 else "!!"
            print("%s %-18s rc=%-4d %s" % (mark, op, rc, line[:150]))
            if rc != 0:
                crashers.append((op, rc))
        print("-- crashing ops (%d/%d): %s" % (
            len(crashers), len(rows),
            ", ".join("%s(rc=%d)" % c for c in crashers) or "none"))


if __name__ == "__main__":
    main()
