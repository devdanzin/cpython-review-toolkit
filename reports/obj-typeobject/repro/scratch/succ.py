import gc, sys
sys.path.insert(0, "/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad")
import metaalloc
meta = metaalloc.make_meta()
ok = metaalloc.drive_success_path(meta)
print("success-path type built:", ok, file=sys.stderr)
del ok
gc.collect()
print("NO CRASH on success-path teardown", file=sys.stderr)
