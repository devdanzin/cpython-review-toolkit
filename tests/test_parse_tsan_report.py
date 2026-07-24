"""Tests for parse_tsan_report.py — TSan report triage over CPython's own source.

Unlike the tree-sitter scanners, this parser consumes plain TSan text, so the
fixtures are inline report blocks (no C project on disk required).
"""

import unittest

from helpers import import_script

# A race in CPython's own dict internals — the target. Two racing sites:
# insertdict (write) and lookdict (read), both in Objects/dictobject.c.
_CPYTHON_RACE = """\
==================
WARNING: ThreadSanitizer: data race (pid=12345)
  Write of size 8 at 0x7b0400000640 by thread T2:
    #0 insertdict /home/py/cpython/Objects/dictobject.c:1123 (python3.14t+0x2a1b3c)
    #1 dict_ass_sub /home/py/cpython/Objects/dictobject.c:2405 (python3.14t+0x2a2000)
    #2 _PyEval_EvalFrameDefault /home/py/cpython/Python/ceval.c:5010 (python3.14t+0x3f0)

  Previous read of size 8 at 0x7b0400000640 by thread T1:
    #0 lookdict /home/py/cpython/Objects/dictobject.c:998 (python3.14t+0x2a1000)
    #1 _PyEval_EvalFrameDefault /home/py/cpython/Python/ceval.c:5010 (python3.14t+0x3f0)

  Location is heap block of size 232 at 0x7b0400000600 allocated by thread T1:
    #0 malloc <null> (libtsan.so.2+0x2a2f9)
    #1 _PyObject_Malloc /home/py/cpython/Objects/obmalloc.c:1234 (python3.14t+0x1a0000)

  Thread T2 (tid=12347, running) created by main thread at:
    #0 pthread_create <null> (libtsan.so.2+0x605b8)
    #1 do_start_new_thread /home/py/cpython/Modules/_threadmodule.c:1567 (python3+0x4b0)

SUMMARY: ThreadSanitizer: data race /home/py/cpython/Objects/dictobject.c:1123 in insertdict
==================
"""

# A race whose frames are all pure thread scaffolding / libc — noise.
_NOISE_RACE = """\
==================
WARNING: ThreadSanitizer: data race (pid=12345)
  Write of size 4 at 0x7b0800000010 by thread T5:
    #0 start_thread <null> (libc.so.6+0x8944)
    #1 clone <null> (libc.so.6+0x105bf)

  Previous write of size 4 at 0x7b0800000010 by thread T4:
    #0 start_thread <null> (libc.so.6+0x8944)
    #1 clone <null> (libc.so.6+0x105bf)

SUMMARY: ThreadSanitizer: data race /usr/lib/libc.so.6 in start_thread
==================
"""

# A race in a CPython built-in test-harness module — noise (not the target).
_TESTHARNESS_RACE = """\
==================
WARNING: ThreadSanitizer: data race (pid=12345)
  Write of size 8 at 0x7b0c00000020 by thread T3:
    #0 test_racy_helper /home/py/cpython/Modules/_testcapimodule.c:4210 (_testcapi+0x99)

  Previous read of size 8 at 0x7b0c00000020 by thread T2:
    #0 test_racy_helper /home/py/cpython/Modules/_testcapimodule.c:4210 (_testcapi+0x99)

SUMMARY: ThreadSanitizer: data race Modules/_testcapimodule.c:4210 in test_racy_helper
==================
"""


