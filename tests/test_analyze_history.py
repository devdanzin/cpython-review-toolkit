"""Tests for analyze_history.py."""

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("analyze_history")


def _git_available() -> bool:
    return shutil.which("git") is not None


class TempGitRepo:
    """A throwaway git repo with a scripted commit history.

    Takes a list of ``(subject, {relpath: content})`` steps and commits each in
    order, so tests can exercise the real ``git log`` / ``git blame`` paths.
    """

    def __init__(self, steps: list[tuple[str, dict[str, str]]]):
        self._steps = steps
        self._tmpdir: str | None = None

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self._tmpdir, capture_output=True,
            text=True, errors="replace", check=True,
        )
        return result.stdout

    def __enter__(self) -> Path:
        self._tmpdir = tempfile.mkdtemp(prefix="cpyrt_git_")
        root = Path(self._tmpdir)
        self._git("init", "-q")
        self._git("config", "user.name", "Test Author")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "commit.gpgsign", "false")
        # CPython root markers so find_project_root() locks on to this dir.
        (root / "Include").mkdir(exist_ok=True)
        (root / "Objects").mkdir(exist_ok=True)
        (root / "Include" / "Python.h").write_text("/* h */\n", encoding="utf-8")
        (root / "Objects" / "object.c").write_text("/* c */\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "Initial import")
        for subject, files in self._steps:
            for relpath, content in files.items():
                path = root / relpath
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            self._git("add", "-A")
            self._git("commit", "-q", "-m", subject)
        return root

    def __exit__(self, *args):
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)


class TestClassifyCommit(unittest.TestCase):
    """Test commit message classification."""

    def test_fix_keywords(self):
        self.assertEqual(mod.classify_commit("Fix null pointer crash"), "fix")
        self.assertEqual(mod.classify_commit("fix refcount leak"), "fix")
        self.assertEqual(mod.classify_commit("Bug in decref path"), "fix")
        self.assertEqual(mod.classify_commit("Resolve segfault"), "fix")

    def test_cpython_fix_keywords(self):
        self.assertEqual(mod.classify_commit("Fix refcount leak"), "fix")
        self.assertEqual(mod.classify_commit("Fix null deref"), "fix")
        self.assertEqual(mod.classify_commit("Fix segfault in parser"), "fix")
        self.assertEqual(mod.classify_commit("Fix GIL deadlock"), "fix")
        self.assertEqual(mod.classify_commit("Fix decref on error"), "fix")

    def test_docs_keywords(self):
        self.assertEqual(mod.classify_commit("Update documentation"), "docs")
        self.assertEqual(mod.classify_commit("Typo in readme"), "docs")

    def test_test_keywords(self):
        self.assertEqual(mod.classify_commit("Add test for parser"), "test")

    def test_refactor_keywords(self):
        self.assertEqual(mod.classify_commit("Refactor ceval loop"), "refactor")
        self.assertEqual(
            mod.classify_commit("Convert to Argument Clinic"), "refactor",
        )

    def test_feature_keywords(self):
        self.assertEqual(mod.classify_commit("Add new method"), "feature")
        self.assertEqual(
            mod.classify_commit("Implement PEP 999"), "feature",
        )

    def test_chore_keywords(self):
        self.assertEqual(mod.classify_commit("Bump version"), "chore")
        self.assertEqual(mod.classify_commit("Merge branch"), "chore")

    def test_unknown(self):
        self.assertEqual(mod.classify_commit("xyzzy plugh"), "unknown")

    def test_first_match_wins(self):
        # "fix" appears before "feature" in rules.
        self.assertEqual(
            mod.classify_commit("Fix by adding new check"), "fix",
        )


