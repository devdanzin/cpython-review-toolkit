#!/usr/bin/env bash
# Run one stress script across the build matrix and print exit codes.
# Usage: run_matrix.sh <script.py> [runs]
set -u
B=/home/danzin/projects/python_build_matrix/builds
SCRIPT="$1"
RUNS="${2:-1}"
cd "$(dirname "$0")" || exit 1

for build in debug-ft-nojit release-ft-nojit debug-gil-nojit release-gil-nojit; do
    py="$B/$build/python"
    [ -x "$py" ] || { echo "$build MISSING"; continue; }
    case "$build" in
        *-ft-*) export PYTHON_GIL=0 ;;
        *) unset PYTHON_GIL ;;
    esac
    codes=""
    for _ in $(seq "$RUNS"); do
        timeout 600 "$py" "$SCRIPT" > "/tmp/rm_${build}_$$.out" 2>&1
        codes="$codes $?"
    done
    echo "=== $build : exits$codes"
    grep -E "CRASH|TIMEOUT|FAIL|RESULTS" "/tmp/rm_${build}_$$.out" | head -20
    rm -f "/tmp/rm_${build}_$$.out"
done
