#!/bin/bash
# Full build matrix for every CONFIRMED init-bypass / stale-guard crash in the
# mod-io slice.  Prints the real exit code (139 SIGSEGV, 134 SIGABRT, 0 clean).
D=/home/danzin/projects/cpython-review-toolkit/reports/mod-io/repro
B=/home/danzin/projects/python_build_matrix/builds
B314=/home/danzin/projects/python_build_matrix/builds-3.14
cd "$D" || exit 1

run() {  # run <label> <script> <scenario>
  printf "%-24s" "$1"
  for b in debug-gil-nojit release-gil-nojit debug-ft-nojit release-ft-nojit; do
    timeout 30 "$B/$b/python" "$2" "$3" >/dev/null 2>&1
    printf " %s=%-4s" "${b%%-nojit}" "$?"
  done
  timeout 30 "$B314/release-gil-nojit/python" "$2" "$3" >/dev/null 2>&1
  printf " 3.14rel=%-4s" "$?"
  PYIO=1 timeout 30 "$B/release-gil-nojit/python" "$2" "$3" >/dev/null 2>&1
  printf " _pyio=%s\n" "$?"
}

echo "site                     <----------- 3.16.0a0 main -----------> <3.14> <oracle>"
run "bufio:591 close"      io_buffered_reentrant_detach.py  close
run "bufio:623 detach"     io_buffered_reentrant_detach.py  detach
run "bufio:788 raw_tell"   io_initbypass_residual.py        raw_tell_via_truncate
run "bufio:818 raw_seek"   io_initbypass_residual.py        raw_seek_systemerror
run "bufio:1389 seekable"  io_buffered_scanner_gaps.py      seek_seekable
run "bufio:1485 truncate"  io_buffered_reentrant_detach2.py truncate
run "bufio:1640 rawread"   io_buffered_reentrant_detach2.py reader_fill
run "bufio:1713 getattr"   io_buffered_scanner_gaps.py      readall_getattr
run "bufio:1748 read_all"  io_buffered_reentrant_detach2.py read_all
run "bufio:1996 rawwrite"  io_buffered_reentrant_detach2.py writer_loop
run "textio:1365 reconfig" io_reconfigure_newbypass.py      newline
run "textio:2775 seekdec"  io_textio_seek_null_decoder.py   seek_null_decoder
echo "-- controls (must be clean) --"
run "bufio:489 deallocwarn" io_buffered_scanner_gaps.py     dealloc_warn
run "bufio:517 simpleflush" io_buffered_reentrant_detach2.py simple_flush
run "bufio:644 inquiries"   io_buffered_reentrant_detach2.py seekable
run "textio:2857 tell"      io_textio_seek_null_decoder.py  tell_null_decoder
run "textio:2740 seek0skip" io_textio_seek_null_decoder.py  seek_zero_skip