class TestParseGitLog(unittest.TestCase):
    """Test git log parsing."""

    def test_basic_parsing(self):
        log_lines = [
            "COMMIT:abc123|2026-01-15T10:00:00+00:00|Author|Fix null check\n",
            "5\t2\tModules/foo.c\n",
            "3\t1\tModules/bar.c\n",
            "\n",
            "COMMIT:def456|2026-01-14T10:00:00+00:00|Author|Add feature\n",
            "10\t0\tModules/foo.c\n",
        ]
        commits, file_stats = mod.parse_git_log(iter(log_lines), 100)
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0]["type"], "fix")
        self.assertEqual(commits[1]["type"], "feature")
        self.assertEqual(len(commits[0]["files"]), 2)

        # File stats should have both files.
        files = {fs["file"] for fs in file_stats}
        self.assertIn("Modules/foo.c", files)
        self.assertIn("Modules/bar.c", files)

        # foo.c should have 2 commits.
        foo = next(fs for fs in file_stats if fs["file"] == "Modules/foo.c")
        self.assertEqual(foo["commits"], 2)

    def test_max_commits_cap(self):
        log_lines = [
            f"COMMIT:abc{i:03d}|2026-01-{i+1:02d}T10:00:00+00:00|A|msg\n"
            for i in range(10)
        ]
        commits, _ = mod.parse_git_log(iter(log_lines), 3)
        self.assertEqual(len(commits), 3)

    def test_binary_file_stats(self):
        log_lines = [
            "COMMIT:abc123|2026-01-15T10:00:00+00:00|A|Update\n",
            "-\t-\timage.png\n",
        ]
        commits, file_stats = mod.parse_git_log(iter(log_lines), 100)
        self.assertEqual(len(commits), 1)
        png = next(
            (fs for fs in file_stats if fs["file"] == "image.png"), None,
        )
        self.assertIsNotNone(png)
        self.assertEqual(png["lines_added"], 0)

    def test_empty_log(self):
        commits, file_stats = mod.parse_git_log(iter([]), 100)
        self.assertEqual(len(commits), 0)
        self.assertEqual(len(file_stats), 0)


class TestCFunctionBoundaries(unittest.TestCase):
    """Test C function boundary detection for history analysis."""

    def test_simple_function(self):
        with TempProject({
            "test.c": (
                "static int\n"
                "my_func(int x)\n"
                "{\n"
                "    return x + 1;\n"
                "}\n"
            ),
        }, cpython_markers=False) as root:
            funcs = mod.get_c_function_boundaries(root / "test.c")
            self.assertEqual(len(funcs), 1)
            self.assertEqual(funcs[0]["name"], "my_func")

    def test_multiline_signature(self):
        with TempProject({
            "test.c": (
                "static int\n"
                "init_sockobject(socket_state *state,\n"
                "                PySocketSockObject *s,\n"
                "                int family)\n"
                "{\n"
                "    s->sock_family = family;\n"
                "    return 0;\n"
                "}\n"
            ),
        }, cpython_markers=False) as root:
            funcs = mod.get_c_function_boundaries(root / "test.c")
            names = [f["name"] for f in funcs]
            self.assertIn("init_sockobject", names)

    def test_clinic_comment(self):
        with TempProject({
            "test.c": (
                "static int\n"
                "sock_initobj_impl(PySocketSockObject *self, int family)\n"
                "/*[clinic end generated code: output=abc input=def]*/\n"
                "{\n"
                "    self->sock_family = family;\n"
                "    return 0;\n"
                "}\n"
            ),
        }, cpython_markers=False) as root:
            funcs = mod.get_c_function_boundaries(root / "test.c")
            names = [f["name"] for f in funcs]
            self.assertIn("sock_initobj_impl", names)


class TestModuleFamilies(unittest.TestCase):
    """Test CPython module family detection."""

    def test_hash_family(self):
        family = mod.get_module_family("Modules/sha1module.c")
        self.assertEqual(family, "hash")

    def test_hash_siblings(self):
        siblings = mod.get_family_members("Modules/sha1module.c")
        self.assertIn("Modules/sha2module.c", siblings)
        self.assertIn("Modules/md5module.c", siblings)
        self.assertNotIn("Modules/sha1module.c", siblings)

    def test_dbm_family(self):
        family = mod.get_module_family("Modules/_dbmmodule.c")
        self.assertEqual(family, "dbm")

    def test_unknown_file(self):
        family = mod.get_module_family("Modules/unknown.c")
        self.assertIsNone(family)

    def test_no_siblings_for_unknown(self):
        siblings = mod.get_family_members("Modules/unknown.c")
        self.assertEqual(siblings, [])

    def test_io_family(self):
        family = mod.get_module_family("Modules/_io/fileio.c")
        self.assertEqual(family, "io")


