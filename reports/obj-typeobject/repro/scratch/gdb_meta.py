import sys

sys.path.insert(0, "/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/"
                   "ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad")
import _testcapi
import metaalloc

meta = metaalloc.make_meta()
metaalloc.probe(meta)          # warm, unarmed
_testcapi.set_nomemory(5, 0)   # the reproducing index from the sweep
metaalloc.probe(meta)
print("no crash")
