"""Sweep locked regions in typeobject.c for refcount drops that could run a
user __del__ while a lock is held (the CPY-0028 / self-deadlock shape)."""

import re

PATH = "/home/danzin/projects/cpython/Objects/typeobject.c"
LINES = open(PATH, encoding="utf-8", errors="replace").read().splitlines()
DEFINE = re.compile(r"^\s*#\s*define\b")
DROP = re.compile(r"\bPy_(X?DECREF|CLEAR|SETREF|XSETREF)\b")

FAMILIES = [
    ("BEGIN_TYPE_LOCK", "END_TYPE_LOCK"),
    ("BEGIN_TYPE_DICT_LOCK", "END_TYPE_DICT_LOCK"),
    ("Py_BEGIN_CRITICAL_SECTION", "Py_END_CRITICAL_SECTION"),
    ("types_stop_world", "types_start_world"),
]


def main() -> None:
    pairs = []
    for begin, end in FAMILIES:
        stack = []
        for i, line in enumerate(LINES, 1):
            if DEFINE.match(line):
                continue
            if re.search(rf"\b{begin}\b", line):
                stack.append(i)
            if re.search(rf"\b{end}\b", line) and stack:
                pairs.append((stack.pop(), i, begin))

    found = False
    for start, end, name in sorted(pairs):
        hits = []
        for off, text in enumerate(LINES[start:end - 1]):
            stripped = text.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if DROP.search(text):
                hits.append((start + 1 + off, stripped))
        if hits:
            found = True
            print(f"\n-- {name} region {start}..{end}")
            for ln, text in hits:
                print(f"   {ln}: {text[:100]}")
    if not found:
        print("no refcount drops inside any locked region")


if __name__ == "__main__":
    main()
