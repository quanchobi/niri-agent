"""Process lifetime helpers, exercised against a real process whose comm is "niri"."""

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import niri_agent as na


class FakeNiriProcessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A copy of sleep named "niri", so /proc/<pid>/comm reads "niri".
        self.fake_niri = Path(tmp.name) / "niri"
        shutil.copy(shutil.which("sleep") or "/bin/sleep", self.fake_niri)

    def spawn(self):
        proc = subprocess.Popen([str(self.fake_niri), "30"], start_new_session=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        deadline = time.monotonic() + 2
        while not na.pid_alive(proc.pid):
            self.assertLess(time.monotonic(), deadline, "fake niri never became visible")
            time.sleep(0.01)
        return proc

    def test_zombie_is_not_alive(self):
        proc = self.spawn()
        os.kill(proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while Path(f"/proc/{proc.pid}/stat").read_text().rpartition(")")[2].split()[0] != "Z":
            self.assertLess(time.monotonic(), deadline, "process never became a zombie")
            time.sleep(0.01)
        self.assertFalse(na.pid_alive(proc.pid))

    def test_terminate_pid_returns_promptly_for_own_child(self):
        proc = self.spawn()
        t0 = time.monotonic()
        na.terminate(proc.pid)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_stop_child_reaps(self):
        proc = self.spawn()
        t0 = time.monotonic()
        na.stop_child(proc)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(Path(f"/proc/{proc.pid}").exists())

    def test_non_niri_pid_is_not_alive(self):
        self.assertFalse(na.pid_alive(os.getpid()))


if __name__ == "__main__":
    unittest.main()
