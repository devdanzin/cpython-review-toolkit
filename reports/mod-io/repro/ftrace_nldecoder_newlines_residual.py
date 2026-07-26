"""ft-race-scanner repro B: gh-144777's fix left the `newlines` getter unguarded.

commit 8db8fc9b510 "gh-144777: Fix data races in IncrementalNewlineDecoder"
added @critical_section to decode / getstate / setstate / reset.  It did NOT
touch incrementalnewlinedecoder_newlines_get (Modules/_io/textio.c:634), a
HAND-WRITTEN getset getter -- so it cannot inherit the clinic guard and takes
none.  It reads self->seennl at :644.

self->seennl is a 3-bit BITFIELD sharing one storage unit with pendingcr:1 and
translate:1 (textio.c:224-226), so the guarded writers
  :511  self->seennl |= seennl;      (decode, now @critical_section)
  :630  self->seennl = 0;            (reset,  now @critical_section)
  :365/:380  self->pendingcr = ...   (decode)
are read-modify-writes of the whole word that the unguarded reader observes.

Expected: TSan race report Modules/_io/textio.c:<decode/reset> vs
incrementalnewlinedecoder_newlines_get.  Exit 66 under
TSAN_OPTIONS=exitcode=66.
"""

import io
import sys
import threading

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
NPAIR = 4

dec = io.IncrementalNewlineDecoder(None, True)
stop = threading.Event()
sink = []


def decoder():
    payload = "a\r\nb\rc\nd\r\n"
    while not stop.is_set():
        dec.decode(payload)
        dec.decode("x\r")
        dec.reset()


def newlines_reader():
    while not stop.is_set():
        sink.append(dec.newlines)
        del sink[:]


ts = []
for _ in range(NPAIR):
    ts.append(threading.Thread(target=decoder, daemon=True))
    ts.append(threading.Thread(target=newlines_reader, daemon=True))
for t in ts:
    t.start()
stop.wait(DUR)
stop.set()
for t in ts:
    t.join(5.0)
print("done")
