"""Collect full crash transcripts for the odict stale-index repro."""

import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
B = Path("/home/danzin/projects/python_build_matrix/builds")
OUT = HERE / "odict_evidence"
OUT.mkdir(exist_ok=True)

env = {"ASAN_OPTIONS": "detect_leaks=0", "PATH": "/usr/bin:/bin", "HOME": "/tmp"}

CASES = [
    ("release-gil-nojit-asan", "grow"),
    ("release-gil-nojit-asan", "delkey"),
    ("release-gil-nojit-asan", "clear"),
    ("debug-gil-nojit-asan", "grow"),
    ("debug-gil-nojit", "grow"),
    ("debug-gil-nojit", "delkey"),
    ("debug-gil-nojit", "clear"),
    ("release-gil-nojit", "grow"),
    ("release-gil-nojit", "delkey"),
    ("release-gil-nojit", "clear"),
]

for build, act in CASES:
    dest = OUT / ("%s__%s.txt" % (build, act))
    try:
        p = subprocess.run(
            [str(B / build / "python"), str(HERE / "odict_resize_stale_index.py"), act, "2000"],
            capture_output=True, text=True, timeout=180, env=env,
        )
        body = "rc=%s\n--- stdout ---\n%s\n--- stderr ---\n%s" % (p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as e:
        body = "rc=TIMEOUT(180s)\n--- stdout ---\n%s\n--- stderr ---\n%s" % (
            (e.stdout or b"").decode(errors="replace"), (e.stderr or b"").decode(errors="replace"))
    dest.write_text(body)
    print("%-30s %-8s -> %s (%d bytes)" % (build, act, dest.name, len(body)), flush=True)
