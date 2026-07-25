import re
import pathlib

p = pathlib.Path("/home/danzin/projects/cpython/Objects/typeobject.c")
lines = p.read_text().split("\n")

danger = [
    "ASSERT_TYPE_LOCK_HELD",
    "ASSERT_NEW_TYPE_OR_LOCKED",
    "ASSERT_WORLD_STOPPED_OR_NEW_TYPE",
    "COPYSLOT",
    "COPYNUM",
    "COPYSEQ",
    "COPYMAP",
    "COPYBUF",
    "COPYASYNC",
    "COPYVAL",
    "BEGIN_TYPE_DICT_LOCK",
    "BEGIN_TYPE_LOCK",
    "END_TYPE_LOCK",
    "END_TYPE_DICT_LOCK",
]

print("=== A. statement-macro used as the UNBRACED body of if/else/for/while ===")
hits = 0
for i, ln in enumerate(lines, 1):
    if re.match(r"\s*#\s*(define|undef)", ln):
        continue
    for d in danger:
        if not re.search(r"\b" + d + r"\s*\(", ln):
            continue
        prev = lines[i - 2].strip() if i >= 2 else ""
        nxt = lines[i].strip() if i < len(lines) else ""
        bad_prev = bool(re.search(r"\b(if|else|for|while)\b[^;{]*\)\s*$", prev)) or prev.endswith("else")
        bad_next = nxt.startswith("else")
        same_line = bool(re.search(r"\b(if|else|for|while)\s*\(.*\)\s*" + d + r"\s*\(", ln))
        if bad_prev or bad_next or same_line:
            hits += 1
            print("  %d: %s" % (i, ln.strip()))
            print("      prev=%r" % prev)
            print("      next=%r" % nxt)
print("  total: %d" % hits)

for name, note in [
    ("MCACHE_CACHEABLE_NAME", "arg evaluated TWICE"),
    ("MCACHE_HASH_METHOD", "type/name once each"),
    ("BEGIN_TYPE_DICT_LOCK", "arg DISCARDED entirely in the GIL build"),
    ("TYPE_IS_REVEALED", "arg DISCARDED in the 32-bit / GIL build"),
    ("NEXT_VERSION_TAG", "lvalue macro"),
]:
    print()
    print("=== %s call sites (%s) ===" % (name, note))
    for i, ln in enumerate(lines, 1):
        if name in ln and not re.match(r"\s*#\s*(define|undef)", ln):
            print("  %d: %s" % (i, ln.strip()))