class TestCoChangeClusters(unittest.TestCase):
    """Test co-change cluster detection."""

    def test_basic_co_changes(self):
        commits = [
            {"files": ["a.c", "b.c"]},
            {"files": ["a.c", "b.c"]},
            {"files": ["a.c", "b.c"]},
            {"files": ["a.c", "c.c"]},
        ]
        clusters = mod.compute_co_change_clusters(commits, min_co_changes=3)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["file_a"], "a.c")
        self.assertEqual(clusters[0]["file_b"], "b.c")
        self.assertEqual(clusters[0]["co_change_count"], 3)

    def test_no_clusters_below_threshold(self):
        commits = [
            {"files": ["a.c", "b.c"]},
            {"files": ["a.c", "b.c"]},
        ]
        clusters = mod.compute_co_change_clusters(commits, min_co_changes=3)
        self.assertEqual(len(clusters), 0)

    def test_max_pairs_cap(self):
        commits = [
            {"files": [f"f{i}.c" for i in range(10)]},
        ] * 5  # 5 identical commits with 10 files each
        clusters = mod.compute_co_change_clusters(
            commits, min_co_changes=3, max_pairs=5,
        )
        self.assertLessEqual(len(clusters), 5)


class TestParseArgs(unittest.TestCase):
    """Test argument parsing."""

    def test_defaults(self):
        args = mod.parse_args([])
        self.assertEqual(args["days"], 90)
        # TK-4: the old 2000 cap discarded 7,203 of Objects/'s 9,203 commits
        # on a full window while the uncapped run takes ~11 s.
        self.assertEqual(args["max_commits"], 50000)
        self.assertEqual(args["workers"], 8)
        self.assertFalse(args["no_function"])
        self.assertFalse(args["no_density"])
        self.assertEqual(args["unknown_args"], [])
        self.assertIsNone(args["introduced_by"])

    def test_days(self):
        args = mod.parse_args(["--days", "365"])
        self.assertEqual(args["days"], 365)

    def test_last(self):
        args = mod.parse_args(["--last", "50"])
        self.assertEqual(args["last"], 50)

    def test_no_function(self):
        args = mod.parse_args(["--no-function"])
        self.assertTrue(args["no_function"])

    def test_path(self):
        args = mod.parse_args(["Modules/"])
        self.assertEqual(args["path"], "Modules/")

    def test_workers(self):
        args = mod.parse_args(["--workers", "4"])
        self.assertEqual(args["workers"], 4)

    def test_combined(self):
        args = mod.parse_args([
            "Modules/", "--days", "180", "--max-commits", "5000",
            "--workers", "16", "--no-function",
        ])
        self.assertEqual(args["path"], "Modules/")
        self.assertEqual(args["days"], 180)
        self.assertEqual(args["max_commits"], 5000)
        self.assertEqual(args["workers"], 16)
        self.assertTrue(args["no_function"])

    def test_density_flags(self):
        args = mod.parse_args([
            "--density-top", "5", "--density-days", "365", "--no-follow",
        ])
        self.assertEqual(args["density_top"], 5)
        self.assertEqual(args["density_days"], 365)
        self.assertTrue(args["no_follow"])

    def test_introduced_by_flag(self):
        args = mod.parse_args(["--introduced-by", "Objects/tupleobject.c:412"])
        self.assertEqual(args["introduced_by"], "Objects/tupleobject.c:412")


class TestUnknownArguments(unittest.TestCase):
    """TK-3: an unrecognized flag must never be silently dropped."""

    def test_unknown_flag_is_collected(self):
        # `--months 420` used to run happily at the default 90-day window.
        args = mod.parse_args(["Objects/", "--months", "420"])
        self.assertIn("--months", args["unknown_args"])
        self.assertEqual(args["days"], 90)

    def test_short_unknown_flag_is_collected(self):
        args = mod.parse_args(["-x"])
        self.assertEqual(args["unknown_args"], ["-x"])

    def test_flag_missing_its_value_is_collected(self):
        args = mod.parse_args(["--days"])
        self.assertTrue(
            any("--days" in u for u in args["unknown_args"]), args["unknown_args"],
        )

    def test_non_integer_value_is_collected(self):
        args = mod.parse_args(["--days", "soon"])
        self.assertTrue(
            any("--days soon" in u for u in args["unknown_args"]),
            args["unknown_args"],
        )
        self.assertEqual(args["days"], 90)

    def test_analyze_refuses_to_run_with_unknown_args(self):
        stderr = io.StringIO()
        original = sys.stderr
        sys.stderr = stderr
        try:
            result = mod.analyze(["--months", "420"])
        finally:
            sys.stderr = original
        self.assertEqual(result["type"], "UnknownArguments")
        self.assertIn("--months", result["unknown_args"])
        self.assertIn("--days", result["known_flags"])
        # ...and it says so on stderr, not only in the JSON.
        self.assertIn("--months", stderr.getvalue())

    def test_known_flags_are_all_documented_in_the_docstring(self):
        for flag in mod.KNOWN_FLAGS:
            self.assertIn(flag, mod.__doc__, f"{flag} missing from usage text")


