"""Find tp_iternext functions in Objects/ that drop an owning self-member.

Models three drop spellings:
  A. Py_CLEAR(x->f)                       <- scanner models this
  B. x->f = NULL; Py_DECREF(local)        <- scanner models this
  C. Py_SETREF(x->f, NULL) / Py_XSETREF   <- scanner does NOT model this
"""

import re
import pathlib

ROOT = pathlib.Path("/home/danzin/projects/cpython/Objects")

FN = re.compile(r"^(?:static\s+)?(?:PyObject\s*\*|int)\s*\n?([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.M)
DROP_CLEAR = re.compile(r"Py_CLEAR\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*->")
DROP_SETREF = re.compile(r"Py_X?SETREF\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*->\s*[A-Za-z0-9_]+\s*,\s*NULL\s*\)")
DROP_NULL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*->\s*[A-Za-z0-9_]+\s*=\s*NULL\s*;")


def split_funcs(text: str) -> list[tuple[str, int, str]]:
    """Crude brace-matched function split."""
    out = []
    for m in re.finditer(
        r"^(?:static\s+)?[A-Za-z_][A-Za-z0-9_ *]*\n([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*?\)\s*\n\{",
        text,
        re.M,
    ):
        name = m.group(1)
        start = m.end() - 1
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((name, text[:m.start()].count("\n") + 1, text[start : i + 1]))
    return out


def main() -> None:
    for p in sorted(ROOT.glob("*.c")):
        text = p.read_text()
        # names registered as tp_iternext
        slots = set(re.findall(r"tp_iternext\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", text))
        slots |= set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*),\s*/\*\s*tp_iternext", text, re.M))
        if not slots:
            continue
        for name, line, body in split_funcs(text):
            if name not in slots:
                continue
            has_cs = "Py_BEGIN_CRITICAL_SECTION" in body
            kinds = []
            if DROP_CLEAR.search(body):
                kinds.append("A:Py_CLEAR")
            if DROP_SETREF.search(body):
                kinds.append("C:Py_SETREF-NULL")
            if DROP_NULL.search(body) and "Py_DECREF" in body:
                kinds.append("B:=NULL+DECREF")
            if not kinds:
                continue
            flag = "GUARDED" if has_cs else "UNGUARDED"
            print(f"{flag:9} {p.name}:{line} {name}  drops=[{','.join(kinds)}]")


if __name__ == "__main__":
    main()
