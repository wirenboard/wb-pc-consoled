import os
import subprocess
import sys
import tempfile
import time
import unittest

from tests.common import ROOT, load

CLI = load("wb_pc_console_cli", "wb-pc-console")


class TestUnescape(unittest.TestCase):
    def test_table(self):
        cases = {
            "abc": b"abc",
            r"\n": b"\n",
            r"\r\t": b"\r\t",
            r"\e[A": b"\x1b[A",
            r"\x1b[B": b"\x1b[B",
            r"\\": b"\\",
            r"\x00": b"\x00",
            "тест": "тест".encode("utf-8"),
        }
        for src, want in cases.items():
            self.assertEqual(CLI.unescape(src), want, "unescape(%r)" % src)

    def test_invalid_hex_falls_through(self):
        self.assertEqual(CLI.unescape(r"\xzz"), b"\\xzz")

    def test_trailing_backslash(self):
        self.assertEqual(CLI.unescape("a\\"), b"a\\")

    def test_short_hex_at_end(self):
        # \x4 at end of string: too short for a hex pair, kept literal
        self.assertEqual(CLI.unescape(r"a\x4"), b"a\\x4")


class TestKeyBytes(unittest.TestCase):
    def test_named_keys(self):
        for name, want in CLI.KEYS.items():
            self.assertEqual(CLI.key_bytes(name), want)
        self.assertEqual(CLI.key_bytes("ENTER"), b"\r")

    def test_ctrl(self):
        self.assertEqual(CLI.key_bytes("ctrl-a"), b"\x01")
        self.assertEqual(CLI.key_bytes("ctrl-c"), b"\x03")
        self.assertEqual(CLI.key_bytes("ctrl-z"), b"\x1a")
        self.assertEqual(CLI.key_bytes("ctrl-]"), b"\x1d")
        self.assertEqual(CLI.key_bytes("ctrl-@"), b"\x00")

    def test_unknown(self):
        self.assertIsNone(CLI.key_bytes("bogus"))
        self.assertIsNone(CLI.key_bytes("ctrl-42"))


class TestEventRe(unittest.TestCase):
    def test_daemon_events_are_skipped(self):
        ts = "[2026-07-06T17:31:02.123456789+0300] "
        self.assertTrue(CLI.EVENT_RE.match(ts + "=== wb-pc-console: TX 5B: b'date\\n'"))
        self.assertTrue(CLI.EVENT_RE.match(ts + "=== wb-pc-console: usb reset done: x (y)"))
        self.assertFalse(CLI.EVENT_RE.match(ts + "date output from the board"))
        self.assertFalse(CLI.EVENT_RE.match(ts + "==== MARK: user marker"))


class TestFollowLines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ts.log")

    def tearDown(self):
        self.tmp.cleanup()

    def test_yields_appended_lines_from_offset(self):
        with open(self.path, "w") as f:
            f.write("old\n")
        offset = os.path.getsize(self.path)
        gen = CLI.follow_lines(self.path, offset, time.monotonic() + 3)
        with open(self.path, "a") as f:
            f.write("a\nb\n")
        self.assertEqual(next(gen), "a")
        self.assertEqual(next(gen), "b")
        with open(self.path, "a") as f:
            f.write("partial")
        with open(self.path, "a") as f:
            f.write(" line\n")
        self.assertEqual(next(gen), "partial line")

    def test_rotation_reopens_from_zero(self):
        with open(self.path, "w") as f:
            f.write("one\n")
        gen = CLI.follow_lines(self.path, 0, time.monotonic() + 5)
        self.assertEqual(next(gen), "one")
        os.replace(self.path, self.path + ".1")
        with open(self.path, "w") as f:
            f.write("two\n")
        self.assertEqual(next(gen), "two")

    def test_truncation_reopens_from_zero(self):
        with open(self.path, "w") as f:
            f.write("aaaa\n")
        gen = CLI.follow_lines(self.path, 0, time.monotonic() + 5)
        self.assertEqual(next(gen), "aaaa")
        with open(self.path, "w") as f:
            f.write("bb\n")
        self.assertEqual(next(gen), "bb")

    def test_deadline_ends_generator(self):
        with open(self.path, "w") as f:
            f.write("x\n")
        t0 = time.monotonic()
        lines = list(CLI.follow_lines(self.path, 0, t0 + 0.3))
        self.assertEqual(lines, ["x"])
        self.assertLess(time.monotonic() - t0, 2.0)


class TestCliSmoke(unittest.TestCase):
    def test_help_exits_zero(self):
        for script in ("wb-pc-console", "wb-pc-consoled"):
            r = subprocess.run([sys.executable, os.path.join(ROOT, script), "--help"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(r.returncode, 0, script)
            self.assertIn(b"wb-pc-console", r.stdout)

    def test_files_subcommand(self):
        env = dict(os.environ, WB_PC_CONSOLE_BASE_DIR="/nonexistent-base")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "wb-pc-console"),
                            "files", "X"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"/nonexistent-base/X/raw.log", r.stdout)


if __name__ == "__main__":
    unittest.main()
