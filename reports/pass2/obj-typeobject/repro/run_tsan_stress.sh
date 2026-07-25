#!/bin/bash
# Driver: one scenario per process (TSan + os.fork() deadlocks in the
# sanitizer runtime, so STRESS_NO_FORK=1 disables the in-script fork and this
# script provides the process isolation instead).
#
#   ./run_tsan_stress.sh [build_dir]
#
# Default build: release-ft-nojit-tsan from the matrix.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${1:-/home/danzin/projects/python_build_matrix/builds/release-ft-nojit-tsan/python}"
LOGS="$HERE/tsan-logs"
mkdir -p "$LOGS"

export PYTHON_GIL=0
export STRESS_NO_FORK=1
export TSAN_OPTIONS="halt_on_error=0:history_size=4:report_bugs=1"

run() {  # run <script> <scenario>
    local script="$1" sc="$2"
    local tag="${script%.py}__${sc}"
    printf '%-64s ' "$tag"
    timeout 600 "$PY" "$HERE/$script" "$sc" \
        > "$LOGS/$tag.out" 2> "$LOGS/$tag.err"
    local ec=$?
    local races
    races=$(grep -c 'WARNING: ThreadSanitizer: data race' "$LOGS/$tag.err")
    echo "exit=$ec races=$races"
}

run tsan_stress_tp_watched.py    scenario_watch_vs_unwatch
run tsan_stress_tp_watched.py    scenario_watched_bits_vs_notify
run tsan_stress_type_cache.py    scenario_clear_vs_lookup
run tsan_stress_type_cache.py    scenario_clear_vs_fill
run tsan_stress_type_cache.py    scenario_clear_vs_instance_dispatch
run tsan_stress_type_mutation.py scenario_setclass_pingpong
run tsan_stress_type_mutation.py scenario_setbases_shared_type
run tsan_stress_type_mutation.py scenario_mro_recompute_vs_lookup
run tsan_stress_type_mutation.py scenario_watcher_callbacks
run tsan_stress_type_mutation.py scenario_mixed

echo
echo "=== unique race site-pairs across all scenarios ==="
grep -h -A6 'WARNING: ThreadSanitizer: data race' "$LOGS"/*.err 2>/dev/null \
  | grep -oE '(Objects|Python|Modules|Include)/[A-Za-z_./]+\.(c|h):[0-9]+' \
  | sort | uniq -c | sort -rn | head -40
