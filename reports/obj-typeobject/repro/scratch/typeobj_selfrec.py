import re

path = "/home/danzin/projects/cpython/Objects/typeobject.c"
src = open(path).read().split("\n")

funcs = []
for i, l in enumerate(src):
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(", l)
    if m and i > 0:
        funcs.append((m.group(1), i + 1))

print("candidate function defs:", len(funcs))

rows = []
for name, ln in funcs:
    j = ln
    while j < len(src) and not src[j].startswith("}"):
        j += 1
    body = "\n".join(src[ln:j + 1])
    n = len(re.findall(r"\b" + re.escape(name) + r"\s*\(", body))
    if n > 0:
        rows.append((ln, j + 1, name, n))

rows.sort()
for a, b, name, n in rows:
    print(f"{name:45s} lines {a}-{b}  selfcalls={n}")
