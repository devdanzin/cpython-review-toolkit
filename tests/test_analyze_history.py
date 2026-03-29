"""Tests for analyze_history.py."""

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("analyze_history")


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
        self.assertEqual(args["max_commits"], 2000)
        self.assertFalse(args["no_function"])

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

    def test_combined(self):
        args = mod.parse_args([
            "Modules/", "--days", "180", "--max-commits", "5000",
            "--no-function",
        ])
        self.assertEqual(args["path"], "Modules/")
        self.assertEqual(args["days"], 180)
        self.assertEqual(args["max_commits"], 5000)
        self.assertTrue(args["no_function"])


if __name__ == "__main__":
    unittest.main()
