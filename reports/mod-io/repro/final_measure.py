"""Consolidated N-of-M measurement for every confirmed mod-io post-guard NULL site.

Runs each scenario REPS times on debug-gil-nojit and release-gil-nojit, for both
the C backend (io) and the pure-Python oracle (_pyio).
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MATRIX = os.path.expanduser("~/projects/python_build_matrix/builds")

# (label, script, scenario-args, cited C site)
CASES = [
    ("detach            ", "io_postguard_null.py", ["detach_seeded"], "bufferedio.c:625"),
    ("close             ", "io_postguard_null.py", ["close_after_detach"], "bufferedio.c:591"),
    ("seek/closed-prop  ", "io_postguard_null.py", ["seek_after_closed_property"], "bufferedio.c:1389"),
    ("truncate          ", "io_postguard_null2.py", ["truncate"], "bufferedio.c:1485"),
    ("read_all loop     ", "io_postguard_null2.py", ["readall_loop"], "bufferedio.c:1748"),
    ("raw_write loop    ", "io_postguard_null2.py", ["raw_write_loop"], "bufferedio.c:1996"),
    ("flush_rewind seek ", "io_postguard_null2.py", ["flush_rewind_seek"], "bufferedio.c:818"),
    ("textio tell       ", "io_textio_snapshot.py", [], "textio.c:2873"),
    ("bytesio truncate  ", "io_other_types.py", ["bytesio_truncate_index_closes"], "bytesio.c:193"),
    ("bytesio exports   ", "io_bytesio_stale_exports.py", [], "bytesio.c:761 (UAF)"),
]

BUILDS = ["debug-gil-nojit", "release-gil-nojit"]
REPS = int(sys.argv[1]) if len(sys.argv) > 1 else 5


def run(interp, script, args, backend):
    try:
        p = subprocess.run(
            [interp, os.path.join(HERE, script)] + args + [backend],
            capture_output=True, text=True, timeout=90,
        )
        return p.returncode
    except subprocess.TimeoutExpired:
        return "TMO"


def verdict(codes):
    segv = sum(1 for c in codes if c == -11 or c == 139)
    abrt = sum(1 for c in codes if c == -6 or c == 134)
    if segv:
        return f"SIGSEGV {segv}/{len(codes)}"
    if abrt:
        return f"SIGABRT {abrt}/{len(codes)}"
    ok = sum(1 for c in codes if c == 0)
    exc = sum(1 for c in codes if c == 1)
    return f"clean (rc0={ok} rc1={exc})"


print(f"{'case':20s} {'site':22s} {'debug io':18s} {'release io':18s} {'_pyio (rel)':18s}")
print("-" * 100)
for label, script, args, site in CASES:
    row = []
    for build in BUILDS:
        interp = os.path.join(MATRIX, build, "python")
        row.append(verdict([run(interp, script, args, "io") for _ in range(REPS)]))
    rel = os.path.join(MATRIX, "release-gil-nojit", "python")
    row.append(verdict([run(rel, script, args, "_pyio") for _ in range(REPS)]))
    print(f"{label:20s} {site:22s} {row[0]:18s} {row[1]:18s} {row[2]:18s}")
