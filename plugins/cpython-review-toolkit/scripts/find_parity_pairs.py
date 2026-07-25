#!/usr/bin/env python3
"""Discover CPython's C-accelerator <-> pure-Python-twin module pairs.

CPython ships several stdlib modules *twice*: a fast C accelerator and a pure
Python implementation of the same public API. The pure-Python twin is a free
differential oracle -- feed the same adversarial input to both backends and any
divergence (a C-side segfault while the twin raises a clean exception, or two
different exception types) is a localized, confirmed lead.

This is a **discovery** script, not a bug scanner. It walks a CPython checkout
and emits the inventory of dual implementations so the ``parity-checker`` agent
knows what to differentially test, and -- with ``--emit-harness`` -- writes the
ready-to-run differential driver for a pair so the agent does not rebuild one.

Three detection methods are used:

1. **explicit ``_py*`` twin** -- a ``Lib/_py<name>.py`` file (e.g.
   ``_pydecimal.py``, ``_pyio.py``, ``_pydatetime.py``).
2. **package twin** -- ``Lib/<pkg>/_<pkg>.py`` (``Lib/zoneinfo/_zoneinfo.py``),
   the *package* spelling of the same idea. Missing this rule cost a confirmed
   crash: ``zoneinfo`` was reported ``python_twin_module: null, confidence:
   low`` and only a hand-identified twin produced CPY-0033.
3. **accelerator-import** -- a public module ``Lib/<name>.py`` (or a package
   ``Lib/<name>/``) that imports its same-named C accelerator ``_<name>``
   (``from _heapq import *``, ``from _json import make_scanner``, ...). The
   pure-Python fallback lives inline in the public module.

A pair discovered by more than one method is merged and reported once.

Two correctness rules keep the inventory honest:

* **The C module must exist.** ``Lib/_pylong.py`` used to yield the pair
  ``long`` at ``high`` confidence with ``c_module: "_long"`` -- but ``import
  _long`` raises ``ModuleNotFoundError``, and ``_pylong`` is an algorithm helper
  for ``longobject.c``, not an API twin. A fabricated module name at the
  *highest* tier is the worst kind of precision loss, because that is the tier
  an agent starts with. Every ``c_module`` is now verified against its C
  sources (a ``PyInit_<mod>`` entry point or the module-name string in a
  ``PyModuleDef``); unverified pairs are dropped into ``rejected_pairs``.
* **Dual bindings promote off ``low``.** A dispatcher that binds *both* a
  ``py_*``/``_*`` pure name and the same symbol imported from the accelerator
  (``py_make_scanner``/``c_make_scanner``, ``_Pickler``/``Pickler``) is a real
  side-by-side dual path even though it is selected by a name rebinding rather
  than a separate ``_py*`` file. Rating those ``low`` deprioritized exactly the
  pairs a ``Modules/`` review needs.

Usage:
    python find_parity_pairs.py [path-to-cpython-checkout] [--max-files N]
    python find_parity_pairs.py [path] --emit-harness datetime
    python find_parity_pairs.py [path] --emit-harness all --out /tmp/harnesses
"""

import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import (
    build_report,
    parse_common_args,
    relpath,
    resolve_roots,
)

# Confidence ordering used to sort the inventory (highest first).
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

# A comment line never carries a real import; skip it during import detection.
_COMMENT_LINE = re.compile(r"^\s*#")

# Affixes that mark the two halves of a dual binding in a dispatcher module.
_DUAL_AFFIXES = ("py_", "c_", "_")

# Probes are attribute paths off the module; cap so the harness stays readable.
_MAX_PROBES = 12

# How many methods of a pure-Python class to offer as probes.
_METHODS_PER_CLASS = 3

_BACKEND_TRAP = (
    "Do NOT use __module__ to tell the backends apart: "
    "datetime.datetime.__module__ is 'datetime' for BOTH the C accelerator and "
    "_pydatetime, so an agent that trusts it runs a 'differential' with the C "
    "backend on both sides and reports a false clean. Compare the *type* of a "
    "probe attribute instead: a C accelerator exposes method_descriptor / "
    "builtin_function_or_method, a pure-Python twin exposes function / method."
)

_BACKEND_METHOD = (
    "type(<module>.<probe>).__name__: {method_descriptor, "
    "builtin_function_or_method, wrapper_descriptor, getset_descriptor, "
    "member_descriptor, classmethod_descriptor} => C accelerator; "
    "{function, method, property, cached_property} => pure Python. The emitted "
    "harness runs every probe on both sides first and uses the first one whose "
    "kind actually differs; if none differs it refuses to run the differential."
)


def _c_module_and_base(twin_stem: str) -> tuple[str, str]:
    """Map a ``_py*`` twin stem to its C accelerator module name and base.

    ``_pydecimal`` -> (``_decimal``, ``decimal``);
    ``_py_abc``    -> (``_abc``, ``abc``);
    ``_py_warnings`` -> (``_warnings``, ``warnings``).
    """
    rest = twin_stem[len("_py") :]
    if rest.startswith("_"):
        # Twin name already carries the accelerator's leading underscore.
        return rest, rest[1:]
    return "_" + rest, rest


