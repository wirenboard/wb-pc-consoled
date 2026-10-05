import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.common import ROOT, load

D = load("wb_pc_consoled_daemon", "wb-pc-consoled")

NS = 1_700_000_000_123_456_789


def make_keeper(tmp, **env):
    saved = dict(os.environ)
    os.environ["WB_PC_CONSOLE_BASE_DIR"] = tmp
    os.environ.update(env)
    try:
        k = D.Keeper("TEST")
    finally:
        os.environ.clear()
        os.environ.update(saved)
    os.makedirs(k.dir, exist_ok=True)
    return k


def open_logs(k):
    k.raw_fd = os.open(k.raw_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    k.ts_f = open(k.ts_path, "a", buffering=1, errors="replace")


def ts_lines(k):
    with open(k.ts_path, errors="replace") as f:
        return f.read().splitlines()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.k = make_keeper(self.tmp.name)
        open_logs(self.k)

    def tearDown(self):
        try:
            if self.k.raw_fd is not None:
                os.close(self.k.raw_fd)
            if self.k.ts_f is not None:
                self.k.ts_f.close()
        except OSError:
            pass
        self.tmp.cleanup()


class TestFmtNs(unittest.TestCase):
    def test_shape_and_ns(self):
        s = D.fmt_ns(NS)
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.123456789[+-]\d{4}$")

    def test_zero_fraction_padded(self):
        s = D.fmt_ns(1_700_000_000_000_000_001)
        self.assertIn(".000000001", s)


class TestFeed(Base):
    def test_line_split_across_chunks_keeps_first_stamp(self):
        self.k.feed(b"foo", NS, 1.0)
        self.k.feed(b"bar\n", NS + 5_000_000_000, 6.0)
        lines = ts_lines(self.k)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "[%s] foobar" % D.fmt_ns(NS))

    def test_multiple_lines_in_one_chunk(self):
        self.k.feed(b"one\ntwo\n", NS, 1.0)
        lines = ts_lines(self.k)
        self.assertEqual(lines, ["[%s] one" % D.fmt_ns(NS),
                                 "[%s] two" % D.fmt_ns(NS)])

    def test_crlf_stripped(self):
        self.k.feed(b"hello\r\n", NS, 1.0)
        self.assertTrue(ts_lines(self.k)[0].endswith("] hello"))

    def test_invalid_utf8_replaced(self):
        self.k.feed(b"\xff\n", NS, 1.0)
        self.assertIn("�", ts_lines(self.k)[0])

    def test_partial_flush_preserves_stamp_and_text(self):
        self.k.feed(b"login: ", NS, 1.0)
        self.assertEqual(ts_lines(self.k), [])
        self.k.flush_partial()
        self.assertEqual(ts_lines(self.k)[0], "[%s] login: " % D.fmt_ns(NS))

    def test_raw_log_is_verbatim(self):
        payload = b"abc\r\n\xff partial"
        self.k.feed(payload, NS, 1.0)
        with open(self.k.raw_path, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_raw_write_failure_is_counted_not_raised(self):
        os.close(self.k.raw_fd)  # make the fd invalid behind the keeper's back
        self.k.feed(b"data\n", NS, 1.0)  # must not raise
        self.assertEqual(self.k.raw_dropped, 5)
        self.assertIsNone(self.k.raw_fd)
        # reopen path restores logging
        self.k._maybe_reopen_logs()
        self.assertIsNotNone(self.k.raw_fd)


class TestRotation(Base):
    def test_shift_generations(self):
        for suffix, content in (("", "cur"), (".1", "g1"), (".2", "g2")):
            with open(self.k.raw_path + suffix, "w") as f:
                f.write(content)
        self.k._shift(self.k.raw_path)
        self.assertFalse(os.path.exists(self.k.raw_path))
        for suffix, content in ((".1", "cur"), (".2", "g1"), (".3", "g2")):
            with open(self.k.raw_path + suffix) as f:
                self.assertEqual(f.read(), content)

    def test_maybe_rotate_swaps_files_and_keeps_writing(self):
        self.k.max_bytes = 100
        self.k.feed(b"x" * 150 + b"\n", NS, 1.0)
        self.k.emit(NS, b"y" * 150)
        self.k.maybe_rotate()
        self.assertTrue(os.path.exists(self.k.raw_path + ".1"))
        self.assertTrue(os.path.exists(self.k.ts_path + ".1"))
        self.assertLess(os.path.getsize(self.k.raw_path), 100)
        self.k.feed(b"after\n", NS, 2.0)  # both files still writable
        with open(self.k.raw_path, "rb") as f:
            self.assertEqual(f.read(), b"after\n")


class TestHandleCtl(Base):
    def test_command_split_across_reads(self):
        self.k.handle_ctl(b"mark he")
        self.k.handle_ctl(b"llo\n")
        self.assertTrue(ts_lines(self.k)[0].endswith("==== MARK: hello"))

    def test_unknown_command_logged(self):
        self.k.handle_ctl(b"bogus arg\n")
        self.assertIn("unknown ctl command", ts_lines(self.k)[0])

    def test_flush_command(self):
        self.k.feed(b"abc", NS, 1.0)
        self.k.handle_ctl(b"flush\n")
        self.assertTrue(ts_lines(self.k)[0].endswith("] abc"))

    def test_overflow_discarded(self):
        self.k.handle_ctl(b"x" * (D.CTL_CAP + 1))
        self.assertEqual(self.k.ctl_buf, b"")
        self.assertIn("ctl buffer overflow", "\n".join(ts_lines(self.k)))


class TestLineKick(Base):
    def setUp(self):
        super().setUp()
        self.master, self.slave = os.openpty()
        self.k.ser_fd = self.slave
        self.k.auto_kick = True
        self.k.last_rx_mono = 0.0
        self.calls = []
        self.real = D.termios.tcsetattr
        D.termios.tcsetattr = self.spy
        self.now = time.monotonic()

    def tearDown(self):
        D.termios.tcsetattr = self.real
        self.k.ser_fd = None
        os.close(self.master)
        os.close(self.slave)
        super().tearDown()

    def spy(self, fd, when, attrs):
        self.calls.append(bool(attrs[2] & D.termios.CSTOPB))
        return self.real(fd, when, attrs)

    def maybe_kick(self, now, stalled):
        with patch.object(D.time, "monotonic", return_value=now):
            self.k.maybe_kick(now, stalled)

    def test_kick_sets_cstopb_and_restores(self):
        before = D.termios.tcgetattr(self.slave)
        self.assertTrue(self.k.kick_line_coding())
        self.assertEqual(self.calls, [True, False])
        self.assertEqual(D.termios.tcgetattr(self.slave), before)
        self.assertEqual(self.k.kicks, 1)

    def test_kick_without_port(self):
        self.k.ser_fd = None
        self.assertFalse(self.k.kick_line_coding())
        self.k.handle_ctl(b"kick\n")
        self.maybe_kick(self.now, 9.0)
        self.assertEqual(self.calls, [])
        self.assertIn("kick: port not open", "\n".join(ts_lines(self.k)))

    def test_idle_kick_needs_rx_silence_and_quiet_tx(self):
        self.k.last_rx_mono = self.now - 5
        self.maybe_kick(self.now, 0.0)
        self.assertEqual(self.k.kicks, 0)          # RX too recent
        self.k.last_rx_mono = self.now - 11
        self.k.tx_busy_mono = self.now - 0.5
        self.maybe_kick(self.now, 0.0)
        self.assertEqual(self.k.kicks, 0)          # our TX too recent
        self.k.tx_busy_mono = self.now - 2         # typing does not postpone it
        self.maybe_kick(self.now, 0.0)
        self.assertEqual(self.k.kicks, 1)
        self.maybe_kick(time.monotonic(), 0.0)
        self.assertEqual(self.k.kicks, 1)          # next one a period later
        self.assertNotIn("line coding", "\n".join(ts_lines(self.k)))

    def test_idle_kick_disabled(self):
        self.k.idle_kick = 0
        self.maybe_kick(self.now, 0.0)
        self.k.idle_kick, self.k.auto_kick = 10.0, False
        self.maybe_kick(self.now, 9.0)           # no stall kick either
        self.assertEqual(self.calls, [])

    def test_stall_kick_waits_for_tx_to_drain(self):
        self.k.idle_kick = 0
        self.maybe_kick(self.now, self.k.stall_kick * 0.9)
        self.assertEqual(self.k.kicks, 0)          # normal cadence
        self.k.tx = bytearray(b"x")
        self.maybe_kick(self.now, 2.0)
        self.assertEqual(self.k.kicks, 0)          # our TX is queued
        self.k.tx = bytearray()
        self.maybe_kick(self.now + self.k.tx_settle / 2, 0.0)
        self.assertEqual(self.k.kicks, 0)          # ... or still in the bridge
        self.maybe_kick(self.now + self.k.tx_settle * 2, 0.0)
        self.assertEqual((self.k.kicks, self.k.stall_kicks), (1, 1))
        self.assertIn("main loop stalled for 2.00 s", "\n".join(ts_lines(self.k)))

    def test_kick_waits_for_kernel_tx_queue(self):
        outq = [1280]
        real = D.fcntl.ioctl
        D.fcntl.ioctl = lambda fd, req, arg: D.struct.pack("i", outq[0])
        try:
            self.k.handle_ctl(b"kick\n")
            self.maybe_kick(self.now, 0.0)
            self.assertEqual(self.k.kicks, 0)
            outq[0] = 0
            self.maybe_kick(self.now + 1, 0.0)
            self.assertEqual(self.k.kicks, 0)  # bridge may just have received TX
            self.maybe_kick(self.now + 1 + self.k.tx_settle * 2, 0.0)
        finally:
            D.fcntl.ioctl = real
        self.assertEqual(self.k.kicks, 1)
        self.assertIn("line coding re-sent: requested via ctl",
                      "\n".join(ts_lines(self.k)))

    def test_kick_waits_after_first_empty_observation_following_write(self):
        self.k.tx = bytearray(b"hello")
        self.k.pump_tx(self.now)
        self.k.handle_ctl(b"kick\n")
        # A delayed poll says nothing about when the last USB packet left
        # the kernel; allow the bridge a full settling interval from now.
        self.maybe_kick(self.now + 2, 0.0)
        self.assertEqual(self.k.kicks, 0)
        self.maybe_kick(self.now + 2 + self.k.tx_settle * 2, 0.0)
        self.assertEqual(self.k.kicks, 1)

    def test_empty_tx_observation_uses_clock_after_ioctl(self):
        self.k.tx_empty_since = None
        self.k.handle_ctl(b"kick\n")
        clock = [self.now]

        def delayed_ioctl(fd, req, arg):
            clock[0] = self.now + 2
            return D.struct.pack("i", 0)

        # The loop timestamp predates both processing and the queue query.
        with patch.object(D.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(D.fcntl, "ioctl", side_effect=delayed_ioctl):
            self.k.maybe_kick(self.now, 0.0)
        self.assertEqual(self.k.tx_empty_since, self.now + 2)
        self.assertEqual(self.k.kicks, 0)
        self.maybe_kick(self.now + 2 + self.k.tx_settle / 2, 0.0)
        self.assertEqual(self.k.kicks, 0)
        self.maybe_kick(self.now + 2 + self.k.tx_settle * 2, 0.0)
        self.assertEqual(self.k.kicks, 1)

    def test_tick_counts_whole_interval_without_reading(self):
        self.k.idle_kick = 0
        self.k.in_fd, self.k.ctl_fd = -1, -2
        self.k.last_sweep = self.now
        for select_start in (100.0, 100.2):
            with self.subTest(select_start=select_start):
                self.k.prev_tick = 100.0
                before = self.k.kicks
                with patch.object(D, "boottime", side_effect=[select_start, 100.4]), \
                        patch.object(D.select, "select", return_value=([], [], [])), \
                        patch.object(self.k, "maybe_rotate"), \
                        patch.object(self.k, "write_status"):
                    self.k._tick()
                self.assertEqual(self.k.kicks, before + 1)

    def test_high_baud_polling_stays_within_stall_budget(self):
        self.k.idle_kick = 0
        self.k.in_fd, self.k.ctl_fd = -1, -2
        self.k.last_sweep = self.now
        self.k.stall_kick = 0.05
        self.k.prev_tick = 100.0
        with patch.object(D, "boottime", side_effect=[100.0, 100.025]), \
                patch.object(D.select, "select", return_value=([], [], [])) as select_call, \
                patch.object(self.k, "maybe_rotate"), \
                patch.object(self.k, "write_status"):
            self.k._tick()
        self.assertEqual(select_call.call_args.args[3], 0.025)
        self.assertEqual(self.k.kicks, 0)

    def test_auto_mode_only_for_the_holtek_bridge(self):
        self.assertEqual(self.k.kick_mode, "auto")
        self.assertEqual(D.usb_id("/dev/null"), "")
        self.assertEqual(D.usb_id(os.ttyname(self.slave)), "")
        self.assertRegex(D.HOLTEK_ID, r"^[0-9a-f]{4}:[0-9a-f]{4}$")


class TestHandleInput(Base):
    def test_input_queued_and_logged(self):
        self.k.handle_input(b"hello")
        self.assertEqual(bytes(self.k.tx), b"hello")
        self.assertIn("TX 5B", "\n".join(ts_lines(self.k)))

    def test_tx_cap_drops_incoming(self):
        self.k.tx = bytearray(D.TX_CAP)
        self.k.handle_input(b"x")
        self.assertEqual(len(self.k.tx), D.TX_CAP)
        self.assertEqual(self.k.tx_dropped, 1)
        self.assertIn("tx queue full", "\n".join(ts_lines(self.k)))


class TestStatus(Base):
    def test_contract_keys_for_client(self):
        self.k.write_status(force=True)
        with open(self.k.status_path) as f:
            st = json.load(f)
        for key in ("pid", "connected", "tty", "rx_bytes", "intruders_killed",
                    "tx_dropped_bytes", "log_dropped", "files", "line_kicks",
                    "stall_kicks", "line_kick_active"):
            self.assertIn(key, st)
        self.assertFalse(st["connected"])


class TestPortUsers(Base):
    def test_fuser_output_parsed_and_self_excluded(self):
        shim = os.path.join(self.tmp.name, "bin")
        os.makedirs(shim)
        with open(os.path.join(shim, "fuser"), "w") as f:
            f.write("#!/bin/sh\necho ' 111 222 %d'\n" % os.getpid())
        os.chmod(os.path.join(shim, "fuser"), 0o755)
        saved = os.environ["PATH"]
        os.environ["PATH"] = shim + os.pathsep + saved
        try:
            self.k.fuser_ok = True
            self.assertEqual(self.k.port_users("/dev/null"), [111, 222])
        finally:
            os.environ["PATH"] = saved

    def test_proc_fallback_finds_tty_holder(self):
        master, slave = os.openpty()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdin=slave, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        try:
            sl_path = os.ttyname(slave)
            rdev = os.stat(sl_path).st_rdev
            deadline = time.monotonic() + 5
            holds = False
            while time.monotonic() < deadline and not holds:
                holds = self.k._pid_holds(child.pid, rdev) is True
                time.sleep(0.05)
            self.assertTrue(holds)
            self.k.fuser_ok = False
            self.assertIn(child.pid, self.k.port_users(sl_path))
        finally:
            child.kill()
            child.wait()
            os.close(master)
            os.close(slave)
        self.assertFalse(self.k._pid_holds(child.pid, rdev))


class TestUsbResetHelper(unittest.TestCase):
    def test_refuses_non_usbfs_path(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "wb-pc-consoled"),
                            "--usb-reset", "/dev/null"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"refusing", r.stderr)


if __name__ == "__main__":
    unittest.main()
