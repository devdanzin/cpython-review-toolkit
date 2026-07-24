"""Tests for run_oom_sweep.py — the dense OOM-injection reproducer harness.

These tests must run anywhere, so they do not require a CPython source build.
The classification/aggregation logic is tested directly, and the subprocess
layer is exercised with a stubbed ``run_one``.
"""

import unittest

from helpers import import_script


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("run_oom_sweep")

    def test_signal_exit_codes(self):
        # Both the shell-style (128+n) and negative-signal spellings.
        self.assertEqual(self.mod.classify(139), "segv")
        self.assertEqual(self.mod.classify(-11), "segv")
        self.assertEqual(self.mod.classify(134), "abort")
        self.assertEqual(self.mod.classify(-6), "abort")

    def test_clean_outcomes(self):
        self.assertEqual(self.mod.classify(0), "completed")
        self.assertEqual(self.mod.classify(1), "memory_error")
        self.assertEqual(self.mod.classify(2), "other_exception")

    def test_other_signal_and_exit(self):
        self.assertEqual(self.mod.classify(-9), "signal_9")
        self.assertEqual(self.mod.classify(42), "exit_42")

    def test_is_crash(self):
        self.assertTrue(self.mod.is_crash("segv"))
        self.assertTrue(self.mod.is_crash("abort"))
        self.assertTrue(self.mod.is_crash("signal_9"))
        # A handled MemoryError is the SAFE outcome, not a crash.
        self.assertFalse(self.mod.is_crash("memory_error"))
        self.assertFalse(self.mod.is_crash("completed"))
        self.assertFalse(self.mod.is_crash("timeout"))


class TestSweep(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("run_oom_sweep")
        # Neutralize the interpreter probe; these tests stub run_one.
        self.mod.check_interpreter = lambda python: None

    def _stub(self, outcomes):
        """Make run_one return a canned outcome per index."""

        def fake_run_one(python, payload, n, *, timeout=30.0):
            outcome = outcomes.get(n, "memory_error")
            return {
                "n": n,
                "outcome": outcome,
                "returncode": 139 if outcome == "segv" else 1,
                "stderr": "",
            }

        self.mod.run_one = fake_run_one

    def test_no_crash_reports_safe(self):
        self._stub({})
        r = self.mod.sweep("py", "pass", max_n=5)
        self.assertFalse(r["reproduced"])
        self.assertEqual(r["summary"]["total_crashes"], 0)
        self.assertEqual(r["outcome_counts"]["memory_error"], 5)
        self.assertIsNone(r["first_crash"])

    def test_crash_is_reported_with_index(self):
        self._stub({3: "segv"})
        r = self.mod.sweep("py", "pass", max_n=6)
        self.assertTrue(r["reproduced"])
        self.assertEqual(r["summary"]["crash_indices"], [3])
        self.assertEqual(r["first_crash"]["n"], 3)
        self.assertIn("REPRODUCED", r["summary"]["verdict"])

    def test_dense_sweep_covers_every_index(self):
        seen = []

        def fake_run_one(python, payload, n, *, timeout=30.0):
            seen.append(n)
            return {"n": n, "outcome": "memory_error", "returncode": 1, "stderr": ""}

        self.mod.run_one = fake_run_one
        self.mod.sweep("py", "pass", max_n=8)
        # Dense, not sampled — a crash window can be one allocation wide.
        self.assertEqual(seen, list(range(8)))

    def test_stop_after_halts_early(self):
        self._stub({1: "segv", 2: "segv", 3: "segv"})
        r = self.mod.sweep("py", "pass", max_n=10, stop_after=2)
        self.assertEqual(r["summary"]["total_crashes"], 2)
        # Stopped right after the second crash rather than finishing the range.
        self.assertLess(r["iterations_run"], 10)

    def test_start_n_offsets_the_range(self):
        self._stub({})
        r = self.mod.sweep("py", "pass", start_n=5, max_n=9)
        self.assertEqual(r["range"], {"start": 5, "stop": 9})
        self.assertEqual(r["iterations_run"], 4)

    def test_interpreter_guard_short_circuits(self):
        self.mod.check_interpreter = lambda python: "no _testcapi"
        r = self.mod.sweep("py", "pass", max_n=5)
        self.assertIn("error", r)
        self.assertNotIn("crashes", r)


class TestHarnessTemplate(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("run_oom_sweep")

    def test_template_arms_and_disarms(self):
        script = self.mod._HARNESS_TEMPLATE.format(payload="x = 1", start=7)
        # Arms the counting allocator at the requested index.
        self.assertIn("set_nomemory(7)", script)
        # Always removes the hooks so teardown itself cannot fault.
        self.assertIn("remove_mem_hooks", script)
        # faulthandler is enabled before arming.
        self.assertLess(
            script.index("faulthandler.enable"), script.index("set_nomemory")
        )
        # A handled MemoryError exits 1 (the SAFE outcome), not 0.
        self.assertIn("sys.exit(1)", script)

    def test_payload_is_embedded_safely(self):
        # Quotes/newlines in the payload must not break the wrapper.
        payload = 'x = "quoted\'s"\ny = 2\n'
        script = self.mod._HARNESS_TEMPLATE.format(payload=payload, start=0)
        self.assertIn(repr(payload), script)
        compile(script, "<harness>", "exec")


if __name__ == "__main__":
    unittest.main()
