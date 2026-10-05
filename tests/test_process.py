"""Process lifetime helpers, exercised against real processes."""

import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import niri_agent as na


class ProcessIdentityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(na, "STATE_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def spawn(self):
        proc = subprocess.Popen([shutil.which("sleep") or "/bin/sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        return proc

    def session(self, proc, start):
        return {"name": "web", "pid": proc.pid, "pid_start": start, "workspace": "agent-web"}

    def test_zombie_is_not_alive(self):
        proc = self.spawn()
        start = na.proc_start(proc.pid)
        os.kill(proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while Path(f"/proc/{proc.pid}/stat").read_text().rpartition(")")[2].split()[0] != "Z":
            self.assertLess(time.monotonic(), deadline, "process never became a zombie")
            time.sleep(0.01)
        self.assertFalse(na.pid_alive(proc.pid, start))

    def test_stop_signals_the_recorded_process_promptly(self):
        proc = self.spawn()
        t0 = time.monotonic()
        na.stop_session(None, self.session(proc, na.proc_start(proc.pid)))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(proc.wait(timeout=1), -signal.SIGTERM)

    def test_stop_never_signals_a_reused_pid(self):
        # After a reboot (other boot id) or pid reuse (other start time), the pid in
        # session.json belongs to someone else, e.g. the user's own niri or dbus-daemon.
        proc = self.spawn()
        boot, starttime = na.proc_start(proc.pid).rsplit(":", 1)
        for stale in (f"{'0' * len(boot)}:{starttime}", f"{boot}:{int(starttime) - 1}", None):
            na.stop_session(None, self.session(proc, stale))
        self.assertIsNone(proc.poll())

    def test_stop_child_reaps(self):
        proc = self.spawn()
        t0 = time.monotonic()
        na.stop_child(proc)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(Path(f"/proc/{proc.pid}").exists())


@unittest.skipUnless(shutil.which("dbus-daemon") and shutil.which("dbus-send"), "needs dbus-daemon and dbus-send")
class PrivateBusProcessTest(unittest.TestCase):
    def test_bus_is_ready_on_return_cannot_activate_portals_and_stops(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sdir = Path(tmp.name)
        address = na.dbus_unix_address(sdir / "bus")
        env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": address}
        bus = na.start_bus(sdir, address, env)
        self.addCleanup(lambda: (bus.poll() is None and bus.kill(), bus.wait()))

        # Uses this machine's real service files: whatever is installed, portals stay out.
        out = subprocess.run(
            ["dbus-send", f"--bus={address}", "--print-reply", "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus", "org.freedesktop.DBus.ListActivatableNames"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        names = set(re.findall(r'string "([^"]+)"', out)) - {"org.freedesktop.DBus"}
        self.assertLessEqual(names, set(na.PRIVATE_BUS_SERVICES))

        start = na.proc_start(bus.pid)
        na.terminate(bus.pid, start)
        bus.wait(timeout=2)
        self.assertFalse(na.pid_alive(bus.pid, start))


if __name__ == "__main__":
    unittest.main()
