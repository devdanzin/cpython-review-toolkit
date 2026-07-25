"""Tests for the review-slice campaign: manifest, status cursor, context generator.

The manifest's whole value is the claim that it partitions the reviewable tree
exactly. That claim decays silently -- CPython adds and removes .c files every
release, and a manifest that quietly stops covering the tree still prints a
completion percentage. So the drift check is tested as carefully as the tools.
"""

import importlib.util
import json
import unittest
from pathlib import Path

from helpers import TempProject

_REPO = Path(__file__).resolve().parent.parent
_MANIFEST = _REPO / "plugins" / "cpython-review-toolkit" / "data" / "review_slices.json"

SIZE_CAP_LINES = 13_000
SIZE_CAP_FILES = 12


def _load_tool(name: str):
    path = _REPO / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict:
    with open(_MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


class TestManifestShape(unittest.TestCase):
    """Internal consistency -- runs without a CPython checkout."""

    def setUp(self):
        self.manifest = _manifest()
        self.slices = self.manifest["slices"]

    def test_no_file_is_owned_by_two_slices(self):
        seen: dict[str, str] = {}
        for sid, spec in self.slices.items():
            for f in spec["files"]:
                self.assertNotIn(f, seen, f"{f} owned by both {seen.get(f)} and {sid}")
                seen[f] = sid

    def test_every_file_is_under_objects_or_modules(self):
        for sid, spec in self.slices.items():
            for f in spec["files"]:
                self.assertRegex(f, r"^(Objects|Modules)/", f"{sid}: {f}")

    def test_tiers_and_statuses_are_from_the_vocabulary(self):
        for sid, spec in self.slices.items():
            self.assertIn(spec["tier"], {"A", "B", "C"}, sid)
            self.assertIn(spec["status"], {"pending", "in-progress", "done"}, sid)

    def test_order_covers_exactly_the_unfinished_slices(self):
        order = self.manifest["_meta"]["order"]
        self.assertEqual(len(order), len(set(order)), "duplicate id in order")
        for sid in order:
            self.assertIn(sid, self.slices, f"order names unknown slice {sid}")
        missing = {
            sid
            for sid, spec in self.slices.items()
            if spec["status"] != "done" and sid not in order
        }
        self.assertEqual(missing, set(), "pending slices absent from order")

    def test_slices_respect_the_sizing_rule(self):
        """Over the cap is allowed only for a single file, which needs passes > 1."""
        for sid, spec in self.slices.items():
            if spec["status"] == "done":
                continue  # historical runs are recorded as-run, not as-sized
            self.assertLessEqual(len(spec["files"]), SIZE_CAP_FILES, sid)
            if spec["lines"] > SIZE_CAP_LINES:
                self.assertEqual(
                    len(spec["files"]),
                    1,
                    f"{sid} is {spec['lines']} lines across "
                    f"{len(spec['files'])} files -- split it",
                )
                self.assertGreater(
                    spec["passes"],
                    1,
                    f"{sid} is {spec['lines']} lines in one file and must declare passes > 1",
                )

    def test_totals_match_the_slices(self):
        self.assertEqual(
            self.manifest["_meta"]["total_files"],
            sum(len(s["files"]) for s in self.slices.values()),
        )
        self.assertEqual(
            self.manifest["_meta"]["total_lines"],
            sum(s["lines"] for s in self.slices.values()),
        )

    def test_every_slice_stays_within_one_top_level_directory(self):
        make = _load_tool("make_slice_context")
        for sid, spec in self.slices.items():
            self.assertIn(make.corpus_of(spec), {"Objects", "Modules"}, sid)


class TestSliceStatus(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool("slice_status")

    def test_next_slice_is_the_first_pending_in_order(self):
        manifest = {
            "_meta": {"order": ["a", "b", "c"], "total_files": 0, "total_lines": 0},
            "slices": {
                "a": {"status": "done"},
                "b": {"status": "pending"},
                "c": {"status": "pending"},
            },
        }
        self.assertEqual(self.tool.next_slice(manifest), "b")

    def test_next_slice_is_none_when_the_campaign_is_complete(self):
        manifest = {
            "_meta": {"order": ["a"]},
            "slices": {"a": {"status": "done"}},
        }
        self.assertIsNone(self.tool.next_slice(manifest))

    def test_sync_recomputes_line_counts_from_the_tree(self):
        with TempProject(
            {
                "Objects/a.c": "one\ntwo\nthree\n",
                "Modules/b.c": "one\n",
            }
        ) as root:
            manifest = {
                "_meta": {"order": ["s1"], "total_files": 0, "total_lines": 999},
                "slices": {
                    "s1": {
                        "status": "pending",
                        "lines": 999,
                        "files": ["Objects/a.c", "Modules/b.c"],
                    }
                },
            }
            changes = self.tool.sync_lines(manifest, Path(root))
        self.assertEqual(manifest["slices"]["s1"]["lines"], 4)
        self.assertEqual(manifest["_meta"]["total_lines"], 4)
        self.assertEqual(manifest["_meta"]["total_files"], 2)
        self.assertTrue(any("999 -> 4" in c for c in changes), changes)

    def test_sync_is_idempotent_and_reports_nothing_when_current(self):
        with TempProject({"Objects/a.c": "one\ntwo\n"}) as root:
            manifest = {
                "_meta": {"order": [], "total_files": 1, "total_lines": 2},
                "slices": {
                    "s1": {"status": "pending", "lines": 2, "files": ["Objects/a.c"]}
                },
            }
            self.assertEqual(self.tool.sync_lines(manifest, Path(root)), [])

    def test_owner_of_maps_a_path_back_to_its_slice(self):
        manifest = {
            "slices": {
                "s1": {"files": ["Objects/x.c"]},
                "s2": {"files": ["Modules/y.c"]},
            }
        }
        self.assertEqual(self.tool.owner_of(manifest, "Modules/y.c"), "s2")
        self.assertIsNone(self.tool.owner_of(manifest, "Python/z.c"))


class TestDriftDetection(unittest.TestCase):
    """--verify is the guard against a manifest that silently stops covering the tree."""

    def setUp(self):
        self.tool = _load_tool("slice_status")
        self.excluded = {
            "prefixes": {"Objects/mimalloc/": "vendored"},
            "dir_names": ["clinic"],
            "dir_prefixes": ["_test"],
            "name_prefixes": ["_test"],
            "names": {"config.c": "build glue"},
        }

    def _manifest_for(self, files):
        return {
            "_meta": {
                "excluded": self.excluded,
                "order": [],
                "total_files": len(files),
            },
            "slices": {"s1": {"files": list(files), "status": "pending"}},
        }

    def test_a_matching_tree_reports_no_drift(self):
        with TempProject({"Objects/a.c": "int a;", "Modules/b.c": "int b;"}) as root:
            manifest = self._manifest_for(
                ["Objects/a.c", "Objects/object.c", "Modules/b.c"]
            )
            self.assertEqual(self.tool.verify(manifest, Path(root)), [])

    def test_a_new_file_in_the_tree_is_reported(self):
        with TempProject(
            {"Objects/a.c": "int a;", "Modules/brand_new.c": "int n;"}
        ) as root:
            manifest = self._manifest_for(["Objects/a.c", "Objects/object.c"])
            problems = self.tool.verify(manifest, Path(root))
            self.assertTrue(
                any("brand_new.c" in p and "unassigned" in p for p in problems),
                problems,
            )

    def test_a_file_that_left_the_tree_is_reported(self):
        with TempProject({"Objects/a.c": "int a;", "Modules/b.c": "int b;"}) as root:
            manifest = self._manifest_for(
                ["Objects/a.c", "Objects/object.c", "Modules/b.c", "Objects/deleted.c"]
            )
            problems = self.tool.verify(manifest, Path(root))
            self.assertTrue(
                any("deleted.c" in p and "GONE" in p for p in problems), problems
            )

    def test_a_tree_without_modules_is_rejected_rather_than_reported_clean(self):
        """A wrong --verify path must not read as 'no drift'."""
        with TempProject({"Objects/a.c": "int a;"}) as root:
            manifest = self._manifest_for(["Objects/a.c", "Objects/object.c"])
            problems = self.tool.verify(manifest, Path(root))
        self.assertTrue(any("does not exist" in p for p in problems), problems)

    def test_excluded_paths_do_not_count_as_drift(self):
        with TempProject(
            {
                "Objects/a.c": "int a;",
                "Objects/mimalloc/heap.c": "int h;",
                "Objects/clinic/a.c.h": "",
                "Modules/keep.c": "int k;",
                "Modules/_testcapi/x.c": "int x;",
                "Modules/config.c": "int c;",
            }
        ) as root:
            manifest = self._manifest_for(
                ["Objects/a.c", "Objects/object.c", "Modules/keep.c"]
            )
            self.assertEqual(self.tool.verify(manifest, Path(root)), [])

    def test_the_real_manifest_owns_no_file_its_own_rules_exclude(self):
        """A contradiction here makes --verify report drift that can never be fixed."""
        manifest = _manifest()
        excluded = manifest["_meta"]["excluded"]
        for sid, spec in manifest["slices"].items():
            for f in spec["files"]:
                self.assertFalse(
                    self.tool.is_excluded(excluded, f),
                    f"{sid} owns {f}, which the exclusion vocabulary drops",
                )

    def test_is_excluded_recognizes_each_rule_kind(self):
        excluded = self.excluded
        self.assertTrue(self.tool.is_excluded(excluded, "Objects/mimalloc/heap.c"))
        self.assertTrue(self.tool.is_excluded(excluded, "Objects/clinic/a.c.h"))
        self.assertTrue(self.tool.is_excluded(excluded, "Modules/_testcapi/x.c"))
        self.assertTrue(self.tool.is_excluded(excluded, "Modules/_testbuffer.c"))
        self.assertTrue(self.tool.is_excluded(excluded, "Modules/config.c"))
        self.assertFalse(self.tool.is_excluded(excluded, "Objects/listobject.c"))
        self.assertFalse(self.tool.is_excluded(excluded, "Modules/_io/textio.c"))


class TestMakeSliceContext(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool("make_slice_context")

    def test_corpus_rejects_a_slice_spanning_two_directories(self):
        with self.assertRaises(SystemExit):
            self.tool.corpus_of({"files": ["Objects/a.c", "Modules/b.c"]})

    def test_known_bug_files_parses_the_tsv_and_skips_comments(self):
        with TempProject({}) as root:
            tsv = Path(root) / "bugs.tsv"
            tsv.write_text(
                "# header\n"
                "gh-1\tObjects/a.c\t10\trecursion\tfoo\tnote\n"
                "CPY-2\tObjects/a.c\t20\tnull-deref\tbar\tnote\n"
                "gh-3\tModules/b.c\t5\trefcount\tbaz\tnote\n",
                encoding="utf-8",
            )
            table = self.tool.known_bug_files(tsv)
        self.assertEqual(table["Objects/a.c"], ["gh-1", "CPY-2"])
        self.assertEqual(table["Modules/b.c"], ["gh-3"])

    def test_missing_tsv_yields_an_empty_table_not_a_crash(self):
        self.assertEqual(self.tool.known_bug_files(Path("/nonexistent/x.tsv")), {})

    def test_catalog_files_collects_every_cited_site(self):
        with TempProject({}) as root:
            report = Path(root) / "reports" / "CPY-0001-thing"
            report.mkdir(parents=True)
            (report / "meta.json").write_text(
                json.dumps(
                    {
                        "id": "CPY-0001",
                        "sites": [
                            {"path": "Objects/a.c", "line": 1},
                            {"path": "Objects/a.c", "line": 2},
                            {"path": "Modules/b.c", "line": 3},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            table = self.tool.catalog_files(Path(root))
        self.assertEqual(table["Objects/a.c"], ["CPY-0001"])  # deduplicated
        self.assertEqual(table["Modules/b.c"], ["CPY-0001"])

    def test_end_to_end_writes_a_usable_run_context(self):
        files = {
            "Objects/alpha.c": '#include "Python.h"\nstatic int alpha(void) { return 1; }\n',
            "Objects/beta.c": '#include "Python.h"\nstatic int beta(void) { return 2; }\n',
        }
        with TempProject(files) as root:
            root = Path(root)
            manifest = {
                "_meta": {
                    "order": ["t1"],
                    "excluded": {},
                    "total_files": 2,
                    "total_lines": 4,
                },
                "slices": {
                    "t1": {
                        "family": "Test slice",
                        "tier": "A",
                        "scope": "Objects",
                        "status": "pending",
                        "passes": 1,
                        "oracle": None,
                        "notes": "a test slice",
                        "lines": 4,
                        "files": ["Objects/alpha.c", "Objects/beta.c"],
                    }
                },
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            reports = root / "reports_out"

            rc = self.tool.main(
                [
                    "t1",
                    "--cpython",
                    str(root),
                    "--manifest",
                    str(manifest_path),
                    "--reports-dir",
                    str(reports),
                    "--quiet",
                ]
            )
            self.assertEqual(rc, 0)

            run_context = (reports / "t1" / "preflight" / "RUN_CONTEXT.md").read_text()
            self.assertIn("slice `t1`", run_context)
            self.assertIn("Objects/alpha.c", run_context)
            self.assertIn("New territory", run_context)
            self.assertIn("Check the denominator", run_context)
            self.assertTrue(
                (reports / "t1" / "preflight" / "informed_briefing.md").is_file()
            )

            # Sample JSON must be sample-scoped, not a filter of a corpus scan.
            sample = json.loads(
                (reports / "t1" / "scanners" / "scan_refcounts.sample.json").read_text()
            )
            self.assertEqual(
                sample["_sample"]["files_scanned"],
                ["Objects/alpha.c", "Objects/beta.c"],
            )
            self.assertEqual(sample["_sample"]["files_missing"], [])

    def test_a_slice_naming_an_absent_file_fails_loudly(self):
        with TempProject({"Objects/alpha.c": "int a;"}) as root:
            root = Path(root)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "_meta": {"order": ["t1"], "excluded": {}},
                        "slices": {
                            "t1": {
                                "family": "T",
                                "tier": "A",
                                "scope": "Objects",
                                "status": "pending",
                                "passes": 1,
                                "oracle": None,
                                "notes": "",
                                "lines": 1,
                                "files": ["Objects/alpha.c", "Objects/vanished.c"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit) as ctx:
                self.tool.main(
                    [
                        "t1",
                        "--cpython",
                        str(root),
                        "--manifest",
                        str(manifest_path),
                        "--reports-dir",
                        str(root / "out"),
                    ]
                )
        self.assertIn("vanished.c", str(ctx.exception))

    def test_an_unknown_slice_id_fails_loudly(self):
        with TempProject({}) as root:
            root = Path(root)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps({"_meta": {"order": []}, "slices": {}}), encoding="utf-8"
            )
            with self.assertRaises(SystemExit) as ctx:
                self.tool.main(
                    [
                        "nope",
                        "--cpython",
                        str(root),
                        "--manifest",
                        str(manifest_path),
                    ]
                )
        self.assertIn("nope", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
