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
        # Neutralize the interpreter probe and the unarmed rehearsal; these
        # tests stub run_one and never touch a real interpreter.
        self.mod.check_interpreter = lambda python: None
        self.mod.dry_run = lambda *a, **k: {
            "ok": True,
            "phase": None,
            "returncode": 0,
            "stderr": "",
        }

    def _stub(self, outcomes):
        """Make run_one return a canned outcome per index."""

        def fake_run_one(python, payload, n, *, timeout=30.0, setup="", width=1):
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

        def fake_run_one(python, payload, n, *, timeout=30.0, setup="", width=1):
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
        script = self.mod.build_child_script("x = 1", 7, setup="")
        # Arms the counting allocator at the requested index, BOUNDED to one
        # allocation. An unbounded set_nomemory(7) fails allocation 7 and every
        # one after it, so any multi-allocation payload dies at the first index
        # reached -- a false positive that reads exactly like a crash.
        self.assertIn("set_nomemory(7, 8)", script)
        # Always removes the hooks so teardown itself cannot fault.
        self.assertIn("remove_mem_hooks", script)

    def test_width_widens_the_failure_window(self):
        script = self.mod.build_child_script("x = 1", 10, setup="", width=5)
        self.assertIn("set_nomemory(10, 15)", script)

    def test_width_zero_restores_the_unbounded_form(self):
        """Kept for the rare case where a sustained famine is the thing tested."""
        script = self.mod.build_child_script("x = 1", 10, setup="", width=0)
        self.assertIn("set_nomemory(10)", script)
        # faulthandler is enabled before arming.
        self.assertLess(
            script.index("faulthandler.enable"), script.index("set_nomemory")
        )
        # A handled MemoryError exits 1 (the SAFE outcome), not 0.
        self.assertIn("sys.exit(1)", script)

    def test_payload_is_embedded_safely(self):
        # Quotes/newlines in the payload must not break the wrapper.
        payload = 'x = "quoted\'s"\ny = 2\n'
        script = self.mod.build_child_script(payload, 0, setup="")
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
        script = self.mod.build_child_script(
            "iter(od.items())",
            3,
            setup="from collections import OrderedDict; od = OrderedDict(a=1)",
        )
        self.assertLess(script.index("exec(_SETUP_CODE"), script.index("set_nomemory"))
        compile(script, "<harness>", "exec")

    def test_compilation_happens_before_arming(self):
        script = self.mod.build_child_script("x = 1", 0, setup="")
        self.assertLess(
            script.index('compile(_PAYLOAD, "<oom-payload>"'),
            script.index("set_nomemory"),
        )

    def test_setup_and_payload_share_a_namespace(self):
        script = self.mod.build_child_script("use(x)", 0, setup="x = 1")
        self.assertIn("exec(_SETUP_CODE, _NS)", script)
        self.assertIn("exec(_PAYLOAD_CODE, _NS)", script)

    def test_sweep_threads_setup_through_to_run_one(self):
        seen = {}

        def fake_run_one(python, payload, n, *, timeout=30.0, setup="", width=1):
            seen["setup"] = setup
            return {"n": n, "outcome": "completed", "returncode": 0, "stderr": ""}

        self.mod.check_interpreter = lambda python: None
        self.mod.dry_run = lambda *a, **k: {
            "ok": True,
            "phase": None,
            "returncode": 0,
            "stderr": "",
        }
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


