#!/bin/bash
# usage: run_matrix.sh <script.py> [extra args...]
# Runs one reproducer across the GIL debug/release builds plus the _pyio oracle,
# printing the exit code (139 = SIGSEGV, 134 = SIGABRT) for each.
S="$1"; shift
BM=~/projects/python_build_matrix/builds
for b in debug-gil-nojit release-gil-nojit; do
    out=$("$BM/$b/python" "$S" "$@" 2>&1)
    rc=$?
    echo "--- $b rc=$rc"
    echo "$out" | tail -4
done
out=$("$BM/release-gil-nojit/python" "$S" --pyio "$@" 2>&1)
rc=$?
echo "--- _pyio oracle (release-gil-nojit) rc=$rc"
echo "$out" | tail -4
