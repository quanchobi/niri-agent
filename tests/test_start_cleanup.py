"""start_session must undo everything it did when any step after spawning fails."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import niri_agent as na

TARGET_WS = {"id": 7, "idx": 3, "name": None, "output": "DP-1"}
NESTED_PID = 4242
BUS_PID = 5151
HOST_BUS = "unix:path=/run/user/1000/bus"


class FakeHost:
    path = "/run/user/1000/niri.wayland-1.100.sock"

    def __init__(self):
        self.actions = []

    def request(self, req):
        if req == "Workspaces":
            return [TARGET_WS]
        if req == "Windows":
            return []
        raise AssertionError(f"unexpected request {req!r}")

    def action(self, action, /, **args):
        self.actions.append(action)


class FakeEvents:
    def __init__(self, path):
        self._events = iter([
            {"ConfigLoaded": {"failed": False}},
            {"WindowOpenedOrChanged": {"window": {"id": 55, "pid": NESTED_PID, "workspace_id": TARGET_WS["id"]}}},
        ])

    def drain(self, *a, **k):
        pass

    def next(self, timeout):
        return next(self._events, None)

    def close(self):
        pass


class FakeProc:
    pid = NESTED_PID
    returncode = None

    def poll(self):
        return None


class FakeBus:
    pid = BUS_PID


class StartCleanupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.runtime = self.state / "run"
        self.runtime.mkdir()
        self.host = FakeHost()
        self.kdl_writes = []
        patches = [
            mock.patch.object(na, "STATE_DIR", self.state),
            mock.patch.object(na, "host_niri", return_value=self.host),
            mock.patch.object(na, "host_wayland_display", return_value="wayland-1"),
            mock.patch.object(na, "check_installed"),
            mock.patch.object(na, "EventStream", FakeEvents),
            mock.patch.object(na, "write_agents_kdl", side_effect=self.kdl_writes.append),
            mock.patch.object(na, "idle_kdl", return_value="IDLE"),
            mock.patch.object(na, "runtime_dir", return_value=self.runtime),
            mock.patch.object(na, "host_bus_address", return_value=HOST_BUS),
        ]
        self.popen = mock.patch.object(na.subprocess, "Popen", return_value=FakeProc()).start()
        self.start_bus = mock.patch.object(na, "start_bus", return_value=FakeBus()).start()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.stop_child = mock.patch.object(na, "stop_child").start()
        self.unname = mock.patch.object(na, "unname_workspace").start()
        self.addCleanup(mock.patch.stopall)

    def assert_rolled_back(self, bus_started: bool):
        stopped = [c.args[0].pid for c in self.stop_child.call_args_list]
        self.assertEqual(stopped, [NESTED_PID, BUS_PID] if bus_started else [NESTED_PID])
        self.unname.assert_called_once_with(self.host, "agent-web")
        self.assertFalse((self.state / "sessions" / "web").exists())
        self.assertEqual(self.kdl_writes[-1], "IDLE")

    def test_socket_never_appears(self):
        with mock.patch.object(na, "find_nested_socket", side_effect=na.Error("no socket")):
            with self.assertRaises(na.Error):
                na.start_session("web", "DP-1", 1280, 800)
        self.assert_rolled_back(bus_started=False)

    def test_nested_output_never_ready(self):
        with mock.patch.object(na, "find_nested_socket", return_value=("/run/x.sock", "wayland-2")), \
             mock.patch.object(na, "wait_for_nested", side_effect=na.Error("nested niri has no output")):
            with self.assertRaises(na.Error):
                na.start_session("web", "DP-1", 1280, 800)
        self.assert_rolled_back(bus_started=True)

    def test_interrupted_after_spawn(self):
        with mock.patch.object(na, "find_nested_socket", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                na.start_session("web", "DP-1", 1280, 800)
        self.assert_rolled_back(bus_started=False)

    def test_success_keeps_session_and_restores_kdl(self):
        with mock.patch.object(na, "find_nested_socket", return_value=("/run/x.sock", "wayland-2")), \
             mock.patch.object(na, "wait_for_nested", return_value={"width": 1280, "height": 800, "scale": 1.0}):
            s = na.start_session("web", "DP-1", 1280, 800)
        self.assertEqual(s["pid"], NESTED_PID)
        self.assertTrue((self.state / "sessions" / "web" / "session.json").is_file())
        self.stop_child.assert_not_called()
        self.unname.assert_not_called()
        self.assertEqual(self.kdl_writes[-1], "IDLE")
        self.assertEqual((s["bus_pid"], s["host_bus"]), (BUS_PID, HOST_BUS))

    def test_apps_get_the_private_bus_and_the_bus_gets_the_nested_display(self):
        with mock.patch.object(na, "find_nested_socket", return_value=("/run/x.sock", "wayland-2")), \
             mock.patch.object(na, "wait_for_nested", return_value={"width": 1280, "height": 800, "scale": 1.0}):
            s = na.start_session("web", "DP-1", 1280, 800)
        private = na.dbus_unix_address(Path(s["bus_socket"]))
        self.assertNotEqual(private, HOST_BUS)
        niri_env = self.popen.call_args.kwargs["env"]
        self.assertEqual(niri_env["DBUS_SESSION_BUS_ADDRESS"], private)
        bus_env = self.start_bus.call_args.args[2]
        self.assertEqual((bus_env["DBUS_SESSION_BUS_ADDRESS"], bus_env["WAYLAND_DISPLAY"]), (private, "wayland-2"))


if __name__ == "__main__":
    unittest.main()
