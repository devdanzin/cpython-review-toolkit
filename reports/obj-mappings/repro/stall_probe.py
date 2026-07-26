"""Run the CPY-0127 gcobjects workload with faulthandler armed, to see WHERE the
long-running runs are spending their time.

ptrace_scope=1 on this machine blocks `gdb -p`, so this uses
faulthandler.dump_traceback_later(), which dumps every thread's Python-level
stack from inside the process and then exits.

    <ft-python> stall_probe.py <dump_after_seconds>
"""
import faulthandler
import runpy
import sys

faulthandler.dump_traceback_later(float(sys.argv[1]), exit=True)
sys.argv = ["CPY-0127_gc_tp_clear_vs_mutator.py", "60", "4", "gcobjects"]
runpy.run_path("CPY-0127_gc_tp_clear_vs_mutator.py", run_name="__main__")