class TestSourceSuffixes(unittest.TestCase):
    """TK-5: a C-source toolkit must not parse Python files."""

    def test_python_files_are_not_discovered(self):
        self.assertNotIn(".py", mod.SOURCE_SUFFIXES)
        self.assertEqual(mod.SOURCE_SUFFIXES, (".c", ".h"))

    def test_no_ast_based_python_boundary_helper_remains(self):
        self.assertFalse(hasattr(mod, "get_py_function_boundaries"))

    def test_get_function_boundaries_ignores_python(self):
        with TempProject({
            "mod.py": "def f():\n    return 1\n",
        }, cpython_markers=False) as root:
            self.assertEqual(mod.get_function_boundaries(root / "mod.py"), [])

    def test_get_function_boundaries_handles_c(self):
        with TempProject({
            "t.c": "static int\nf(int x)\n{\n    return x;\n}\n",
        }, cpython_markers=False) as root:
            funcs = mod.get_function_boundaries(root / "t.c")
            self.assertEqual([f["name"] for f in funcs], ["f"])


class TestCrashClassAndFixScoring(unittest.TestCase):
    """The re-scored fix classifier: crash weighting + hygiene demotion."""

    def test_crash_classes(self):
        self.assertEqual(mod.crash_class("Fix use-after-free in list"), "use-after-free")
        self.assertEqual(mod.crash_class("gh-1: double free in dealloc"), "double-free")
        self.assertEqual(mod.crash_class("Fix segfault on deep tuple"), "crash")
        self.assertEqual(mod.crash_class("Fix refleak in subs_tvars"), "memory-leak")
        self.assertEqual(mod.crash_class("Fix data race on ob_refcnt"), "data-race")
        self.assertEqual(mod.crash_class("Fix integer overflow in resize"), "overflow")
        self.assertIsNone(mod.crash_class("Use Py_NewRef() in Objects/"))

    def test_crash_fix_with_issue_ref_is_high_confidence(self):
        confidence, klass = mod.score_fix("gh-154318: Fix segfault in tuple_hash")
        self.assertEqual(confidence, "high")
        self.assertEqual(klass, "crash")

    def test_defect_named_without_a_fix_verb_still_scores(self):
        # "Handle allocate_weakref returning NULL" names a defect, no verb.
        confidence, klass = mod.score_fix(
            "gh-121652: Handle allocate_weakref returning NULL",
        )
        self.assertIn(confidence, ("high", "medium"))
        self.assertEqual(klass, "null-deref")
        self.assertEqual(
            mod.classify_commit("gh-121652: Handle allocate_weakref returning NULL"),
            "fix",
        )

    def test_hygiene_sweep_is_demoted_out_of_the_fix_bucket(self):
        for subject in (
            "gh-99300: Use Py_NewRef() in Objects/",
            "gh-111178: Fix function signatures in structseq.c",
            "Remove unused variable",
            "Clean up whitespace",
        ):
            with self.subTest(subject=subject):
                confidence, _ = mod.score_fix(subject)
                self.assertEqual(confidence, "none")
                self.assertNotEqual(mod.classify_commit(subject), "fix")

    def test_bare_fix_verb_is_low_confidence(self):
        confidence, klass = mod.score_fix("Fix by adding new check")
        self.assertEqual(confidence, "low")
        self.assertIsNone(klass)
        self.assertEqual(mod.classify_commit("Fix by adding new check"), "fix")

    def test_parse_git_log_attaches_scores(self):
        log_lines = [
            "COMMIT:abc123|2026-01-15T10:00:00+00:00|A|gh-1: Fix segfault\n",
            "5\t2\tObjects/foo.c\n",
        ]
        commits, _ = mod.parse_git_log(iter(log_lines), 100)
        self.assertEqual(commits[0]["fix_confidence"], "high")
        self.assertEqual(commits[0]["crash_class"], "crash")


