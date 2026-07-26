"""Does upstream's new test_reinit_with_view actually catch the bug it was written for?

PR #153498 (gh-153419, merged 2026-07-24) added:

    def test_reinit_with_view(self):
        a = bytearray()
        with memoryview(a):
            self.assertRaises(BufferError, a.__init__, "x", "ascii")
        self.assertEqual(a, b"")

The claim under test: "x" encodes to the INTERNED one-character bytes object, which
fails _PyObject_IsUniquelyReferenced and so takes a different path that happens to
raise BufferError even WITHOUT the fix -- meaning the regression test passes on the
unfixed code and would not have caught the regression.

The build matrix is at a1d580430c8, which predates the fix, so this runs against
unfixed code.

    python test_reinit_with_view_efficacy.py
"""

import sys


def attempt(src, enc="ascii"):
    a = bytearray()
    mv = memoryview(a)
    raised = None
    try:
        a.__init__(src, enc)
    except BaseException as exc:
        raised = type(exc).__name__
    # Read the contents BEFORE releasing the view.
    try:
        after = bytes(a)
    except BaseException as exc:
        after = "<%s>" % type(exc).__name__
    mv.release()
    return raised, after


def main():
    print("interpreter: %s" % sys.version.split()[0], file=sys.stderr)
    print(file=sys.stderr)
    print("  %-8s %-14s %-14s %s" % ("source", "raised", "a after", "verdict"),
          file=sys.stderr)
    for src in ("x", "xy", "xyz", "", "abcdefgh"):
        raised, after = attempt(src)
        if raised == "BufferError":
            verdict = "guarded"
        elif after == b"":
            verdict = "no raise, but unchanged"
        else:
            verdict = "*** MUTATED UNDER A LIVE VIEW ***"
        print("  %-8r %-14s %-14r %s" % (src, raised or "-", after, verdict),
              file=sys.stderr)
    print(file=sys.stderr)
    print("If 'x' says guarded and a longer string says MUTATED, upstream's",
          file=sys.stderr)
    print("regression test passes on the unfixed code it was written for.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
