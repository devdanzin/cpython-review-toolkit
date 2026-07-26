#!/bin/bash
# usage: run_scenarios_initbypass.sh <script.py>
# Runs EVERY scenario of a multi-scenario reproducer on debug-gil-nojit and
# release-gil-nojit plus the _pyio oracle, printing the real exit code
# (139 = SIGSEGV, 134 = SIGABRT).  Written for the init-bypass-checker agent.
S="$1"
D=/home/danzin/projects/cpython-review-toolkit/reports/mod-io/repro
B=/home/danzin/projects/python_build_matrix/builds
cd "$D" || exit 1
for s in $("$B/release-gil-nojit/python" "$S" --list | awk '{print $1}'); do
  for b in debug-gil-nojit release-gil-nojit; do
    out=$(timeout 20 "$B/$b/python" "$S" "$s" 2>&1)
    rc=$?
    printf "%-22s %-20s rc=%-4s %s\n" "$s" "$b" "$rc" "$(echo "$out" | tail -1 | cut -c1-110)"
  done
  out=$(PYIO=1 timeout 20 "$B/release-gil-nojit/python" "$S" "$s" 2>&1)
  rc=$?
  printf "%-22s %-20s rc=%-4s %s\n" "$s" "_pyio-oracle" "$rc" "$(echo "$out" | tail -1 | cut -c1-110)"
  echo
done
