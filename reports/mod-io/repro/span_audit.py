"""Mechanical audit of ENTER_BUFFERED / LEAVE_BUFFERED spans in bufferedio.c.

For each ENTER_BUFFERED site, find the enclosing function (by brace depth from a
column-0 '{'), then enumerate EVERY control-flow exit token in the remainder of
the function and report whether a LEAVE_BUFFERED textually dominates it on the
same or a preceding line within the span.

This is a *reporting* tool, not a prover: it prints the raw exit inventory so a
human can adjudicate each one.  It exists to catch exits the eye skipped --
notably macros that expand to `return`.
"""

import re
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/danzin/projects/cpython/Modules/_io/bufferedio.c"

lines = open(SRC, encoding="utf-8").read().splitlines()

# Macros in this file that expand to a bare `return` -- an invisible exit.
RETURNING_MACROS = [
    "CHECK_INITIALIZED", "CHECK_INITIALIZED_INT", "CHECK_CLOSED",
    "Py_RETURN_NONE", "Py_RETURN_TRUE", "Py_RETURN_FALSE",
]

EXIT_RE = re.compile(
    r"\b(return|goto\s+\w+|break|continue|"
    + "|".join(RETURNING_MACROS) + r")\b"
)


def func_start(idx):
    """Walk back to the opening brace of the enclosing top-level function."""
    for i in range(idx, -1, -1):
        if lines[i].startswith("{"):
            return i
    return 0


def func_end(start):
    for i in range(start, len(lines)):
        if lines[i].startswith("}"):
            return i
    return len(lines) - 1


enters = [i for i, ln in enumerate(lines)
          if "ENTER_BUFFERED(" in ln and not ln.lstrip().startswith("#define")]
leaves = {i for i, ln in enumerate(lines)
          if "LEAVE_BUFFERED(" in ln and not ln.lstrip().startswith("#define")}

print(f"{len(enters)} ENTER_BUFFERED sites, {len(leaves)} LEAVE_BUFFERED sites\n")

for e in enters:
    fs, fe = func_start(e), func_end(func_start(e))
    # function name is a line or two above the opening brace
    name = "?"
    for j in range(fs - 1, max(fs - 6, 0), -1):
        m = re.match(r"^(\w+)\s*\(", lines[j])
        if m:
            name = m.group(1)
            break
    span_leaves = sorted(x + 1 for x in leaves if e < x <= fe)
    print(f"=== ENTER at :{e+1} in {name}()  [fn :{fs+1}-{fe+1}]")
    print(f"    LEAVE sites later in fn: {span_leaves}")
    labels = [(i + 1, lines[i].strip())
              for i in range(e, fe) if re.match(r"^\w+:\s*$", lines[i])]
    print(f"    labels in fn after enter: {labels}")
    for i in range(e + 1, fe):
        s = lines[i]
        if "LEAVE_BUFFERED" in s:
            continue
        m = EXIT_RE.search(s)
        if m:
            prev = lines[i - 1].strip()
            tag = "  <-- LEAVE on prev line" if "LEAVE_BUFFERED" in prev else ""
            print(f"      :{i+1:5d} {m.group(1):<22} | {s.strip()[:70]}{tag}")
    print()
