#!/bin/bash
# ASan sweep for the notify-site reproducers.  GIL build only: the FT ASan
# builds have no shadow for the object heap.
cd "$(dirname "$0")" || exit 1
PY=/home/danzin/projects/python_build_matrix/builds/release-gil-nojit-asan/python
export PYTHONMALLOC=malloc
export ASAN_OPTIONS="detect_leaks=0:abort_on_error=0:handle_abort=1"
RUNS="${RUNS:-3}"

run() {
    local name="$1"; shift
    echo "######## $name : $* ########"
    local hits=0
    for i in $(seq 1 "$RUNS"); do
        out=$("$PY" "$@" 2>&1)
        rc=$?
        line=$(echo "$out" | grep -m1 "ERROR: AddressSanitizer")
        if [ -n "$line" ]; then
            hits=$((hits+1))
            if [ "$hits" = 1 ]; then
                echo "$out" | sed -n '1,32p'
            fi
        else
            echo "  run $i: no ASan report, rc=$rc"
        fi
    done
    echo "  >>> $hits/$RUNS ASan reports"
    echo
}

run 3307 notify_site_3307_pop_knownhash.py clear
run 5066 notify_site_5066_popitem_general.py clear
run 3083 notify_site_3083_delitemif.py clear
run 2003_mod_detached notify_site_1997_2003_insert_split_value.py mod_detached
run 1997_add_detached notify_site_1997_2003_insert_split_value.py add_detached
run 2003_mod_clear notify_site_1997_2003_insert_split_value.py mod_clear
run 7510_mod notify_site_7510_store_instance_attr.py mod
run 7510_del notify_site_7510_store_instance_attr.py del
run 7510_detach notify_site_7510_store_instance_attr.py detach
run 2060_regrow_int notify_site_2060_insertdict_modified.py regrow_int
run 4234_insert notify_site_4234_clone_combined.py insert
run 4234_clearsrc notify_site_4234_clone_combined.py clearsrc
