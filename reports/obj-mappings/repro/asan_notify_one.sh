#!/bin/bash
# Run ONE notify-site reproducer under the GIL ASan build N times.
#   asan_notify_one.sh <tag> <script> [args...]
# Writes asan_notify_<tag>.txt in this directory.
cd "$(dirname "$0")" || exit 1
PY=/home/danzin/projects/python_build_matrix/builds/release-gil-nojit-asan/python
export PYTHONMALLOC=malloc
export ASAN_OPTIONS="detect_leaks=0:abort_on_error=0:handle_abort=1"
RUNS="${RUNS:-3}"
tag="$1"; shift
out="asan_notify_${tag}.txt"
{
  echo "### $tag : $* (release-gil-nojit-asan, PYTHONMALLOC=malloc)"
  hits=0
  for i in $(seq 1 "$RUNS"); do
      o=$("$PY" "$@" 2>&1); rc=$?
      if echo "$o" | grep -q "ERROR: AddressSanitizer"; then
          hits=$((hits+1))
          [ "$hits" = 1 ] && echo "$o" | sed -n '1,30p'
      else
          echo "  run $i: no ASan report, rc=$rc"
          [ "$i" = 1 ] && echo "$o" | tail -6 | sed 's/^/    | /'
      fi
  done
  echo "  >>> $hits/$RUNS ASan reports"
} > "$out" 2>&1
