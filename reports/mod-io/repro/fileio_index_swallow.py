"""fileio.c:296-304 -- FileIO.__init__ clears whatever __index__ raised.

    fd = PyLong_AsInt(nameobj);        /* :296 calls __index__ */
    if (fd < 0) {
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_ValueError, "negative file descriptor");
            return -1;
        }
        PyErr_Clear();                 /* :303 -- unnarrowed */
    }
    if (fd < 0) {
        ... PyUnicode_FSConverter(nameobj, &stringobj) ...   /* try as a path */
    }

The clear is the "not an int, try it as a path" step of a two-interpretation
constructor, which is a legitimate pattern. The question is whether it should be
narrowed to the TypeError that actually signals "no __index__", instead of
discarding everything a user's __index__ can raise.

    python fileio_index_swallow.py [--pyio]
"""

import sys

if "--pyio" in sys.argv:
    import _pyio as io

    BACKEND = "_pyio"
else:
    import io

    BACKEND = "_io"


def probe(label, exc_factory):
    class Hostile:
        def __index__(self):
            raise exc_factory()

    try:
        io.FileIO(Hostile())
    except BaseException as exc:
        got = "%s: %s" % (type(exc).__name__, str(exc)[:58])
    else:
        got = "no exception"
    raised = exc_factory()
    survived = type(raised).__name__ in got
    print(
        "  %-18s raised %-18s -> %-62s %s"
        % (label, type(raised).__name__, got, "PRESERVED" if survived else "SWALLOWED"),
        file=sys.stderr,
    )


def main():
    print("backend: %s" % BACKEND, file=sys.stderr)
    probe("ordinary error", lambda: ZeroDivisionError("from __index__"))
    probe("interpreter exit", lambda: KeyboardInterrupt("user pressed ^C"))
    probe("resource exhaustion", lambda: MemoryError("out of memory"))
    probe("system exit", lambda: SystemExit(3))
    probe("recursion", lambda: RecursionError("too deep"))
    # The control: no __index__ at all is the case the clear exists to handle.
    class NoIndex:
        pass

    try:
        io.FileIO(NoIndex())
    except BaseException as exc:
        print(
            "  %-18s %-18s -> %s: %s"
            % ("control (no dunder)", "", type(exc).__name__, str(exc)[:58]),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
