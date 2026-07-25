import faulthandler
import gc
import sys

faulthandler.enable()
sys.path.insert(0, "/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/"
                   "ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad")
import metaalloc

meta = metaalloc.make_meta()
print("metaclass built:", meta, file=sys.stderr)

# The probe: a post-ownership-transfer `goto finally` inside
# type_from_slots_or_spec -> Py_CLEAR(res).  The half-built type is cyclic
# (tp_mro[0] is the type itself), so it is reclaimed by the GC, not
# synchronously -- collect explicitly.
metaalloc.drive_error_path(meta)
print("error path returned; forcing gc.collect()", file=sys.stderr)
gc.collect()
print("NO CRASH after collect", file=sys.stderr)
