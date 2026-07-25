"""Run odict_resize_stale_index.py across builds x actions, N times each."""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILDS = Path("/home/danzin/projects/python_build_matrix/builds")
SCRIPT = HERE / "odict_resize_stale_index.py"

builds = sys.argv[1].split(",") if len(sys.argv) > 1 else ["release-gil-nojit-asan"]
actions = sys.argv[2].split(",") if len(sys.argv) > 2 else ["none", "grow", "delkey", "clear"]
runs = int(sys.argv[3]) if len(sys.argv) > 3 else 5
fillers = sys.argv[4] if len(sys.argv) > 4 else "2000"

env = {"ASAN_OPTIONS": "detect_leaks=0", "PATH": "/usr/bin:/bin", "HOME": "/tmp"}
results = {}
for b in builds:
    exe = BUILDS / b / "python"
    for act in actions:
        key = "%s/%s" % (b, act)
        outcomes = []
        first = None
        for _ in range(runs):
            try:
                p = subprocess.run(
                    [str(exe), str(SCRIPT), act, fillers],
                    capture_output=True, text=True, timeout=40, env=env,
                )
                rc, out = p.returncode, (p.stdout + p.stderr)
            except subprocess.TimeoutExpired as e:
                rc, out = "TIMEOUT", (e.stdout or b"").decode() + (e.stderr or b"").decode()
            tag = "rc=%s" % rc
            for marker in ("AddressSanitizer: heap-buffer-overflow",
                           "AddressSanitizer: heap-use-after-free",
                           "AddressSanitizer: SEGV",
                           "Assertion", "Segmentation fault", "SURVIVED"):
                if marker in out:
                    tag += " " + marker
                    break
            outcomes.append(tag)
            if first is None:
                first = out
        results[key] = {"outcomes": outcomes, "first": first[-6000:] if first else ""}
        print("%-45s %s" % (key, outcomes), flush=True)

(HERE / "odict_matrix_results.json").write_text(json.dumps(results, indent=1))
