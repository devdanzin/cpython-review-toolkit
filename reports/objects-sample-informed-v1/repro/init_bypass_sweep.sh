#!/bin/bash
cd /home/danzin/projects/cpython
export ASAN_OPTIONS=detect_leaks=0
SC=/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad
TYPES="tuple tuple_iterator GenericAlias UnionType Template Interpolation template_iter property mappingproxy method_descriptor classmethod_descriptor getset_descriptor member_descriptor wrapper_descriptor method_wrapper OrderedDict odict_iterator odict_keys odict_items odict_values function classmethod staticmethod weakref_ref weakref_proxy weakref_callableproxy stat_result version_info struct_time seq_iterator callable_iterator cell"
ACTIONS="getattr-all callattr-all repr str hash iter call len get set delete eq lt getitem reduce copy deepcopy bool next sizeof keys contains setattr index format dir gc"
for t in $TYPES; do
  for mode in new subclass; do
    for a in $ACTIONS; do
      out=$(timeout 20 ./python $SC/probe2.py "$t" "$mode" "$a" 2>&1); rc=$?
      if [ $rc -gt 1 ] && [ $rc -ne 124 ]; then
        echo "### rc=$rc  $t / $mode / $a"
        echo "$out" | grep -v "^ALLOC-OK$" | head -8
      fi
    done
  done
done
echo "=== SWEEP DONE ==="
