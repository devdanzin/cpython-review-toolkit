import json
import sys
from pathlib import Path

d = json.loads((Path(__file__).resolve().parent / "odict_matrix_results.json").read_text())
for key in sys.argv[1:] or sorted(d):
    print("=" * 70)
    print(key, d[key]["outcomes"])
    print("-" * 70)
    print(d[key]["first"][:4500])
