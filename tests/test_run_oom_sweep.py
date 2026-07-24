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

        def fake_run_one(python, payload, n, *, timeout=30.0, setup=""):
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

        def fake_run_one(python, payload, n, *, timeout=30.0, setup=""):
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
        script = self.mod._HARNESS_TEMPLATE.format(payload="x = 1", start=7, setup="")
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
        script = self.mod._HARNESS_TEMPLATE.format(payload=payload, start=0, setup="")
        self.assertIn(repr(payload), script)
        compile(script, "<harness>", "exec")


class TestSetupPhase(unittest.TestCase):
    """Setup must run *before* set_nomemory, or it burns the budget.

    Verified against the real regression case: with the setup inlined in the
    payload the odictiter_new sweep reports {memory_error: 25} over 0..25 (the
    SAFE reading); with it in --setup the same range aborts at K=2 with
    ``_PyObject_GC_UNTRACK: Assertion "_PyObject_GC_IS_TRACKED" failed ...
    odict_iterator``.
    """

    def setUp(self):
        self.mod = import_script("run_oom_sweep")

    def test_setup_runs_before_arming(self):
        script = self.mod._HARNESS_TEMPLATE.format(
            payload="iter(od.items())",
            start=3,
            setup="from collections import OrderedDict; od = OrderedDict(a=1)",
        )
        self.assertLess(script.index("exec(_SETUP_CODE"), script.index("set_nomemory"))
        compile(script, "<harness>", "exec")

    def test_compilation_happens_before_arming(self):
        script = self.mod._HARNESS_TEMPLATE.format(payload="x = 1", start=0, setup="")
        self.assertLess(
            script.index('compile(_PAYLOAD, "<oom-payload>"'),
            script.index("set_nomemory"),
        )

    def test_setup_and_payload_share_a_namespace(self):
        script = self.mod._HARNESS_TEMPLATE.format(
            payload="use(x)", start=0, setup="x = 1"
        )
        self.assertIn("exec(_SETUP_CODE, _NS)", script)
        self.assertIn("exec(_PAYLOAD_CODE, _NS)", script)

    def test_sweep_threads_setup_through_to_run_one(self):
        seen = {}

        def fake_run_one(python, payload, n, *, timeout=30.0, setup=""):
            seen["setup"] = setup
            return {"n": n, "outcome": "completed", "returncode": 0, "stderr": ""}

        self.mod.check_interpreter = lambda python: None
        self.mod.run_one = fake_run_one
        result = self.mod.sweep("py", "pass", max_n=1, setup="import gc")
        self.assertEqual(seen["setup"], "import gc")
        self.assertEqual(result["setup"], "import gc")


class TestSanitizerClassification(unittest.TestCase):
    """ASan exits 1 — the same code the harness uses for a clean MemoryError.

    Verified live: `./python -c "import ctypes; ctypes.string_at(1, 4)"` on the
    in-tree ASan build prints `ERROR: AddressSanitizer: SEGV` and exits 1.
    """

    def setUp(self):
        self.mod = import_script("run_oom_sweep")

    ASAN_REPORT = (
        "AddressSanitizer:DEADLYSIGNAL\n"
        "=================================================================\n"
        "==3777302==ERROR: AddressSanitizer: SEGV on unknown address 0x01\n"
        "==3777302==The signal is caused by a READ memory access.\n"
    )

    def test_asan_exit_1_is_a_crash_not_a_memory_error(self):
        self.assertEqual(self.mod.classify(1, self.ASAN_REPORT), "sanitizer_error")
        self.assertTrue(self.mod.is_crash("sanitizer_error"))

    def test_plain_exit_1_is_still_a_memory_error(self):
        self.assertEqual(self.mod.classify(1, "MemoryError\n"), "memory_error")
        self.assertEqual(self.mod.classify(1), "memory_error")

    def test_tsan_and_ubsan_reports_are_crashes(self):
        self.assertEqual(
            self.mod.classify(1, "==1==ERROR: ThreadSanitizer: data race\n"),
            "sanitizer_error",
        )
        self.assertEqual(
            self.mod.classify(
                1, "obj.c:12:5: runtime error: signed integer overflow\n"
            ),
            "sanitizer_error",
        )

    def test_leak_report_is_neither_a_crash_nor_a_memory_error(self):
        """OOM injection strands allocations by construction."""
        stderr = "==1==ERROR: LeakSanitizer: detected memory leaks\n"
        self.assertEqual(self.mod.classify(1, stderr), "sanitizer_leak")
        self.assertFalse(self.mod.is_crash("sanitizer_leak"))

    def test_sanitizer_report_wins_over_a_zero_exit(self):
        self.assertEqual(self.mod.classify(0, self.ASAN_REPORT), "sanitizer_error")

    def test_signal_outcomes_are_unchanged(self):
        self.assertEqual(self.mod.classify(-6, ""), "abort")
        self.assertEqual(self.mod.classify(139, ""), "segv")
        self.assertEqual(self.mod.classify(0, ""), "completed")


if __name__ == "__main__":
    unittest.main()