class TestParseLineSpec(unittest.TestCase):
    """--introduced-by argument parsing."""

    def test_single_line(self):
        self.assertEqual(
            mod.parse_line_spec("Objects/tupleobject.c:412"),
            ("Objects/tupleobject.c", 412, 412),
        )

    def test_range(self):
        self.assertEqual(
            mod.parse_line_spec("Objects/x.c:10-20"), ("Objects/x.c", 10, 20),
        )

    def test_missing_colon(self):
        with self.assertRaises(ValueError):
            mod.parse_line_spec("Objects/x.c")

    def test_non_numeric(self):
        with self.assertRaises(ValueError):
            mod.parse_line_spec("Objects/x.c:abc")

    def test_inverted_range(self):
        with self.assertRaises(ValueError):
            mod.parse_line_spec("Objects/x.c:20-10")


# Two crash-shaped fixes around one mechanical hygiene sweep, so the density
# pass has something to count and something it must NOT count.
_HISTORY_STEPS: list[tuple[str, dict[str, str]]] = [
    ("gh-1: Fix segfault in widget_dealloc", {
        "Objects/widget.c":
            "static void\nwidget_dealloc(PyObject *o)\n{\n    free(o);\n}\n",
    }),
    ("Use Py_NewRef() in Objects/", {
        "Objects/widget.c":
            "static void\nwidget_dealloc(PyObject *o)\n{\n"
            "    Py_DECREF(o);\n    free(o);\n}\n",
    }),
    ("gh-2: Fix use-after-free in widget_dealloc", {
        "Objects/widget.c":
            "static void\nwidget_dealloc(PyObject *o)\n{\n"
            "    PyObject *tmp = o;\n    Py_CLEAR(tmp);\n}\n",
    }),
]


@unittest.skipUnless(_git_available(), "git not installed")
class TestAgainstRealGitHistory(unittest.TestCase):
    """End-to-end coverage of the git-backed additions."""

    STEPS = _HISTORY_STEPS

    def test_envelope_carries_the_new_keys(self):
        with TempGitRepo(self.STEPS) as root:
            report = mod.analyze([str(root), "--days", "36500", "--no-function"])
        for key in (
            "is_shallow_clone", "repo_total_commits", "repo_first_commit_date",
            "timeout_hit", "notes", "watchlist",
        ):
            self.assertIn(key, report)
        self.assertFalse(report["is_shallow_clone"])
        self.assertGreaterEqual(report["repo_total_commits"], 4)
        self.assertFalse(report["time_range"]["commit_cap_applied"])

    def test_commit_cap_is_promoted_into_notes(self):
        with TempGitRepo(self.STEPS) as root:
            report = mod.analyze([
                str(root), "--days", "36500", "--max-commits", "2",
                "--no-function", "--no-density",
            ])
        self.assertTrue(report["time_range"]["commit_cap_applied"])
        self.assertTrue(
            any("COMMIT CAP APPLIED" in n for n in report["notes"]),
            report["notes"],
        )

    def test_shallow_clone_is_detected_and_noted(self):
        with TempGitRepo(self.STEPS) as root:
            shallow_dir = Path(tempfile.mkdtemp(prefix="cpyrt_shallow_"))
            try:
                clone = shallow_dir / "c"
                subprocess.run(
                    ["git", "clone", "-q", "--depth", "1",
                     f"file://{root}", str(clone)],
                    check=True, capture_output=True, text=True,
                )
                self.assertTrue(mod.is_shallow_clone(clone))
                report = mod.analyze([str(clone), "--no-function", "--no-density"])
                self.assertTrue(report["is_shallow_clone"])
                self.assertTrue(
                    any("SHALLOW CLONE" in n for n in report["notes"]),
                    report["notes"],
                )
            finally:
                shutil.rmtree(shallow_dir, ignore_errors=True)

    def test_bugfix_density_and_watchlist(self):
        with TempGitRepo(self.STEPS) as root:
            report = mod.analyze([
                str(root), "--days", "36500", "--no-function",
            ])
        widget = next(
            f for f in report["file_churn"] if f["file"] == "Objects/widget.c"
        )
        # Two crash fixes; the Py_NewRef sweep must not be counted.
        self.assertEqual(widget["crash_fix_commits"], 2)
        self.assertEqual(widget["fix_commits"], 2)
        self.assertTrue(widget["follow_renames"])
        self.assertGreater(widget["crash_fix_density"], 0)
        self.assertEqual(report["watchlist"][0]["file"], "Objects/widget.c")
        self.assertTrue(
            any("watchlist" in n for n in report["notes"]), report["notes"],
        )

    def test_introduced_by_reports_the_originating_commit(self):
        with TempGitRepo(self.STEPS) as root:
            report = mod.analyze([
                str(root), "--introduced-by", "Objects/widget.c:5",
            ])
        self.assertEqual(report["mode"], "introduced-by")
        self.assertEqual(report["target"]["line_start"], 5)
        self.assertIn("Py_CLEAR", report["line_text"])
        self.assertTrue(report["line_history"])
        self.assertIn(
            "use-after-free", report["last_touched_by"]["subject"].lower(),
        )
        self.assertEqual(report["last_touched_by"]["crash_class"], "use-after-free")
        self.assertIn("files", report["last_touched_by"])

    def test_introduced_by_rejects_a_bad_spec(self):
        with TempGitRepo(self.STEPS) as root:
            stderr = io.StringIO()
            original = sys.stderr
            sys.stderr = stderr
            try:
                report = mod.analyze([str(root), "--introduced-by", "nope"])
            finally:
                sys.stderr = original
        self.assertEqual(report["type"], "ValueError")

    def test_introduced_by_missing_file(self):
        with TempGitRepo(self.STEPS) as root:
            report = mod.analyze([
                str(root), "--introduced-by", "Objects/ghost.c:1",
            ])
        self.assertIn("error", report)