class TestParseTsanReport(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("parse_tsan_report")

    # --- core extraction ---------------------------------------------------

    def test_extracts_two_racing_sites(self):
        result = self.mod.parse_report(_CPYTHON_RACE)
        self.assertEqual(result["total_warnings"], 1)
        self.assertEqual(result["unique_races"], 1)
        finding = result["findings"][0]
        sites = {s["site"] for s in finding["sites"]}
        self.assertEqual(sites, {"dictobject.c:insertdict", "dictobject.c:lookdict"})
        # The two accesses are captured with their access types.
        access_types = [a["access_type"] for a in finding["accesses"]]
        self.assertEqual(len(access_types), 2)
        self.assertTrue(any("Write" in t for t in access_types))
        self.assertTrue(any("read" in t.lower() for t in access_types))

    def test_signature_is_unordered_site_pair(self):
        result = self.mod.parse_report(_CPYTHON_RACE)
        # Sorted, so the signature is order-independent.
        self.assertEqual(
            result["findings"][0]["signature"],
            "dictobject.c:insertdict | dictobject.c:lookdict",
        )

    # --- deduplication -----------------------------------------------------

    def test_two_identical_races_dedup_to_one(self):
        result = self.mod.parse_report(_CPYTHON_RACE + _CPYTHON_RACE)
        self.assertEqual(result["total_warnings"], 2)
        self.assertEqual(result["unique_races"], 1)
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["frequency"], 2)

    # --- the key inversion: CPython frame = target, scaffolding = noise ----

    def test_cpython_frame_race_is_target(self):
        result = self.mod.parse_report(_CPYTHON_RACE)
        finding = result["findings"][0]
        self.assertTrue(finding["is_cpython_race"])
        self.assertFalse(finding["is_noise"])
        self.assertEqual(result["cpython_races"], 1)
        self.assertEqual(result["noise_races"], 0)
        self.assertEqual(result["summary"]["actionable"], 1)

    def test_scaffolding_only_race_is_noise(self):
        result = self.mod.parse_report(_NOISE_RACE)
        finding = result["findings"][0]
        self.assertFalse(finding["is_cpython_race"])
        self.assertTrue(finding["is_noise"])
        self.assertEqual(result["cpython_races"], 0)
        self.assertEqual(result["noise_races"], 1)

    def test_test_harness_module_race_is_noise(self):
        # A frame in Modules/_testcapimodule.c is under Modules/ but is
        # test-harness scaffolding, so noise must win over the CPython-source
        # path match.
        result = self.mod.parse_report(_TESTHARNESS_RACE)
        finding = result["findings"][0]
        self.assertTrue(finding["is_noise"])
        self.assertFalse(finding["is_cpython_race"])

    def test_mixed_report_separates_target_from_noise(self):
        result = self.mod.parse_report(
            _CPYTHON_RACE + _CPYTHON_RACE + _NOISE_RACE + _TESTHARNESS_RACE
        )
        self.assertEqual(result["total_warnings"], 4)
        self.assertEqual(result["unique_races"], 3)
        self.assertEqual(result["cpython_races"], 1)
        self.assertEqual(result["noise_races"], 2)

    # --- classification ----------------------------------------------------

    def test_write_read_race_is_high(self):
        result = self.mod.parse_report(_CPYTHON_RACE)
        finding = result["findings"][0]
        self.assertEqual(finding["classification"], "RACE")
        self.assertEqual(finding["severity"], "HIGH")

    def test_global_variable_race_is_critical(self):
        global_race = _CPYTHON_RACE.replace(
            "Location is heap block", "Location is global 'interp_state'"
        )
        result = self.mod.parse_report(global_race)
        self.assertEqual(result["findings"][0]["severity"], "CRITICAL")

    # --- envelope / summary shape ------------------------------------------

    def test_envelope_shape(self):
        result = self.mod.parse_report(_CPYTHON_RACE)
        for key in (
            "total_warnings",
            "unique_races",
            "cpython_races",
            "noise_races",
            "findings",
            "summary",
            "findings_repo",
        ):
            self.assertIn(key, result)
        self.assertEqual(result["findings_repo"], "cpython-tsan-findings")
        summary = result["summary"]
        for key in ("total_findings", "by_classification", "by_severity", "actionable"):
            self.assertIn(key, summary)

    def test_empty_report_is_clean(self):
        result = self.mod.parse_report("no thread sanitizer output here\n")
        self.assertEqual(result["total_warnings"], 0)
        self.assertEqual(result["unique_races"], 0)
        self.assertEqual(result["findings"], [])

    def test_analyze_reads_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tsan.txt"
            p.write_text(_CPYTHON_RACE, encoding="utf-8")
            result = self.mod.analyze(str(p))
        self.assertIn("report_path", result)
        self.assertEqual(result["cpython_races"], 1)

    def test_analyze_missing_file_errors(self):
        result = self.mod.analyze("/nonexistent/tsan_report_xyz.txt")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