class TestSetupErrorIsNotSafe(unittest.TestCase):
    """D-14: a raising setup used to score a perfect clean sweep.

    The child ran ``exec(_SETUP_CODE, _NS)`` outside any ``try``. A raising
    setup exited 1 — the harness's clean-``MemoryError`` code — so every index
    was reported as the SAFE outcome. Measured on ``Objects/typeobject.c``
    pass 2: four sweeps reporting 400/400 ``memory_error`` became 13 aborts and
    2 SIGSEGVs after one bad line was deleted from the setup.
    """

    def setUp(self):
        self.mod = import_script("run_oom_sweep")

    def test_exit_3_is_setup_error_not_memory_error(self):
        self.assertEqual(self.mod.classify(3), "setup_error")
        self.assertNotEqual(self.mod.classify(3), "memory_error")

    def test_setup_error_is_neither_a_crash_nor_safe(self):
        self.assertFalse(self.mod.is_crash("setup_error"))
        self.assertTrue(self.mod.is_harness_error("setup_error"))
        # The two safe/void categories must not be confusable.
        self.assertFalse(self.mod.is_harness_error("memory_error"))
        self.assertFalse(self.mod.is_harness_error("segv"))

    def test_child_guards_the_setup_exec(self):
        script = self.mod.build_child_script("x = 1", 0, setup="raise ValueError")
        # The setup exec is inside a try that exits 3, and it still runs before
        # arming (the two obligations are independent and both must hold).
        self.assertIn("sys.exit(3)", script)
        self.assertLess(script.index("exec(_SETUP_CODE"), script.index("set_nomemory"))
        compile(script, "<harness>", "exec")

    def test_child_guards_compilation_too(self):
        """A payload that does not compile is a harness error, not a result."""
        script = self.mod.build_child_script("this is not python", 0, setup="")
        head = script[: script.index("set_nomemory")]
        self.assertIn("sys.exit(3)", head)

    def test_a_raising_setup_really_exits_3(self):
        """End-to-end on the running interpreter, minus the _testcapi arming."""
        import subprocess
        import sys as _sys

        script = self.mod.build_dry_run_script(
            "x = 1", setup="raise ValueError('boom')"
        )
        proc = subprocess.run(
            [_sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.mod.classify(proc.returncode), "setup_error")
        self.assertIn("SETUP FAILED", proc.stderr)

    def test_sweep_aborts_on_a_setup_error_instead_of_counting_it(self):
        def fake_run_one(python, payload, n, *, timeout=30.0, setup="", width=1):
            return {"n": n, "outcome": "setup_error", "returncode": 3, "stderr": "boom"}

        self.mod.check_interpreter = lambda python: None
        self.mod.dry_run = lambda *a, **k: {
            "ok": True,
            "phase": None,
            "returncode": 0,
            "stderr": "",
        }
        self.mod.run_one = fake_run_one
        r = self.mod.sweep("py", "pass", max_n=40)
        self.assertIn("error", r)
        self.assertIn("setup", r["error"])
        # Crucially: no clean bill of health is emitted.
        self.assertNotIn("summary", r)
        self.assertNotIn("reproduced", r)


class TestPayloadExceptionIsNamed(unittest.TestCase):
    """An allocation failure surfacing as a NON-MemoryError is the finding.

    P2-F2 (`Objects/typeobject.c:6714`) converts every failed type-dict
    insertion into an unnarrowed `PyErr_Format(AttributeError, ...)`, so a
    MemoryError is replaced by a nonsensical AttributeError. The sweep's only
    trace of that is `other_exception`, which does not say which exception —
    so the child names it.
    """

    def setUp(self):
        self.mod = import_script("run_oom_sweep")

    def test_child_names_the_exception(self):
        script = self.mod.build_child_script("raise ValueError('x')", 0, setup="")
        self.assertIn("OOM-SWEEP-EXC", script)

    def test_extractor_lifts_the_line(self):
        stderr = (
            "some noise\n"
            "OOM-SWEEP-EXC: AttributeError: type object 'T' has no attribute 'a15'"
            " | __context__=None\n"
        )
        got = self.mod.extract_payload_exception(stderr)
        self.assertEqual(
            got,
            "AttributeError: type object 'T' has no attribute 'a15' | __context__=None",
        )

    def test_extractor_is_quiet_when_absent(self):
        self.assertIsNone(self.mod.extract_payload_exception(""))
        self.assertIsNone(self.mod.extract_payload_exception("MemoryError\n"))

    def test_sweep_collects_payload_exceptions(self):
        def fake_run_one(python, payload, n, *, timeout=30.0, setup="", width=1):
            r = {"n": n, "outcome": "memory_error", "returncode": 1, "stderr": ""}
            if n == 2:
                r["outcome"] = "other_exception"
                r["returncode"] = 2
                r["exception"] = "AttributeError: nope | __context__=None"
            return r

        self.mod.check_interpreter = lambda python: None
        self.mod.dry_run = lambda *a, **k: {
            "ok": True,
            "phase": None,
            "returncode": 0,
            "stderr": "",
        }
        self.mod.run_one = fake_run_one
        r = self.mod.sweep("py", "pass", max_n=5)
        self.assertEqual(
            r["payload_exceptions"],
            [{"n": 2, "exception": "AttributeError: nope | __context__=None"}],
        )
        # Still not a crash: the clobbered exception is a correctness finding,
        # not an interpreter fault.
        self.assertFalse(r["reproduced"])


class TestDryRun(unittest.TestCase):
    """``sweep()`` rehearses setup+payload unarmed before it believes anything."""

    def setUp(self):
        self.mod = import_script("run_oom_sweep")
        self.mod.check_interpreter = lambda python: None

    def test_dry_run_script_never_arms_the_allocator(self):
        script = self.mod.build_dry_run_script("x = 1", setup="y = 2")
        self.assertNotIn("set_nomemory", script)
        self.assertNotIn("_testcapi", script)
        compile(script, "<dry-run>", "exec")

    def test_dry_run_distinguishes_setup_from_payload(self):
        import subprocess
        import sys as _sys

        cases = [
            ("raise ValueError", "x = 1", 3, "setup"),
            ("y = 1", "raise ValueError", 4, "payload"),
            ("y = 1", "x = y + 1", 0, None),
        ]
        for setup, payload, code, phase in cases:
            with self.subTest(setup=setup, payload=payload):
                script = self.mod.build_dry_run_script(payload, setup=setup)
                proc = subprocess.run(
                    [_sys.executable, "-c", script],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, code)
                self.assertEqual({3: "setup", 4: "payload"}.get(code), phase)

    def test_failed_dry_run_blocks_the_sweep(self):
        swept = []

        def fake_run_one(python, payload, n, *, timeout=30.0, setup="", width=1):
            swept.append(n)
            return {"n": n, "outcome": "memory_error", "returncode": 1, "stderr": ""}

        self.mod.run_one = fake_run_one
        self.mod.dry_run = lambda *a, **k: {
            "ok": False,
            "phase": "setup",
            "returncode": 3,
            "stderr": "NameError",
        }
        r = self.mod.sweep("py", "pass", max_n=50, setup="broken")
        self.assertIn("error", r)
        self.assertIn("setup", r["error"])
        self.assertEqual(swept, [], "the sweep must not run at all")
        self.assertFalse(r["dry_run"]["ok"])

    def test_passing_dry_run_is_recorded_on_the_result(self):
        self.mod.run_one = lambda python, payload, n, **k: {
            "n": n,
            "outcome": "memory_error",
            "returncode": 1,
            "stderr": "",
        }
        self.mod.dry_run = lambda *a, **k: {
            "ok": True,
            "phase": None,
            "returncode": 0,
            "stderr": "",
        }
        # Enough real failure points to clear THIN_EVIDENCE_POINTS, so this
        # test measures the dry-run gate and not the thin-evidence gate.
        r = self.mod.sweep("py", "pass", max_n=self.mod.THIN_EVIDENCE_POINTS + 5)
        # A clean sweep is only meaningful alongside proof the payload ran.
        self.assertTrue(r["dry_run"]["ok"])
        self.assertIn("handled cleanly", r["summary"]["verdict"])
        self.assertFalse(r["summary"]["thin_evidence"])

    def test_skipping_the_dry_run_marks_the_clean_verdict_unverified(self):
        self.mod.run_one = lambda python, payload, n, **k: {
            "n": n,
            "outcome": "memory_error",
            "returncode": 1,
            "stderr": "",
        }
        r = self.mod.sweep("py", "pass", max_n=3, run_dry_run=False)
        self.assertIsNone(r["dry_run"])
        self.assertIn("UNVERIFIED", r["summary"]["verdict"])

    def test_dry_run_reports_an_interpreter_level_death(self):
        """A payload that segfaults with no injection is not an OOM finding."""
        import subprocess

        class _Proc:
            returncode = -11
            stdout = ""
            stderr = "Segmentation fault"

        real_run = subprocess.run
        try:
            subprocess.run = lambda *a, **k: _Proc()
            d = self.mod.dry_run("py", "boom()", setup="")
        finally:
            subprocess.run = real_run
        self.assertFalse(d["ok"])
        self.assertEqual(d["phase"], "interpreter")


class TestThinEvidenceDenominator(unittest.TestCase):
    """D-17: a clean sweep's denominator is failure points, not iterations.

    Every index past the payload's last allocation returns ``completed`` and
    exercises nothing. Measured on obj-typeobject pass 2: three regions were
    certified clean over 4, 12 and 5 real failure points while printing a
    220-iteration clean verdict, because the setup warmed the paths under test.
    """

    def setUp(self):
        self.mod = import_script("run_oom_sweep")
        self.mod.check_interpreter = lambda python: None
        self.mod.dry_run = lambda *a, **k: {
            "ok": True,
            "phase": None,
            "returncode": 0,
            "stderr": "",
        }

    def _stub_failing_below(self, cutoff):
        """Allocation failures below ``cutoff``; ``completed`` at and above it.

        This is the real shape: a payload with N allocations fails for n < N
        and completes for every larger index.
        """

        def fake_run_one(python, payload, n, *, timeout=30.0, setup="", width=1):
            outcome = "memory_error" if n < cutoff else "completed"
            return {"n": n, "outcome": outcome, "returncode": 1, "stderr": ""}

        self.mod.run_one = fake_run_one

    def test_completed_indices_are_excluded_from_the_denominator(self):
        self._stub_failing_below(4)
        r = self.mod.sweep("py", "pass", max_n=220)
        self.assertEqual(r["iterations_run"], 220)
        # The number that matters: 4, not 220.
        self.assertEqual(r["allocation_failure_points"], 4)
        self.assertEqual(r["summary"]["allocation_failure_points"], 4)

    def test_a_thin_clean_sweep_refuses_to_say_handled_cleanly(self):
        self._stub_failing_below(4)
        r = self.mod.sweep("py", "pass", max_n=220)
        self.assertFalse(r["reproduced"])
        self.assertTrue(r["summary"]["thin_evidence"])
        verdict = r["summary"]["verdict"]
        self.assertIn("TOO THIN", verdict)
        self.assertIn("N=4", verdict)
        # The old phrasing certified this as clean; it must not reappear.
        self.assertNotIn("handled cleanly", verdict)

    def test_a_thick_clean_sweep_still_certifies(self):
        self._stub_failing_below(self.mod.THIN_EVIDENCE_POINTS + 3)
        r = self.mod.sweep("py", "pass", max_n=220)
        self.assertFalse(r["summary"]["thin_evidence"])
        self.assertIn("handled cleanly", r["summary"]["verdict"])
        self.assertIn(f"N={self.mod.THIN_EVIDENCE_POINTS + 3}", r["summary"]["verdict"])

    def test_thin_evidence_never_masks_a_real_crash(self):
        """A crash at a thin denominator is still REPRODUCED, not downgraded."""

        def fake_run_one(python, payload, n, *, timeout=30.0, setup="", width=1):
            if n == 2:
                return {"n": n, "outcome": "segv", "returncode": 139, "stderr": ""}
            outcome = "memory_error" if n < 3 else "completed"
            return {"n": n, "outcome": outcome, "returncode": 1, "stderr": ""}

        self.mod.run_one = fake_run_one
        r = self.mod.sweep("py", "pass", max_n=220)
        self.assertTrue(r["reproduced"])
        self.assertIn("REPRODUCED", r["summary"]["verdict"])
        self.assertLess(r["allocation_failure_points"], self.mod.THIN_EVIDENCE_POINTS)

    def test_an_all_completed_sweep_has_a_zero_denominator(self):
        """The degenerate case: the payload never allocates at all."""
        self._stub_failing_below(0)
        r = self.mod.sweep("py", "pass", max_n=50)
        self.assertEqual(r["allocation_failure_points"], 0)
        self.assertTrue(r["summary"]["thin_evidence"])
        self.assertIn("N=0", r["summary"]["verdict"])


if __name__ == "__main__":
    unittest.main()
