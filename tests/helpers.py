"""Helpers for importing scripts as modules and creating test fixtures."""

import importlib.util
import tempfile
from pathlib import Path


_SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "cpython-review-toolkit"
    / "scripts"
)


def import_script(name: str):
    """Import a script from the scripts/ directory as a module.

    Usage:
        mod = import_script("analyze_includes")
        result = mod.analyze(str(root))
    """
    script_path = _SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    module = importlib.util.module_from_spec(spec)
    # Don't add to sys.modules to avoid side-effects between tests.
    spec.loader.exec_module(module)
    return module


class TempProject:
    """Context manager that creates a temporary C project on disk.

    For CPython-style projects, creates marker files so that
    find_cpython_root() can detect the project layout.

    Usage:
        with TempProject({
            "Include/Python.h": "#ifndef PYTHON_H\\n#define PYTHON_H\\n#endif",
            "Objects/object.c": '#include "Python.h"\\nvoid foo(void) {}',
        }) as root:
            mod = import_script("analyze_includes")
            result = mod.analyze(str(root))
    """

    def __init__(self, files: dict[str, str], cpython_markers: bool = True):
        self._files = files
        self._cpython_markers = cpython_markers
        self._tmpdir = None

    def __enter__(self) -> Path:
        self._tmpdir = tempfile.mkdtemp(prefix="cpyrt_test_")
        root = Path(self._tmpdir)
        for relpath, content in self._files.items():
            filepath = root / relpath
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content, encoding="utf-8")
        if self._cpython_markers:
            # Create CPython root markers if not already present.
            marker1 = root / "Include" / "Python.h"
            marker2 = root / "Objects" / "object.c"
            if not marker1.exists():
                marker1.parent.mkdir(parents=True, exist_ok=True)
                marker1.write_text(
                    "#ifndef Py_PYTHON_H\n#define Py_PYTHON_H\n#endif\n",
                    encoding="utf-8",
                )
            if not marker2.exists():
                marker2.parent.mkdir(parents=True, exist_ok=True)
                marker2.write_text("/* object.c */\n", encoding="utf-8")
        return root

    def __exit__(self, *args):
        import shutil
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
