"""Tests for scan_gil_usage.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("scan_gil_usage")


class TestGilDetection(unittest.TestCase):
    """Test GIL discipline issue detection."""

    def test_detects_mismatched_threads(self):
        c_code = (
            "static int\n"
            "bad_threads(int fd)\n"
            "{\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            "    read(fd, buf, n);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            mismatched = [
                f for f in result["findings"] if f["type"] == "mismatched_allow_threads"
            ]
            self.assertGreater(len(mismatched), 0)

    def test_balanced_threads_no_finding(self):
        c_code = (
            "static int\n"
            "good_threads(int fd)\n"
            "{\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            "    read(fd, buf, n);\n"
            "    Py_END_ALLOW_THREADS\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            mismatched = [
                f for f in result["findings"] if f["type"] == "mismatched_allow_threads"
            ]
            self.assertEqual(len(mismatched), 0)

    def test_detects_api_without_gil(self):
        c_code = (
            "static int\n"
            "api_no_gil(PyObject *self)\n"
            "{\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            '    PyObject_CallMethod(self, "method", NULL);\n'
            "    Py_END_ALLOW_THREADS\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            api_findings = [
                f for f in result["findings"] if f["type"] == "api_without_gil"
            ]
            self.assertGreater(len(api_findings), 0)

    def test_detects_blocking_with_gil(self):
        c_code = (
            "static int\n"
            "blocking_gil(int fd)\n"
            "{\n"
            "    char buf[1024];\n"
            "    read(fd, buf, sizeof(buf));\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            blocking = [
                f for f in result["findings"] if f["type"] == "blocking_with_gil"
            ]
            self.assertGreater(len(blocking), 0)

    def test_blocking_in_released_region_no_finding(self):
        c_code = (
            "static int\n"
            "good_blocking(int fd)\n"
            "{\n"
            "    char buf[1024];\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            "    read(fd, buf, sizeof(buf));\n"
            "    Py_END_ALLOW_THREADS\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            blocking = [
                f for f in result["findings"] if f["type"] == "blocking_with_gil"
            ]
            self.assertEqual(len(blocking), 0)


class TestAnalyze(unittest.TestCase):
    """Test full GIL analysis."""

    def test_summary_fields(self):
        with TempProject(
            {
                "Modules/test.c": ("static int\ntest(void)\n{\n    return 0;\n}\n"),
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertIn("summary", result)
            self.assertIn("mismatched_pairs", result["summary"])
            self.assertIn("api_without_gil", result["summary"])
            self.assertIn("blocking_with_gil", result["summary"])
            # A zero result needs a denominator: "no constructs present" and
            # "constructs present and clean" are different answers.
            self.assertIn("vocabulary_counts", result)


class TestSharedChassis(unittest.TestCase):
    """The move from the private regex function-finder to extract_functions()."""

    def test_multiline_signature_is_extracted(self):
        """The old chassis needed the whole prototype on the previous line."""
        c_code = (
            "static PyObject *\n"
            "sock_recv(PySocketSockObject *s,\n"
            "          char *buf,\n"
            "          Py_ssize_t len)\n"
            "{\n"
            "    read(s->fd, buf, len);\n"
            "    return NULL;\n"
            "}\n"
        )
        with TempProject({"Modules/socket.c": c_code}) as root:
            result = mod.analyze(str(root))
            blocking = [
                f for f in result["findings"] if f["type"] == "blocking_with_gil"
            ]
            self.assertEqual(len(blocking), 1)
            self.assertEqual(blocking[0]["function"], "sock_recv")
            self.assertEqual(blocking[0]["line"], 6)

    def test_line_number_survives_a_block_comment(self):
        """Collapsing a block comment used to shift every later finding up."""
        c_code = (
            "static int\n"
            "reader(int fd)\n"
            "{\n"
            "    /* A multi-line comment\n"
            "       that the old stripper\n"
            "       collapsed into one space,\n"
            "       shifting everything below. */\n"
            "    read(fd, buf, n);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            blocking = [
                f for f in result["findings"] if f["type"] == "blocking_with_gil"
            ]
            self.assertEqual(len(blocking), 1)
            self.assertEqual(blocking[0]["line"], 8)

    def test_char_literal_holding_a_quote_does_not_eat_the_file(self):
        """Modules/socketmodule.c contains '"'; masking must survive it."""
        source = "char q = '\"';\nstatic void f(void) { read(1, b, 2); }\n"
        masked = mod.strip_comments_and_strings(source)
        self.assertIn("read", masked)
        self.assertEqual(len(masked), len(source))

    def test_strip_preserves_length_and_lines(self):
        source = 'a\n/* x\ny */\n"str"\nb\n'
        masked = mod.strip_comments_and_strings(source)
        self.assertEqual(len(masked), len(source))
        self.assertEqual(masked.count("\n"), source.count("\n"))
        self.assertEqual(masked.split("\n")[4], "b")

    def test_blocking_call_in_a_docstring_is_not_a_call(self):
        c_code = (
            "static int\n"
            "documented(void)\n"
            "{\n"
            '    const char *doc = "call read() to fill the buffer";\n'
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["findings"], [])


class TestFalsePositiveGates(unittest.TestCase):
    def test_file_local_helper_is_not_a_python_api_call(self):
        """_PySSL_errno matches _Py[A-Z]\\w+ but is Modules/_ssl.c's own static."""
        c_code = (
            "static int\n"
            "_PySSL_errno(int failed, SSL *ssl, int retcode)\n"
            "{\n"
            "    return 0;\n"
            "}\n"
            "\n"
            "static PyObject *\n"
            "do_handshake(PySSLSocket *self)\n"
            "{\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            "    ret = SSL_do_handshake(self->ssl);\n"
            "    err = _PySSL_errno(ret < 1, self->ssl, ret);\n"
            "    Py_END_ALLOW_THREADS\n"
            "    return NULL;\n"
            "}\n"
        )
        with TempProject({"Modules/_ssl.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(
                [f for f in result["findings"] if f["type"] == "api_without_gil"], []
            )

    def test_real_api_call_in_released_region_is_still_flagged(self):
        c_code = (
            "static PyObject *\n"
            "do_work(PyObject *self)\n"
            "{\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            '    PyObject_CallMethod(self, "m", NULL);\n'
            "    Py_END_ALLOW_THREADS\n"
            "    return NULL;\n"
            "}\n"
        )
        with TempProject({"Modules/_ssl.c": c_code}) as root:
            result = mod.analyze(str(root))
            api = [f for f in result["findings"] if f["type"] == "api_without_gil"]
            self.assertEqual(len(api), 1)
            self.assertEqual(api[0]["api_call"], "PyObject_CallMethod")
            self.assertEqual(api[0]["line"], 5)

    def test_gilstate_release_on_each_exit_path_is_clean(self):
        """Modules/_ssl.c's four callbacks: Ensure once, Release per path."""
        c_code = (
            "static int\n"
            "_servername_callback(SSL *s, int *al, void *args)\n"
            "{\n"
            "    PyGILState_STATE gstate = PyGILState_Ensure();\n"
            "    if (sni_cb == NULL) {\n"
            "        PyGILState_Release(gstate);\n"
            "        return 0;\n"
            "    }\n"
            "    PyGILState_Release(gstate);\n"
            "    return 1;\n"
            "}\n"
        )
        with TempProject({"Modules/_ssl.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(
                [f for f in result["findings"] if f["type"] == "mismatched_gilstate"],
                [],
            )

    def test_gilstate_never_released_is_flagged(self):
        c_code = (
            "static int\n"
            "leaky_callback(void *args)\n"
            "{\n"
            "    PyGILState_STATE gstate = PyGILState_Ensure();\n"
            "    call_into_python();\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/_ssl.c": c_code}) as root:
            result = mod.analyze(str(root))
            mismatched = [
                f for f in result["findings"] if f["type"] == "mismatched_gilstate"
            ]
            self.assertEqual(len(mismatched), 1)
            self.assertEqual(mismatched[0]["line"], 4)

    def test_callback_whose_caller_releases_the_gil_is_not_flagged(self):
        """Modules/socketmodule.c: sock_call_ex() releases around the impl."""
        c_code = (
            "static int\n"
            "sock_recv_impl(PySocketSockObject *s, void *data)\n"
            "{\n"
            "    ctx->result = recv(s->sock_fd, ctx->cbuf, ctx->len, 0);\n"
            "    return 1;\n"
            "}\n"
            "\n"
            "static int\n"
            "sock_call_ex(PySocketSockObject *s, int w, int (*func)(void *),\n"
            "             void *data)\n"
            "{\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            "    res = func(data);\n"
            "    Py_END_ALLOW_THREADS\n"
            "    return res;\n"
            "}\n"
            "\n"
            "static PyObject *\n"
            "sock_recv(PySocketSockObject *s)\n"
            "{\n"
            "    if (sock_call_ex(s, 0, sock_recv_impl, &ctx) < 0)\n"
            "        return NULL;\n"
            "    return NULL;\n"
            "}\n"
        )
        with TempProject({"Modules/socket.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(
                [f for f in result["findings"] if f["type"] == "blocking_with_gil"], []
            )


if __name__ == "__main__":
    unittest.main()
