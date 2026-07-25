import re
src = open('/home/danzin/projects/cpython/Objects/typeobject.c').read().split('\n')
print("=== open-coded clear-then-decref ===")
for i, l in enumerate(src):
    if re.search(r'->\s*\w+\s*=\s*NULL\s*;', l):
        for j in range(i + 1, min(i + 5, len(src))):
            m2 = re.search(r'Py_X?DECREF\(\s*([A-Za-z_]\w*)\s*\)', src[j])
            if m2:
                print(f"{i+1}: {l.strip()}   ==> {j+1}: {src[j].strip()}")
                break