def _locate_c_sources(project_root: Path, c_module: str, base: str) -> list[str]:
    """Return the C accelerator source path(s) for module ``c_module``.

    Searches the conventional CPython layouts, in order:

    * ``Modules/<c_module>/*.c``      (package accelerators: ``_decimal``, ``_io``)
    * ``Modules/<c_module>module.c``  (``_datetimemodule.c``, ``_heapqmodule.c``)
    * ``Modules/<c_module>.c``        (``_json.c``, ``_csv.c``, ``_pickle.c``)
    * ``Python/<c_module>.c``         (``_warnings.c``)
    * ``Objects/<base>object.c``      (last-resort; e.g. ``_pylong`` -> longobject.c)

    The last-resort fallback is deliberately kept, but a pair that reaches it
    almost never has a real accelerator module -- :func:`_verify_c_module` is
    what stops it from being reported.
    """
    sources: list[Path] = []
    modules = project_root / "Modules"

    pkg_dir = modules / c_module
    if pkg_dir.is_dir():
        # Non-recursive: skip vendored subtrees like Modules/_decimal/libmpdec.
        sources.extend(sorted(pkg_dir.glob("*.c")))

    for candidate in (
        modules / f"{c_module}module.c",
        modules / f"{c_module}.c",
        project_root / "Python" / f"{c_module}.c",
    ):
        if candidate.is_file():
            sources.append(candidate)

    if not sources:
        fallback = project_root / "Objects" / f"{base}object.c"
        if fallback.is_file():
            sources.append(fallback)

    return [relpath(s, project_root) for s in sources]


def _verify_c_module(
    project_root: Path, c_module: str, c_sources: list[str]
) -> tuple[bool, str]:
    """Check that ``c_module`` is a module an interpreter can actually import.

    Returns ``(verified, evidence)``. A C accelerator either has a
    ``PyInit_<mod>`` entry point (an extension module) or names itself in a
    ``PyModuleDef`` (a builtin such as ``_warnings``, whose ``Python/_warnings.c``
    carries ``#define MODULE_NAME "_warnings"``). Neither present means the pair
    was synthesised from a filename and no ``import <c_module>`` will succeed.
    """
    init = re.compile(rf"\bPyInit_{re.escape(c_module)}\b")
    literal = f'"{c_module}"'
    for rel in c_sources:
        path = project_root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if init.search(text):
            return True, f"PyInit_{c_module} in {rel}"
        if literal in text:
            return True, f'module-name literal "{c_module}" in {rel}'
    return False, f"no PyInit_{c_module} and no module-name literal in {c_sources}"


def _module_python_files(lib: Path, name: str) -> list[Path]:
    """Return the Python file(s) making up public module ``name``.

    A single-file module is ``Lib/<name>.py``; a package is every ``*.py`` one
    level deep in ``Lib/<name>/`` (enough to reach the accelerator imports that
    live in package submodules, e.g. ``Lib/json/scanner.py``).
    """
    single = lib / f"{name}.py"
    if single.is_file():
        return [single]
    pkg = lib / name
    if (pkg / "__init__.py").is_file():
        return sorted(pkg.glob("*.py"))
    return []


def _detect_accelerator_import(
    files: list[Path], base: str
) -> tuple[str, list[Path], bool]:
    """Detect whether ``files`` import the ``_<base>`` C accelerator.

    Returns ``(import_style, import_sites, guarded)``. ``import_style`` is
    ``"star"`` (``from _base import *`` -- a full replacement), ``"named"``
    (a selective ``from _base import name`` / ``import _base``), or ``"none"``.

    ``guarded`` is True when at least one accelerator import is **indented**,
    i.e. sits inside a ``try:``/``except ImportError:`` (or an ``if``) and
    therefore has a pure-Python fallback. An accelerator import at column 0 is
    unconditional: blocking the C module breaks ``import <mod>`` outright, so
    there is no second implementation to differentially test. ``Lib/csv.py``,
    ``Lib/struct.py`` and ``Lib/weakref.py`` are all of that shape -- the pair
    is real as *inventory* but is not an oracle.
    """
    star_re = re.compile(rf"from\s+_{re.escape(base)}\s+import\s+\*")
    from_re = re.compile(rf"from\s+_{re.escape(base)}\s+import\b")
    import_re = re.compile(rf"(?:^|;)\s*import\s+_{re.escape(base)}\b")

    style = "none"
    sites: list[Path] = []
    guarded = False
    for filepath in files:
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hit = False
        for line in text.splitlines():
            if _COMMENT_LINE.match(line):
                continue
            if star_re.search(line):
                style = "star"
                hit = True
            elif from_re.search(line) or import_re.search(line):
                if style == "none":
                    style = "named"
                hit = True
            else:
                continue
            if line[:1].isspace():
                guarded = True
        if hit:
            sites.append(filepath)
    return style, sites, guarded


# ---------------------------------------------------------------------------
# Python-side structure (ast, so comments and strings can never fool it)
# ---------------------------------------------------------------------------


def _public_methods(node: ast.ClassDef) -> list[str]:
    return [
        b.name
        for b in node.body
        if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not b.name.startswith("_")
    ]


