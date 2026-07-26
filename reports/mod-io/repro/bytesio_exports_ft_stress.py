"""(b) Is the RELAXED exports counter + check-then-resize atomic under free-threading?

check_exports (bytesio.c:57) reads self->exports with FT_ATOMIC_LOAD_SSIZE_RELAXED.
The increment is FT_ATOMIC_ADD_SSIZE inside Py_BEGIN_CRITICAL_SECTION(source)
(bytesio.c:1291/:1305); the decrement (bytesio.c:1316) is an atomic add with NO
critical section at all.

Two questions, measured separately:

  concurrent   Do a resize (truncate/write/close) and a getbuffer on two threads
               interleave badly?  Both sides are @critical_section on the same
               BytesIO, so if the section is honoured this must stay clean --
               and a crash here would mean the RELAXED load or the unlocked
               decrement is the defect.

  suspend      Same two threads, but the resizing side is truncate(Evil()) whose
               __index__ runs arbitrary Python INSIDE the section.  This is the
               cross-thread expression of the same window the single-threaded
               bytesio_truncate_index_export.py exploits.

Usage: <python> bytesio_exports_ft_stress.py {concurrent|suspend} [rounds]
"""
import sys
import threading

MODE = sys.argv[1] if len(sys.argv) > 1 else "concurrent"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 300

import io

stop = False
errors = []


def resizer(bio):
    for _ in range(ROUNDS):
        if stop:
            return
        try:
            bio.write(b"q" * 4096)
            bio.truncate(0)
            bio.seek(0)
        except BufferError:
            pass
        except Exception as e:  # noqa: BLE001
            errors.append(("resizer", type(e).__name__, str(e)))
            return


def exporter(bio):
    for _ in range(ROUNDS):
        if stop:
            return
        try:
            m = bio.getbuffer()
            if len(m):
                _ = m[0]
                m[0:1] = b"z"
            m.release()
        except (BufferError, ValueError):
            pass
        except Exception as e:  # noqa: BLE001
            errors.append(("exporter", type(e).__name__, str(e)))
            return


def suspender(bio):
    holder = {}

    class Evil:
        def __index__(self):
            try:
                holder["m"] = bio.getbuffer()
            except BufferError:
                holder["m"] = None
            return 0

    for _ in range(ROUNDS):
        if stop:
            return
        try:
            bio.write(b"q" * 65536)
            bio.truncate(Evil())
        except BufferError:
            pass
        except Exception as e:  # noqa: BLE001
            errors.append(("suspender", type(e).__name__, str(e)))
            return
        m = holder.pop("m", None)
        if m is not None:
            try:
                _ = bytes(m)
                m.release()
            except Exception:  # noqa: BLE001
                pass


def main():
    print("gil disabled:", not sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else "n/a")
    for it in range(40):
        bio = io.BytesIO(b"seed" * 4096)
        if MODE == "concurrent":
            ts = [threading.Thread(target=resizer, args=(bio,)),
                  threading.Thread(target=exporter, args=(bio,)),
                  threading.Thread(target=exporter, args=(bio,))]
        else:
            ts = [threading.Thread(target=suspender, args=(bio,)),
                  threading.Thread(target=exporter, args=(bio,))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    print("done, unexpected exceptions:", errors[:5], "count", len(errors))


main()
