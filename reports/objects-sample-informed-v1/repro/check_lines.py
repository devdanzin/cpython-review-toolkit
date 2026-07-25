"""Measure how accurate scan_null_checks.py's reported line numbers are."""
import json
import pathlib
import re

CPY = pathlib.Path("/home/danzin/projects/cpython")
RUN = pathlib.Path(
    "/home/danzin/projects/cpython-review-toolkit/reports/objects-sample-informed-v1")


def check(path, label):
    data = json.loads(path.read_text())
    exact = off = missing = 0
    rows = []
    for f in data["findings"]:
        src = (CPY / f["file"]).read_text(errors="replace").splitlines()
        pat = re.compile(re.escape(f["variable"]) + r"\s*=\s*(?:\([^)]*\)\s*)?"
                         + re.escape(f["api_call"]) + r"\s*\(")
        ln = f["line"]
        if 1 <= ln <= len(src) and pat.search(src[ln - 1]):
            exact += 1
            rows.append((f["file"], ln, 0))
            continue
        # find the nearest real site
        best = None
        for i, line in enumerate(src, 1):
            if pat.search(line):
                d = i - ln
                if best is None or abs(d) < abs(best[1]):
                    best = (i, d)
        if best is None:
            missing += 1
            rows.append((f["file"], ln, None))
        else:
            off += 1
            rows.append((f["file"], ln, best[1]))
    print(f"--- {label}: {len(data['findings'])} findings")
    print(f"    line exact:   {exact}")
    print(f"    line WRONG:   {off}")
    print(f"    no site found:{missing}")
    deltas = [d for _, _, d in rows if d]
    if deltas:
        print(f"    offsets seen: {sorted(set(deltas))}")
    return rows


rows = check(RUN / "scanners/scan_null_checks.sample.json", "sample (14 files)")
print()
for r in rows:
    print(f"    {r[0]}:{r[1]}  delta={r[2]}")
print()
check(RUN / "scanners/scan_null_checks.Objects.json", "all of Objects/")
