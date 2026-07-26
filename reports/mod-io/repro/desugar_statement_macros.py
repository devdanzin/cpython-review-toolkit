"""Correct desugaring: add a trailing ';' ONLY to a whole-line statement-macro
invocation. v1 used [^;{}]* which matches newlines, so a single match spanned
three lines and appended ';' after `if (!ENTER_BUFFERED(self))` -- changing the
semantics and manufacturing false positives. The negated class must exclude \\n.
"""

import re
import sys

MACROS = ["LEAVE_BUFFERED", "CHECK_INITIALIZED_INT", "CHECK_INITIALIZED",
          "CHECK_CLOSED", "CHECK_ATTACHED_INT", "CHECK_ATTACHED"]

PAT = re.compile(
    r"^([ \t]*(?:" + "|".join(MACROS) + r")\([^;{}\n]*\))[ \t]*$", re.MULTILINE)

src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
fixed, n = PAT.subn(r"\1;", text)

# sanity: the transform must not touch any line containing `if (`
for a, b in zip(text.splitlines(), fixed.splitlines()):
    if a != b:
        assert "if (" not in a, f"REGRESSION: rewrote a conditional: {a!r}"
assert len(text.splitlines()) == len(fixed.splitlines())

open(dst, "w", encoding="utf-8").write(fixed)
print(f"{src} -> {dst}: {n} semicolons added, no conditionals touched")