def _ast_structure(tree: ast.Module) -> dict:
    """Top-level defs / classes / assignments / accelerator imports of a module."""
    classes: dict[str, list[str]] = {}
    functions: list[str] = []
    assigned: set[str] = set()
    imported: dict[str, set[str]] = defaultdict(set)

    def _targets(node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            assigned.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for elt in node.elts:
                _targets(elt)

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes[node.name] = _public_methods(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _targets(t)
        elif isinstance(node, ast.AnnAssign):
            _targets(node.target)
        elif isinstance(node, (ast.Try, ast.If)):
            # Accelerator imports and their fallbacks live in try/except and
            # `if` blocks; walk one level of nesting for both.
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.module:
                    for alias in sub.names:
                        imported[sub.module].add(alias.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        _targets(t)
                elif isinstance(sub, ast.ClassDef):
                    classes.setdefault(sub.name, _public_methods(sub))
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append(sub.name)
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported[node.module].add(alias.name)

    return {
        "classes": classes,
        "functions": functions,
        "assigned": assigned,
        "imported": dict(imported),
        "parsed_with": "ast",
    }


# Regex fallback, used when the *target* tree is newer than the interpreter
# running this script. That is the normal case, not an edge case: the toolkit
# venv is 3.12 while CPython main is 3.16, and `ast.parse` rejects
# `Lib/collections/__init__.py` and `Lib/typing.py` outright. Silently treating
# those as structureless is how `collections` lost its OrderedDict dual binding.
_RE_CLASS = re.compile(r"^class\s+(\w+)\s*[(:]")
_RE_FUNC = re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\(")
_RE_METHOD = re.compile(r"^\s+(?:async\s+)?def\s+(\w+)\s*\(")
_RE_ASSIGN = re.compile(r"^(\w+(?:\s*,\s*\w+)*)\s*(?::[^=]+)?=[^=]")
_RE_TRIPLE = re.compile(r'"""|\'\'\'')


def _regex_structure(text: str, c_module: str) -> dict:
    """Best-effort structure extraction that does not need a matching grammar."""
    classes: dict[str, list[str]] = {}
    functions: list[str] = []
    assigned: set[str] = set()
    imported: set[str] = set()

    lines = text.split("\n")
    in_triple: str | None = None
    current_class: str | None = None
    import_tail = 0

    from_re = re.compile(rf"^\s*from\s+{re.escape(c_module)}\s+import\s+(.*)$")

    def _record_names(blob: str) -> None:
        for chunk in blob.replace("(", " ").replace(")", " ").split(","):
            chunk = chunk.strip()
            if not chunk or chunk == "*":
                continue
            name = chunk.split()[0]
            if name.isidentifier():
                imported.add(name)

    for line in lines:
        # Track triple-quoted blocks so a docstring cannot fake a definition.
        if in_triple:
            if in_triple in line:
                in_triple = None
            continue
        marks = _RE_TRIPLE.findall(line)
        if marks and line.count(marks[0]) % 2 == 1:
            in_triple = marks[0]
            continue

        if import_tail:
            _record_names(line)
            if ")" in line:
                import_tail = 0
            continue
        m = from_re.match(line)
        if m:
            tail = m.group(1)
            _record_names(tail)
            if "(" in tail and ")" not in tail:
                import_tail = 1
            continue

        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _RE_CLASS.match(line)
        if m:
            current_class = m.group(1)
            classes.setdefault(current_class, [])
            continue
        m = _RE_FUNC.match(line)
        if m:
            current_class = None
            functions.append(m.group(1))
            continue
        if line[:1] not in (" ", "\t", ")", "]", "}"):
            m = _RE_ASSIGN.match(line)
            if m:
                for name in m.group(1).split(","):
                    name = name.strip()
                    if name.isidentifier():
                        assigned.add(name)
            current_class = None
            continue
        if current_class is not None:
            m = _RE_METHOD.match(line)
            if m and not m.group(1).startswith("_"):
                classes[current_class].append(m.group(1))

    return {
        "classes": classes,
        "functions": functions,
        "assigned": assigned,
        "imported": {c_module: imported} if imported else {},
        "parsed_with": "regex",
    }


def module_structure(path: Path, c_module: str) -> dict:
    """Structure of a Python module, via ``ast`` when the grammar allows it."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {
            "classes": {},
            "functions": [],
            "assigned": set(),
            "imported": {},
            "parsed_with": "unreadable",
        }
    try:
        return _ast_structure(ast.parse(text))
    except (SyntaxError, ValueError):
        return _regex_structure(text, c_module)


def _strip_affix(name: str) -> str:
    for affix in _DUAL_AFFIXES:
        if name.startswith(affix) and len(name) > len(affix):
            return name[len(affix) :]
    return name


def _dual_bindings(
    files: list[Path], c_module: str
) -> tuple[list[dict], set[str], dict[str, int]]:
    """Symbols a dispatcher binds *both* from the accelerator and in Python.

    ``py_make_scanner`` + ``from _json import make_scanner as c_make_scanner``;
    ``class _Pickler`` + ``from _pickle import Pickler``. Each such symbol is a
    genuine side-by-side dual path, selected by a name rebinding at the bottom
    of the module rather than by a separate ``_py*`` file.

    Also returns every name the dispatcher imports from the accelerator (those
    are, by definition, the symbols the C side overrides -- the best probes),
    and a parse-health tally so a zero here is never mistaken for "clean".
    """
    bindings: list[dict] = []
    seen: set[str] = set()
    accelerated: set[str] = set()
    health: dict[str, int] = defaultdict(int)
    for path in files:
        struct = module_structure(path, c_module)
        health[struct["parsed_with"]] += 1
        from_c = struct["imported"].get(c_module, set())
        accelerated |= set(from_c)
        if not from_c:
            continue
        pure_names = set(struct["classes"]) | set(struct["functions"])
        for imported_name in sorted(from_c):
            base = _strip_affix(imported_name)
            for pure in sorted(pure_names):
                if _strip_affix(pure) != base:
                    continue
                public = (
                    base
                    if base in struct["assigned"] or base in pure_names
                    else imported_name
                )
                if public in seen:
                    continue
                seen.add(public)
                bindings.append(
                    {
                        "public_name": public,
                        "pure_name": pure,
                        "imported_name": imported_name,
                        "file": str(path),
                        "is_class": pure in struct["classes"],
                        "methods": struct["classes"].get(pure, []),
                    }
                )
                break
    return bindings, accelerated, dict(health)


def _attr_prefix(lib: Path, path: Path, module: str) -> str:
    """Attribute prefix needed to reach ``path``'s symbols from ``import module``.

    ``Lib/json/scanner.py`` inside package ``json`` -> ``scanner.``;
    ``Lib/json/__init__.py`` and ``Lib/heapq.py`` -> ``''``.
    """
    try:
        rel = path.relative_to(lib / module)
    except ValueError:
        return ""
    if rel.name == "__init__.py":
        return ""
    return rel.stem + "."


def _derive_probes(
    lib: Path,
    module: str,
    c_module: str,
    twin_path: Path | None,
    py_files: list[Path],
    bindings: list[dict],
    accelerated: set[str],
) -> list[str]:
    """Attribute paths whose *type* tells the two backends apart.

    Ordered strongest first: dual-bound symbols, then symbols the dispatcher
    imports from the accelerator (the C side provably overrides those), then
    methods of the twin's biggest classes, then remaining module-level
    functions. A bare class is useless as a probe -- ``type(cls)`` is ``type``
    on both sides -- so classes are always probed through a method.
    """
    probes: list[str] = []

    def _add(path: str) -> None:
        # A dunder is never a useful probe: it is either present on both sides
        # or absent from both, and its type is rarely the backend's own.
        if path.rpartition(".")[2].startswith("__"):
            return
        if path not in probes:
            probes.append(path)

    for binding in bindings:
        prefix = _attr_prefix(lib, Path(binding["file"]), module)
        if binding["is_class"]:
            for method in binding["methods"][:_METHODS_PER_CLASS]:
                _add(f"{prefix}{binding['public_name']}.{method}")
        else:
            _add(f"{prefix}{binding['public_name']}")

    sources = [twin_path] if twin_path is not None else py_files
    for path in sources:
        if path is None:
            continue
        struct = module_structure(path, c_module)
        prefix = "" if twin_path is not None else _attr_prefix(lib, path, module)

        def _class_probes(cls: str, methods: list[str], _p: str = "") -> None:
            for method in methods[:_METHODS_PER_CLASS]:
                _add(f"{_p}{cls}.{method}")

        # 1. Names the dispatcher imports from the accelerator.
        for func in struct["functions"]:
            if func in accelerated:
                _add(f"{prefix}{func}")
        for cls, methods in struct["classes"].items():
            if cls in accelerated:
                _class_probes(cls, methods, prefix)
        # 2. The twin's richest classes -- an exception class with one method is
        #    a far weaker probe than the module's main type.
        for cls, methods in sorted(
            struct["classes"].items(), key=lambda kv: -len(kv[1])
        ):
            if cls.startswith("_") or not methods:
                continue
            _class_probes(cls, methods, prefix)
        # 3. Anything else public.
        for func in struct["functions"]:
            if not func.startswith("_"):
                _add(f"{prefix}{func}")

    return probes[:_MAX_PROBES]


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def _confidence(pair: dict) -> str:
    """Grade the confidence of a discovered pair."""
    if pair["detection"] in ("explicit_py_twin", "package_twin", "both"):
        # A dedicated twin file is definitive evidence of a dual impl; only
        # downgrade if we could not locate the C side to test against.
        return "high" if pair["c_sources"] else "medium"
    if pair["import_style"] == "star":
        # A star import replaces the whole module.
        return "medium"
    # A selective import accelerates only some primitives -- but if the module
    # binds both halves of the same symbol, the dual path is explicit and real.
    return "medium" if pair.get("dual_bindings") else "low"


# ---------------------------------------------------------------------------
# Harness emission
# ---------------------------------------------------------------------------

_HARNESS_TEMPLATE = r'''#!/usr/bin/env python3
"""Differential harness for the CPython parity pair `@@MODULE@@`.

Generated by find_parity_pairs.py --emit-harness @@MODULE@@.

Runs one payload through the C accelerator and through the pure-Python twin in
**separate subprocesses**, because a C-side crash kills the interpreter and
cannot be caught with try/except. Compares exit code, signal, exception type and
value, and -- before any of that -- *proves* the two sides really are different
backends.

    ./@@FILENAME@@ -e "<expression>"
    ./@@FILENAME@@ -f payload.py --interpreter ./python --interpreter ./python-dbg
    ./@@FILENAME@@                       # backend probe only, no payload

A payload is evaluated as an expression when it is one, otherwise exec'd; in the
exec case the harness reports the variable `result` if the payload sets it. The
module under test is bound as `m` on both sides.

Backend assertion (this is the part agents keep getting wrong):
@@TRAP@@
"""

import argparse
import json
import subprocess
import sys

PAIR = json.loads("""
@@PAIR_JSON@@
""")

C_PRELUDE = @@C_PRELUDE@@
PY_PRELUDE = @@PY_PRELUDE@@
PROBES = @@PROBES_JSON@@

_CHILD = r"""
import sys

_C_KINDS = {"method_descriptor", "builtin_function_or_method",
            "wrapper_descriptor", "getset_descriptor", "member_descriptor",
            "classmethod_descriptor", "method-wrapper", "builtin_method"}
_PY_KINDS = {"function", "method", "property", "cached_property",
             "staticmethod", "classmethod"}


def _resolve(root, path):
    obj = root
    for part in path.split("."):
        try:
            obj = getattr(obj, part)
        except Exception:
            return None
    return obj


def _kind(obj):
    name = type(obj).__name__
    if name in _C_KINDS:
        return "c"
    if name in _PY_KINDS:
        return "py"
    return None


_ns = {}
exec(%%PRELUDE%%, _ns)
m = _ns.get("m")
if m is None:
    sys.stderr.write("PARITY-PRELUDE-ERROR: prelude did not bind `m`\n")
    raise SystemExit(3)

for _p in %%PROBES%%:
    _o = _resolve(m, _p)
    if _o is not None:
        sys.stdout.write("PARITY-PROBE:%s=%s\n" % (_p, _kind(_o)))

_expect = %%EXPECT%%
_probe = %%PROBE%%
if _expect is not None:
    _o = _resolve(m, _probe)
    _k = _kind(_o) if _o is not None else None
    if _k != _expect:
        sys.stderr.write(
            "PARITY-BACKEND-ASSERTION-FAILED: probe %r is %r, expected %r\n"
            % (_probe, _k, _expect))
        raise SystemExit(4)
    sys.stdout.write("PARITY-BACKEND-OK:%s=%s\n" % (_probe, _k))

_body = %%BODY%%
if _body is None:
    raise SystemExit(0)

sys.stdout.flush()
try:
    try:
        _code = compile(_body, "<parity-body>", "eval")
    except SyntaxError:
        exec(compile(_body, "<parity-body>", "exec"), _ns)
        _v = _ns.get("result")
    else:
        _v = eval(_code, _ns)
except BaseException as _e:
    sys.stdout.write("PARITY-EXC:%s\n" % type(_e).__name__)
    sys.stdout.flush()
    raise
sys.stdout.write("PARITY-VALUE:%r\n" % (_v,))
"""

_SIGNAL_NAMES = {4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE",
                 11: "SIGSEGV", 9: "SIGKILL", 15: "SIGTERM"}

_FATAL_MARKERS = ("Fatal Python error", "Assertion", "AddressSanitizer",
                  "ERROR: ", "SUMMARY: ", "Segmentation fault", "Aborted",
                  "runtime error:")


def _child_source(prelude, body, expect, probe):
    src = _CHILD
    src = src.replace("%%PRELUDE%%", repr(prelude))
    src = src.replace("%%PROBES%%", repr(PROBES))
    src = src.replace("%%EXPECT%%", repr(expect))
    src = src.replace("%%PROBE%%", repr(probe))
    src = src.replace("%%BODY%%", repr(body))
    return src


def run_side(interpreter, prelude, body, expect=None, probe=None, timeout=30):
    """Run one backend in its own subprocess and decode the outcome."""
    src = _child_source(prelude, body, expect, probe)
    try:
        proc = subprocess.run([interpreter, "-c", src], capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"timeout": True, "returncode": None, "signal": 0,
                "crashed": False, "exc": None, "value": None,
                "probes": {}, "stdout": "", "stderr": ""}
    rc = proc.returncode
    if rc < 0:
        signal = -rc
    elif rc > 128:
        signal = rc - 128
    else:
        signal = 0
    probes = {}
    exc = None
    value = None
    for line in proc.stdout.splitlines():
        if line.startswith("PARITY-PROBE:"):
            key, _, kind = line[len("PARITY-PROBE:"):].partition("=")
            probes[key] = None if kind == "None" else kind
        elif line.startswith("PARITY-EXC:"):
            exc = line[len("PARITY-EXC:"):]
        elif line.startswith("PARITY-VALUE:"):
            value = line[len("PARITY-VALUE:"):]
    return {"timeout": False, "returncode": rc, "signal": signal,
            "crashed": signal != 0, "exc": exc, "value": value,
            "probes": probes, "stdout": proc.stdout, "stderr": proc.stderr}


def choose_probe(interpreter, timeout=30):
    """Return (probe, c_kind, py_kind) for the first probe that DIFFERS.

    A probe whose kind is the same on both sides proves nothing; if no probe
    differs, the pair cannot be driven as a differential from this module and
    the caller must stop rather than report a false clean.
    """
    c = run_side(interpreter, C_PRELUDE, None, timeout=timeout)
    p = run_side(interpreter, PY_PRELUDE, None, timeout=timeout)
    for probe in PROBES:
        ck, pk = c["probes"].get(probe), p["probes"].get(probe)
        if ck and pk and ck != pk:
            return probe, ck, pk, c, p
    return None, None, None, c, p


def classify(c, p):
    """Verdict for one payload, from the two decoded outcomes."""
    if c["crashed"] and not p["crashed"]:
        return "FIX", ("C accelerator died with %s while the pure-Python twin "
                       "did not -- memory-safe Python does not segfault, so "
                       "this is a C-side defect"
                       % _SIGNAL_NAMES.get(c["signal"], c["signal"]))
    if p["crashed"] and not c["crashed"]:
        return "CONSIDER", ("only the pure-Python twin died (%s) -- a twin bug, "
                            "not a C-accelerator defect"
                            % _SIGNAL_NAMES.get(p["signal"], p["signal"]))
    if c["crashed"] and p["crashed"]:
        return "SHARED", ("both backends died -- usually one shared root cause "
                          "(native recursion, algorithmic blowup). Check the "
                          "CPython tracker BEFORE reporting anything")
    if c["timeout"] != p["timeout"]:
        side = "C" if c["timeout"] else "pure-Python"
        return "CONSIDER", "only the %s backend hung" % side
    if c["timeout"] and p["timeout"]:
        return "SHARED", "both backends hung -- a shared algorithmic limit"
    if c["exc"] != p["exc"]:
        return "CONSIDER", ("different exception: C %s, twin %s"
                            % (("raised " + c["exc"]) if c["exc"]
                               else "did not raise",
                               ("raised " + p["exc"]) if p["exc"]
                               else "did not raise"))
    if c["value"] != p["value"]:
        return "CONSIDER", "same outcome, different value: %s vs %s" % (
            c["value"], p["value"])
    return "AGREE", "both backends agreed"


def differential(interpreter, body, timeout=30, repeat=1):
    probe, ck, pk, c_probe, p_probe = choose_probe(interpreter, timeout=timeout)
    if probe is None:
        return {"error": "no probe distinguishes the two backends",
                "probes_tried": PROBES,
                "c_probes": c_probe["probes"], "py_probes": p_probe["probes"],
                "c_stderr": c_probe["stderr"][-800:],
                "py_stderr": p_probe["stderr"][-800:]}
    trials = []
    for _ in range(max(1, repeat)):
        c = run_side(interpreter, C_PRELUDE, body, expect=ck, probe=probe,
                     timeout=timeout)
        p = run_side(interpreter, PY_PRELUDE, body, expect=pk, probe=probe,
                     timeout=timeout)
        verdict, why = classify(c, p)
        trials.append({"verdict": verdict, "why": why, "c": c, "py": p})
    return {"interpreter": interpreter, "backend_probe": probe,
            "c_kind": ck, "py_kind": pk, "body": body, "trials": trials}


def _print_result(res):
    if "error" in res:
        print("BACKEND ASSERTION IMPOSSIBLE: %s" % res["error"])
        print("  probes tried : %s" % ", ".join(res["probes_tried"]))
        print("  C kinds      : %s" % res["c_probes"])
        print("  pure-Py kinds: %s" % res["py_probes"])
        if res["c_stderr"]:
            print("  C stderr     : %s" % res["c_stderr"].strip()[-400:])
        if res["py_stderr"]:
            print("  py stderr    : %s" % res["py_stderr"].strip()[-400:])
        print("This pair cannot be driven as a differential through this module."
              " Do NOT report a clean run.")
        return 2
    print("interpreter    : %s" % res["interpreter"])
    print("backend probe  : %s  (C=%s, pure-Python=%s)"
          % (res["backend_probe"], res["c_kind"], res["py_kind"]))
    print("payload        : %s" % (res["body"],))
    for i, trial in enumerate(res["trials"], 1):
        c, p = trial["c"], trial["py"]
        print("  run %d: %-8s %s" % (i, trial["verdict"], trial["why"]))
        for label, side in (("C  ", c), ("py ", p)):
            print("    %s exit=%s signal=%s exc=%s value=%s%s"
                  % (label, side["returncode"], side["signal"], side["exc"],
                     side["value"], " TIMEOUT" if side["timeout"] else ""))
            # A fatal abort prints its diagnosis several lines ABOVE the last
            # traceback frame ("Fatal Python error: ...", the failed assertion,
            # the ASan report header), so surface those in preference to the
            # tail.
            lines = [ln for ln in side["stderr"].splitlines() if ln.strip()]
            picked = [ln for ln in lines
                      if any(mk in ln for mk in _FATAL_MARKERS)][:2]
            for tail in (picked or lines[-2:]):
                print("        stderr: %s" % tail[:160])
    return 0 if all(t["verdict"] == "AGREE" for t in res["trials"]) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-e", "--expr", action="append", default=[],
                    help="payload (expression or statements); repeatable")
    ap.add_argument("-f", "--file", action="append", default=[],
                    help="payload read from a file; repeatable")
    ap.add_argument("--interpreter", action="append", default=[],
                    help="interpreter to test with; repeatable (build matrix)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--repeat", type=int, default=1,
                    help="re-run each payload N times to confirm determinism")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args(argv)

    interpreters = args.interpreter or [sys.executable]
    bodies = list(args.expr)
    for path in args.file:
        with open(path, encoding="utf-8") as fh:
            bodies.append(fh.read())

    results = []
    status = 0
    for interp in interpreters:
        if not bodies:
            probe, ck, pk, c, p = choose_probe(interp, timeout=args.timeout)
            res = {"interpreter": interp, "backend_probe": probe,
                   "c_kind": ck, "py_kind": pk,
                   "c_probes": c["probes"], "py_probes": p["probes"]}
            results.append(res)
            if not args.json:
                print("interpreter    : %s" % interp)
                if probe is None:
                    print("  NO PROBE DISTINGUISHES THE BACKENDS -- "
                          "this pair is not differentiable through this module.")
                    if PAIR.get("notes"):
                        print("  %s" % PAIR["notes"])
                    print("  C kinds      : %s" % c["probes"])
                    print("  pure-Py kinds: %s" % p["probes"])
                    status = 2
                else:
                    print("  backend probe: %s (C=%s, pure-Python=%s) -- ready"
                          % (probe, ck, pk))
            continue
        for body in bodies:
            res = differential(interp, body, timeout=args.timeout,
                               repeat=args.repeat)
            results.append(res)
            if not args.json:
                status = max(status, _print_result(res))
    if args.json:
        json.dump(results, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
'''


def emit_harness(pair: dict) -> str:
    """Render the ready-to-run differential driver for ``pair``."""
    module = pair["module"]
    filename = f"parity_harness_{module}.py"
    slim = {
        k: pair[k]
        for k in (
            "module",
            "python_impl",
            "python_twin_module",
            "c_module",
            "c_sources",
            "detection",
            "import_style",
            "confidence",
            "differentiable",
            "notes",
        )
        if k in pair
    }
    text = _HARNESS_TEMPLATE
    text = text.replace("@@MODULE@@", module)
    text = text.replace("@@FILENAME@@", filename)
    text = text.replace("@@TRAP@@", _BACKEND_TRAP)
    text = text.replace("@@PAIR_JSON@@", json.dumps(slim, indent=4))
    text = text.replace("@@C_PRELUDE@@", repr(pair["force_c_hint"]))
    text = text.replace("@@PY_PRELUDE@@", repr(pair["force_python_hint"]))
    text = text.replace(
        "@@PROBES_JSON@@", json.dumps(pair["backend_assertion"]["probes"])
    )
    return text


def harness_filename(module: str) -> str:
    return f"parity_harness_{module}.py"


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Discover C-accelerator / pure-Python-twin module pairs in a checkout."""
    project_root, scan_root = resolve_roots(target)
    lib = project_root / "Lib"

    pairs: dict[str, dict] = {}
    twin_paths: dict[str, Path] = {}
    files_examined = 0
    skipped: list[dict] = []
    parse_health: dict[str, int] = defaultdict(int)

    if not lib.is_dir():
        skipped.append(
            {"file": str(lib), "reason": "no Lib/ directory under project root"}
        )

    def _record_twin(
        base: str, twin: Path, c_module: str, twin_module: str, detection: str
    ) -> None:
        c_sources = _locate_c_sources(project_root, c_module, base)
        twin_paths[base] = twin
        pairs[base] = {
            "type": "parity_pair",
            "module": base,
            "python_impl": relpath(twin, project_root),
            "python_twin_module": twin_module,
            "c_module": c_module,
            "c_sources": c_sources,
            "detection": detection,
            "import_style": "none",
            "import_sites": [],
            "force_python_hint": f"import {twin_module} as m",
            "force_c_hint": f"import {base} as m",
        }

    # --- Pass 1a: explicit Lib/_py*.py twins -------------------------------
    if lib.is_dir():
        for twin in sorted(lib.glob("_py*.py")):
            files_examined += 1
            c_module, base = _c_module_and_base(twin.stem)
            _record_twin(base, twin, c_module, twin.stem, "explicit_py_twin")

    # --- Pass 1b: package twins, Lib/<pkg>/_<pkg>.py -----------------------
    # The rule the scanner used to lack. `Lib/zoneinfo/_zoneinfo.py` is a full
    # 25 KB pure-Python implementation and `Lib/zoneinfo/__init__.py` does the
    # canonical try/except accelerator import, but the `Lib/_py<name>.py`
    # heuristic never saw it: the pair was reported `python_twin_module: null,
    # confidence: low`, and CPY-0033 exists only because the twin was found by
    # hand afterwards.
    if lib.is_dir():
        for pkg_init in sorted(lib.glob("*/__init__.py")):
            pkg = pkg_init.parent.name
            twin = pkg_init.parent / f"_{pkg}.py"
            if not twin.is_file():
                continue
            files_examined += 1
            _record_twin(pkg, twin, f"_{pkg}", f"{pkg}._{pkg}", "package_twin")

    # --- Pass 2: accelerator imports in public modules ---------------------
    if lib.is_dir():
        candidates: set[str] = set()
        for entry in sorted(lib.iterdir()):
            if entry.name.startswith("_"):
                continue
            if entry.is_file() and entry.suffix == ".py":
                candidates.add(entry.stem)
            elif entry.is_dir() and (entry / "__init__.py").is_file():
                candidates.add(entry.name)

        for name in sorted(candidates):
            c_module = "_" + name
            # Cheap gate: only inspect a module's Python source if a same-named
            # C accelerator actually exists (avoids reading all of Lib/test/).
            c_sources = _locate_c_sources(project_root, c_module, name)
            if not c_sources:
                continue
            files = _module_python_files(lib, name)
            if not files:
                continue
            if max_files and files_examined >= max_files:
                break
            files_examined += len(files)
            import_style, sites, guarded = _detect_accelerator_import(files, name)
            if import_style == "none":
                continue
            site_rel = [relpath(s, project_root) for s in sites]
            bindings, accelerated, health = _dual_bindings(files, c_module)
            for kind, count in health.items():
                parse_health[kind] += count

            existing = pairs.get(name)
            if existing is not None:
                # Already found as a twin file -> upgrade to "both" and enrich.
                existing["detection"] = "both"
                existing["import_style"] = import_style
                existing["import_sites"] = site_rel
                existing["python_dispatcher"] = site_rel[0] if site_rel else None
                existing["dual_bindings"] = [b["public_name"] for b in bindings]
                existing["accelerator_import_guarded"] = guarded
                existing["_files"] = files
                existing["_bindings"] = bindings
                existing["_accelerated"] = accelerated
                if not existing["c_sources"]:
                    existing["c_sources"] = c_sources
            else:
                entry_point = files[0]
                pairs[name] = {
                    "type": "parity_pair",
                    "module": name,
                    "python_impl": relpath(entry_point, project_root),
                    "python_twin_module": None,
                    "c_module": c_module,
                    "c_sources": c_sources,
                    "detection": "accelerator_import",
                    "import_style": import_style,
                    "import_sites": site_rel,
                    "dual_bindings": [b["public_name"] for b in bindings],
                    "accelerator_import_guarded": guarded,
                    "force_python_hint": (
                        # Popping the PUBLIC module matters as much as blocking
                        # the accelerator: `os` imports `stat` (and `codecs`,
                        # `types`, ...) during interpreter startup, so by the
                        # time the prelude runs it is already in sys.modules
                        # with the C names bound. Without the pop, both
                        # "backends" are the accelerator and the differential
                        # silently tests nothing.
                        f"import sys; sys.modules[{c_module!r}] = None; "
                        f"sys.modules.pop({name!r}, None); import {name} as m"
                    ),
                    "force_c_hint": f"import {name} as m",
                    "_files": files,
                    "_bindings": bindings,
                    "_accelerated": accelerated,
                }

    # --- Finalize ----------------------------------------------------------
    findings: list[dict] = []
    rejected: list[dict] = []
    for base, pair in pairs.items():
        py_files = pair.pop("_files", None) or _module_python_files(lib, base)
        bindings = pair.pop("_bindings", None) or []
        accelerated = pair.pop("_accelerated", None) or set()
        pair.setdefault("dual_bindings", [])

        verified, evidence = _verify_c_module(
            project_root, pair["c_module"], pair["c_sources"]
        )
        pair["c_module_verified"] = verified
        pair["c_module_evidence"] = evidence
        if not verified:
            # `import <c_module>` would raise ModuleNotFoundError: this is not a
            # pair, and reporting it (at `high`, from a _py* filename) is worse
            # than reporting nothing.
            rejected.append(
                {
                    "module": base,
                    "c_module": pair["c_module"],
                    "python_impl": pair["python_impl"],
                    "detection": pair["detection"],
                    "reason": (
                        f"c_module '{pair['c_module']}' does not exist as an "
                        f"importable module: {evidence}"
                    ),
                }
            )
            skipped.append(
                {
                    "file": pair["python_impl"],
                    "reason": f"unverifiable c_module '{pair['c_module']}'",
                }
            )
            continue

        pair["confidence"] = _confidence(pair)
        twin_path = twin_paths.get(base)
        if twin_path is not None:
            health = module_structure(twin_path, pair["c_module"])["parsed_with"]
            parse_health[health] += 1
        probes = _derive_probes(
            lib,
            base,
            pair["c_module"],
            twin_path,
            py_files,
            bindings,
            accelerated,
        )
        unguarded = pair["detection"] == "accelerator_import" and not pair.get(
            "accelerator_import_guarded", True
        )
        if not probes or unguarded:
            # No second implementation to compare against. Two shapes:
            # `Lib/struct.py` is `from _struct import *` and nothing else (no
            # probes), and `Lib/csv.py` imports `_csv` unconditionally, so
            # blocking the accelerator breaks `import csv` outright. Say so
            # instead of ranking these above pairs that can actually be driven.
            pair["confidence"] = "low"
            pair["differentiable"] = False
            if unguarded:
                pair["notes"] = (
                    "the accelerator import is unconditional, so blocking the C "
                    f"module breaks `import {base}` entirely -- there is no "
                    "pure-Python fallback to compare against. Inventory only, "
                    "not an oracle."
                )
            else:
                pair["notes"] = (
                    "no pure-Python symbol to probe: the public module has no "
                    "fallback definitions, so there is no second implementation "
                    "to differentially test. Inventory only, not an oracle."
                )
        else:
            pair["differentiable"] = True
        pair["backend_assertion"] = {
            "probes": probes,
            "method": _BACKEND_METHOD,
            "trap": _BACKEND_TRAP,
        }
        pair["harness_command"] = (
            f"python find_parity_pairs.py <cpython> --emit-harness {base} --out <dir>"
        )
        findings.append(pair)

    findings.sort(key=lambda p: (_CONFIDENCE_RANK.get(p["confidence"], 9), p["module"]))
    rejected.sort(key=lambda p: p["module"])

    by_confidence: dict[str, int] = defaultdict(int)
    by_detection: dict[str, int] = defaultdict(int)
    for pair in findings:
        by_confidence[pair["confidence"]] += 1
        by_detection[pair["detection"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_examined,
        functions_analyzed=0,
        findings=findings,
        summary={
            "total_pairs": len(findings),
            "by_confidence": dict(by_confidence),
            "by_detection": dict(by_detection),
            "rejected_pairs": len(rejected),
            "pairs_with_dual_bindings": sum(
                1 for p in findings if p.get("dual_bindings")
            ),
            "differentiable_pairs": sum(1 for p in findings if p.get("differentiable")),
        },
        rejected_pairs=rejected,
        parse_health=dict(parse_health),
        skipped_files=skipped,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _split_args(argv: list[str]) -> tuple[list[str], str | None, str | None]:
    """Pull ``--emit-harness V`` / ``--out V`` out before the shared parser."""
    rest: list[str] = []
    emit: str | None = None
    out: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--emit-harness", "--out") and i + 1 < len(argv):
            if arg == "--emit-harness":
                emit = argv[i + 1]
            else:
                out = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--emit-harness="):
            emit = arg.split("=", 1)[1]
            i += 1
            continue
        if arg.startswith("--out="):
            out = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    return rest, emit, out


def _do_emit(result: dict, emit: str, out: str | None) -> int:
    pairs = result["findings"]
    if emit == "all":
        if not out:
            print("error: --emit-harness all requires --out DIR", file=sys.stderr)
            return 2
        selected = pairs
    else:
        selected = [p for p in pairs if p["module"] == emit]
        if not selected:
            known = ", ".join(p["module"] for p in pairs)
            print(
                f"error: no parity pair named '{emit}'. Known pairs: {known}",
                file=sys.stderr,
            )
            return 2
    if not out:
        sys.stdout.write(emit_harness(selected[0]))
        return 0
    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    for pair in selected:
        path = outdir / harness_filename(pair["module"])
        path.write_text(emit_harness(pair), encoding="utf-8")
        path.chmod(0o755)
        print(str(path))
    return 0


def main() -> None:
    try:
        argv, emit, out = _split_args(sys.argv[1:])
        target, max_files = parse_common_args(argv)
        result = analyze(target, max_files=max_files)
        if emit:
            sys.exit(_do_emit(result, emit, out))
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - top-level guard emits JSON error
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
