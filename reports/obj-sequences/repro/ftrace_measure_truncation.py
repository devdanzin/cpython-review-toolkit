"""Measure how much of each slice file tree_sitter_utils.extract_functions sees."""

import sys
from pathlib import Path

sys.path.insert(
    0,
    "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts",
)

import tree_sitter_utils as tsu  # noqa: E402

FILES = [
    "/home/danzin/projects/cpython/Objects/listobject.c",
    "/home/danzin/projects/cpython/Objects/bytesobject.c",
    "/home/danzin/projects/cpython/Objects/bytearrayobject.c",
    "/home/danzin/projects/cpython/Objects/bytes_methods.c",
]


def main() -> None:
    for path in FILES:
        src = Path(path).read_bytes()
        text = src.decode("utf-8", "replace")
        nlines = text.count("\n") + 1
        tree = tsu.parse_bytes(src)
        funcs = tsu.extract_functions(tree, src)
        last = max((f["end_line"] for f in funcs), default=0)
        health = tsu.parse_health(tree, src)
        print(f"=== {path}")
        print(f"    lines={nlines}  functions={len(funcs)}  last_end_line={last}")
        print(f"    parse_health={health}")
        if funcs:
            tail = sorted(funcs, key=lambda f: f["start_line"])[-3:]
            for f in tail:
                print(f"      last fns: {f['name']} {f['start_line']}-{f['end_line']}")


if __name__ == "__main__":
    main()
