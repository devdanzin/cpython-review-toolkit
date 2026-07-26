"""Repair the tree-sitter-hostile bare-METHODDEF tables, then re-run scan_ft_races.

The repair replaces every line that consists solely of a SCREAMING_CASE
identifier (a bare Argument-Clinic METHODDEF macro inside a PyMethodDef
initialiser list) with `{0},`.  Line count and every other byte offset per line
is preserved, so line numbers in findings stay valid against the real file.
"""

import json
import re
import shutil
import sys
from pathlib import Path

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)

import scan_ft_races  # noqa: E402
import tree_sitter_utils as tsu  # noqa: E402

BARE_MACRO_LINE = re.compile(r"^([ \t]*)([A-Z][A-Z0-9_]*(?:_METHODDEF|_GETSETDEF))[ \t]*$")

SLICE = [
    "Objects/listobject.c",
    "Objects/bytesobject.c",
    "Objects/bytearrayobject.c",
    "Objects/bytes_methods.c",
]

CPY = Path("/home/danzin/projects/cpython")


def repair(text: str) -> tuple[str, int]:
    out = []
    n = 0
    for line in text.split("\n"):
        m = BARE_MACRO_LINE.match(line)
        if m:
            out.append(m.group(1) + "{0},")
            n += 1
        else:
            out.append(line)
    return "\n".join(out), n


def main() -> None:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ftrepair")
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "Objects").mkdir(parents=True)
    for rel in SLICE:
        src = (CPY / rel).read_text(encoding="utf-8", errors="replace")
        fixed, n = repair(src)
        (dest / rel).write_text(fixed, encoding="utf-8")
        b = fixed.encode("utf-8")
        tree = tsu.parse_bytes(b)
        funcs = tsu.extract_functions(tree, b)
        health = tsu.parse_health(tree, b)
        print(
            f"{rel}: repaired {n} bare-macro lines; "
            f"functions {len(funcs)} coverage {health['coverage']} "
            f"last_end {max((f['end_line'] for f in funcs), default=0)}"
        )
    print()
    res = scan_ft_races.analyze(str(dest))
    print(json.dumps(res, indent=2)[:20000])


if __name__ == "__main__":
    main()
