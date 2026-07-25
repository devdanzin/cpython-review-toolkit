"""Fresh OOM sweeps of every pass-2 region of Objects/typeobject.c.

Run AFTER the D-14 harness fix: each sweep is preceded by an unarmed dry run, so
a broken setup is reported as an error instead of as a clean bill of health.

    python sweep_regions.py <build-name> [max_n]
"""

import json
import sys
from pathlib import Path

sys.path.insert(
    0,
    "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts",
)

import run_oom_sweep as R  # noqa: E402

HERE = Path(__file__).resolve().parent
UD = HERE.parent / "uninit-dealloc"
BUILDS = Path("/home/danzin/projects/python_build_matrix/builds")

BUILD = sys.argv[1]
MAX_N = int(sys.argv[2]) if len(sys.argv) > 2 else 220
PY = str(BUILDS / BUILD / "python")

# (label, typeobject.c region, setup file, payload file)
CASES = [
    ("mro-c3", "3217-3702", UD / "p2_setup.py", UD / "p2_bases.py"),
    ("setclass", "7482-7846", UD / "p2_setup.py", UD / "p2_setclass.py"),
    ("pickle", "7848-8406", UD / "p2_setup.py", UD / "p2_pickle.py"),
    (
        "lookup-getattro",
        "6140-6848",
        UD / "p2_setup.py",
        UD / "p2_super_lookup.py",
    ),
    ("watchers", "971-1481", UD / "p2_watch_setup.py", UD / "p2_watch.py"),
    ("static-types", "228-522", HERE / "p2_static_setup.py", HERE / "p2_static.py"),
    ("wide", "all", UD / "p2_setup.py", UD / "p2_wide.py"),
]

out = {}
for label, region, setup_p, payload_p in CASES:
    setup = setup_p.read_text()
    payload = payload_p.read_text()
    res = R.sweep(PY, payload, setup=setup, max_n=MAX_N, timeout=60.0)
    if "error" in res:
        print(f"{label:<16} region {region:<10} ERROR: {res['error']}")
        print(f"  dry-run stderr: {(res.get('dry_run') or {}).get('stderr', '')[-600:]}")
        out[label] = {"error": res["error"], "dry_run": res.get("dry_run")}
        continue
    counts = res["outcome_counts"]
    idx = res["summary"]["crash_indices"]
    print(
        f"{label:<16} region {region:<10} dry_run=ok  {counts}"
        f"{'  CRASHES @ ' + str(idx) if idx else ''}"
    )
    out[label] = {
        "region": region,
        "counts": counts,
        "crash_indices": idx,
        "crashes": [
            {"n": c["n"], "outcome": c["outcome"], "stderr_head": c["stderr"][:1200]}
            for c in res["crashes"]
        ],
    }

dest = HERE / f"sweep_{BUILD}.json"
dest.write_text(json.dumps(out, indent=2))
print(f"\nwrote {dest}")
