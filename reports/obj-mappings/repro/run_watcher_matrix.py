"""N/M matrix for the watcher NULL-callback reproducer."""

import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
B = Path("/home/danzin/projects/python_build_matrix/builds")
SCRIPT = HERE / "watcher_null_callback_reentrant_clear.py"
env = {"ASAN_OPTIONS": "detect_leaks=0", "PATH": "/usr/bin:/bin", "HOME": "/tmp"}

for build in ["release-gil-nojit", "debug-gil-nojit", "release-gil-nojit-asan",
              "debug-gil-nojit-asan", "release-ft-nojit", "debug-ft-nojit"]:
    exe = B / build / "python"
    if not exe.exists():
        print("%-24s MISSING" % build)
        continue
    tags = []
    last = ""
    for _ in range(5):
        p = subprocess.run([str(exe), str(SCRIPT)], capture_output=True, text=True,
                           timeout=120, env=env)
        out = p.stdout + p.stderr
        last = out
        t = "rc=%d" % p.returncode
        if "Assertion `cb != NULL'" in out or "cb != NULL" in out:
            t += " ASSERT(cb!=NULL)"
        elif "SURVIVED" in out:
            t += " SURVIVED"
        elif p.returncode == -11:
            t += " SIGSEGV"
        tags.append(t)
    print("%-24s %s" % (build, tags), flush=True)
    (HERE / ("watcher_null__%s.txt" % build)).write_text(last)
