"""Run every case of io_buffered_stale_raw_sweep.py in its own subprocess on
several builds, and tabulate the exit codes.

  rc=0   survived
  rc=134 SIGABRT (debug assertion / Fatal Python error)
  rc=139 SIGSEGV

usage: python sweep_driver.py [sweep_script.py]
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, sys.argv[1] if len(sys.argv) > 1
                      else "io_buffered_stale_raw_sweep.py")
BM = os.path.expanduser("~/projects/python_build_matrix/builds")
BUILDS = ["debug-gil-nojit", "release-gil-nojit"]

drv = sys.executable
cases = subprocess.run([os.path.join(BM, BUILDS[1], "python"), SCRIPT, "--list"],
                       capture_output=True, text=True).stdout.split()


def run(build, case, pyio=False):
    cmd = [os.path.join(BM, build, "python"), SCRIPT, "--case", case]
    if pyio:
        cmd.append("--pyio")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    last = [ln for ln in p.stderr.splitlines() if ln.strip()]
    tail = last[-1] if last else ""
    return p.returncode, tail


print("%-28s %-10s %-10s  %s" % ("case", "debug", "release", "_pyio oracle"))
print("-" * 100)
for case in cases:
    row = []
    for b in BUILDS:
        try:
            rc, tail = run(b, case)
        except subprocess.TimeoutExpired:
            rc, tail = "TIMEOUT", ""
        row.append((rc, tail))
    try:
        prc, ptail = run(BUILDS[1], case, pyio=True)
    except subprocess.TimeoutExpired:
        prc, ptail = "TIMEOUT", ""
    flag = "  <<< CRASH" if any(r[0] not in (0,) for r in row) else ""
    print("%-28s rc=%-7s rc=%-7s  rc=%-4s %s%s"
          % (case, row[0][0], row[1][0], prc, ptail[:44], flag))
    if flag:
        print("      debug : %s" % row[0][1][:110])
        print("      pyio  : %s" % ptail[:110])