@unittest.skipUnless(_git_available(), "git not installed")
class TestNonUtf8History(unittest.TestCase):
    """A non-UTF-8 byte anywhere in a diff must not abort the analysis.

    CPython's history contains exactly one such commit (42bb126f0aa, the 2015
    listsort.txt UTF-8 conversion), and under strict decoding it took down the
    whole 9,203-commit run with UnicodeDecodeError. Both git subprocess sites
    must pass errors="replace".
    """

    def _repo(self) -> Path:
        tmpdir = tempfile.mkdtemp(prefix="cpyrt_latin1_")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        root = Path(tmpdir)
        (root / "Include").mkdir()
        (root / "Objects").mkdir()
        (root / "Include" / "Python.h").write_bytes(b"/* h */\n")
        # Latin-1 bytes in the *file content*: git never transcodes diff
        # payload, so this is what actually reaches the decoder.
        (root / "Objects" / "object.c").write_bytes(
            b"/* Alejandro L\xf3pez-Ortiz */\nstatic void f(void) {}\n"
        )
        for args in (
            ["init", "-q"],
            ["config", "user.name", "Test Author"],
            ["config", "user.email", "test@example.invalid"],
            ["config", "commit.gpgsign", "false"],
            ["add", "-A"],
            ["commit", "-q", "-m", "gh-1: Fix segfault in listsort"],
        ):
            subprocess.run(
                ["git", *args], cwd=tmpdir, check=True,
                capture_output=True, text=True, errors="replace",
            )
        return root

    def test_strict_decoding_would_have_failed(self):
        # Guard the guard: if this ever stops raising, the fixture no longer
        # exercises the bug and the test below proves nothing.
        root = self._repo()
        with self.assertRaises(UnicodeDecodeError):
            subprocess.run(
                ["git", "show", "--format=", "--patch", "HEAD"],
                cwd=str(root), capture_output=True, text=True,
            )

    def test_analysis_survives_a_non_utf8_diff(self):
        root = self._repo()
        report = mod.analyze([
            str(root), "--days", "36500", "--no-function", "--no-density",
        ])
        self.assertNotIn("error", report)
        self.assertEqual(report["summary"]["total_commits"], 1)
        self.assertEqual(len(report["recent_fixes"]), 1)
        self.assertIn("Alejandro", report["recent_fixes"][0]["diff"])


if __name__ == "__main__":
    unittest.main()
