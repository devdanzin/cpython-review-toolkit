"""Prototype of the proposed `iterator_sentinel_field_asymmetry` rule.

Group B's formulation (reports/obj-sequences/agents/gil-discipline-checker.md 6.2):

 1. For every type registering a tp_iternext, find the exhaustion sentinel the
    iternext writes -- an owning-object field set to NULL (plain store,
    Py_CLEAR, Py_SETREF/Py_XSETREF ..., NULL).
 2. If that store is NOT inside `#ifndef Py_GIL_DISABLED`, mark the field
    volatile-by-concurrency.
 3. Flag every function that TESTS the marked field and dereferences it in a
    SEPARATE expression.
 4. Suppress when the owning field is never NULLed on FT.

Implemented so the measurement is about the RULE, not the chassis: bare
Argument-Clinic METHODDEF macro lines are repaired in memory first, so
tree-sitter does not truncate the file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)

import scan_ft_races as sfr  # noqa: E402
import tree_sitter_utils as tsu  # noqa: E402

BARE_MACRO_LINE = re.compile(
    r"^([ \t]*)([A-Z][A-Z0-9_]*(?:_METHODDEF|_GETSETDEF))[ \t]*$", re.MULTILINE
)


def repair(text: str) -> str:
    return BARE_MACRO_LINE.sub(lambda m: m.group(1) + "{0},", text)


# --- sentinel discovery -----------------------------------------------------

DROP_PATTERNS = [
    ("setnull", re.compile(r"\b(\w+)\s*->\s*(\w+)\s*=\s*NULL\s*;")),
    ("clear", re.compile(r"Py_CLEAR\s*\(\s*(\w+)\s*->\s*(\w+)\s*\)")),
    ("setref", re.compile(r"Py_X?SETREF\s*\(\s*(\w+)\s*->\s*(\w+)\s*,\s*NULL\s*\)")),
]

# test-then-deref: `if (X->F ...)` anywhere, plus a SEPARATE `X->F` occurrence
COND_RE = re.compile(r"\b(?:if|while)\s*\([^;{]*?\b(\w+)\s*->\s*(\w+)\b")

CAST_RE = re.compile(r"\b(\w+)\s*\*\s*(\w+)\s*=\s*\(\s*(\w+)\s*\*\s*\)")


def self_type_of(func: dict) -> str | None:
    """Struct type the function treats as the iterator, from cast or param."""
    body = sfr.strip_comments(func["body"])
    for m in CAST_RE.finditer(body):
        if m.group(1) == m.group(3) and m.group(1) not in ("PyObject", "void"):
            return m.group(1)
    params = func.get("parameters", "") or ""
    m = re.search(r"\b([A-Za-z_]\w*)\s*\*\s*\w+", params)
    if m and m.group(1) not in ("PyObject", "void", "char"):
        return m.group(1)
    return None


def field_var_types(func: dict) -> dict[str, str]:
    """local/param name -> declared struct type, for `T *x` declarations."""
    out: dict[str, str] = {}
    text = (func.get("parameters", "") or "") + ";" + sfr.strip_comments(func["body"])
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\*\s*([A-Za-z_]\w*)\s*(?:[=,;)])", text):
        out.setdefault(m.group(2), m.group(1))
    return out


def analyze_file(path: Path, rel: str) -> tuple[list[dict], dict]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    src = repair(raw)
    b = src.encode("utf-8")
    try:
        tree = tsu.parse_bytes(b)
    except Exception:
        return [], {}
    funcs = tsu.extract_functions(tree, b)
    if not funcs:
        return [], {}
    slot_names = sfr._collect_iternext_names(src)
    gil_only, _ = sfr._gil_disabled_regions(src)

    # step 1+2: sentinel fields marked volatile-by-concurrency
    marked: dict[tuple[str, str], dict] = {}  # (selftype, field) -> info
    for f in funcs:
        if not sfr._is_iternext(f["name"], slot_names):
            continue
        body = sfr.strip_comments(f["body"])
        base = sfr._body_start_line(f)
        stype = self_type_of(f)
        vtypes = field_var_types(f)
        for kind, rx in DROP_PATTERNS:
            for m in rx.finditer(body):
                var, field = m.group(1), m.group(2)
                if kind == "setnull" and "Py_DECREF" not in body:
                    continue
                line = sfr._line_of(body, m.start(), base)
                if sfr._in_ranges(line, gil_only):
                    continue  # step 2/4 suppression: elided on FT
                owner = vtypes.get(var) or stype
                if not owner:
                    continue
                key = (owner, field)
                marked.setdefault(
                    key,
                    {
                        "self_type": owner,
                        "field": field,
                        "iternext": f["name"],
                        "drop_line": line,
                        "drop_kind": kind,
                    },
                )

    if not marked:
        return [], {"iternext": sum(1 for f in funcs if sfr._is_iternext(f["name"], slot_names))}

    # step 3: accessors that test the field then deref it separately
    findings: list[dict] = []
    for f in funcs:
        body = sfr.strip_comments(f["body"])
        base = sfr._body_start_line(f)
        vtypes = field_var_types(f)
        ftype = self_type_of(f)
        for m in COND_RE.finditer(body):
            var, field = m.group(1), m.group(2)
            owner = vtypes.get(var) or ftype
            key = (owner, field)
            if key not in marked:
                continue
            cond_end = m.end()
            # a SEPARATE occurrence of var->field after the condition
            sep = re.search(rf"\b{re.escape(var)}\s*->\s*{re.escape(field)}\b", body[cond_end:])
            if not sep:
                continue
            use_line = sfr._line_of(body, cond_end + sep.start(), base)
            cond_line = sfr._line_of(body, m.start(), base)
            info = marked[key]
            findings.append(
                {
                    "file": rel,
                    "function": f["name"],
                    "member": f"{owner}.{field}",
                    "test_line": cond_line,
                    "use_line": use_line,
                    "iternext": info["iternext"],
                    "drop_line": info["drop_line"],
                    "drop_kind": info["drop_kind"],
                }
            )
            break  # one finding per function
    return findings, {"marked": len(marked)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+")
    args = ap.parse_args()
    cpy = Path("/home/danzin/projects/cpython")
    all_findings = []
    nfiles = 0
    for root in args.roots:
        for p in sorted((cpy / root).rglob("*.c")):
            nfiles += 1
            fs, _ = analyze_file(p, str(p.relative_to(cpy)))
            all_findings.extend(fs)
    print(json.dumps({"files": nfiles, "findings": all_findings, "n": len(all_findings)}, indent=1))


if __name__ == "__main__":
    main()
