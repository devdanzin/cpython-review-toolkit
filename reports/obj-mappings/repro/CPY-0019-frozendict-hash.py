"""CPY-0019 / gh-154318 confirmation -- frozendict_pair_hash (dictobject.c:8427).

`frozendict_hash` (dictobject.c:8446) reads the *cached* key hash out of
_PyDict_Next, exactly like its source `frozenset_hash_impl`
(setobject.c:989).  The copy adds a second descent axis the original does not
have: `frozendict_pair_hash(key_hash, value)` calls PyObject_Hash(value) at
:8427, and PyObject_Hash (Objects/object.c:1158) has NO recursion guard.

Values are never hashed on insertion, so nothing is memoised: one hash() call
walks the whole chain.  Native C-stack overflow -> SIGSEGV, not RecursionError.

Depth matters: 200_000 SURVIVES on release-gil-nojit with `ulimit -s 16384`;
1_000_000 segfaults.  Pass the depth as argv[1].

    $ ~/projects/python_build_matrix/builds/release-gil-nojit/python \
          CPY-0019-frozendict-hash.py 1000000
    Segmentation fault (core dumped)
"""

import sys

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000

d = frozendict({})
for _ in range(N):
    d = frozendict({0: d})

print(f"built depth={N}; nothing hashed yet (values are not hashed on insert)",
      flush=True)
print("hash ->", hash(d), flush=True)
print("SURVIVED", flush=True)
