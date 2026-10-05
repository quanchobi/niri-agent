import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import niri_agent as na


class MenuTest(unittest.TestCase):
    def test_missing_launcher_is_a_clean_error(self):
        args = argparse.Namespace(dmenu="definitely-not-a-launcher-xyz --dmenu")
        with mock.patch.object(na, "all_sessions", return_value=[{"name": "a1", "pid": 1}]), \
             mock.patch.object(na, "pid_alive", return_value=True):
            with self.assertRaises(na.Error) as cm:
                na.cmd_menu(args)
        self.assertIn("definitely-not-a-launcher-xyz", str(cm.exception))


class StopAllTest(unittest.TestCase):
    def test_one_failure_does_not_skip_the_rest(self):
        sessions = [{"name": n, "pid": i, "workspace": f"agent-{n}"} for i, n in enumerate(("a1", "a2", "a3"))]
        stopped = []

        def fake_stop(host, s):
            if s["name"] == "a2":
                raise PermissionError("cannot signal pid")
            stopped.append(s["name"])

        out = io.StringIO()
        with mock.patch.object(na, "all_sessions", return_value=sessions), \
             mock.patch.object(na, "host_niri", side_effect=na.Error("no host")), \
             mock.patch.object(na, "stop_session", side_effect=fake_stop), \
             redirect_stdout(out):
            with self.assertRaises(na.Error) as cm:
                na.cmd_stop(argparse.Namespace(all=True, name=None))
        self.assertEqual(stopped, ["a1", "a3"])
        self.assertIn("a2", str(cm.exception))
        self.assertIn('"a1"', out.getvalue())
        self.assertIn('"a3"', out.getvalue())


class RunBusTest(unittest.TestCase):
    def spawned(self, session, argv):
        nested = mock.Mock()
        with mock.patch.object(na, "live_session", return_value=session), \
             mock.patch.object(na, "Niri", return_value=nested), redirect_stdout(io.StringIO()):
            na.main(argv)
        nested.action.assert_called_once()
        return nested.action.call_args.kwargs["command"]

    def test_default_inherits_the_private_bus(self):
        s = {"niri_socket": "/x.sock", "host_bus": "unix:path=/run/user/1000/bus"}
        self.assertEqual(self.spawned(s, ["run", "web", "--", "kitty", "-e", "sh"]), ["kitty", "-e", "sh"])

    def test_host_bus_points_the_app_at_the_recorded_host_bus(self):
        s = {"niri_socket": "/x.sock", "host_bus": "unix:path=/run/user/1000/bus"}
        self.assertEqual(self.spawned(s, ["run", "--host-bus", "web", "--", "kitty"]),
                         ["env", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus", "kitty"])

    def test_host_bus_without_recorded_address_fails(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()) as err:
            self.spawned({"niri_socket": "/x.sock", "host_bus": None}, ["run", "--host-bus", "web", "--", "kitty"])
        self.assertIn("--host-bus", err.getvalue())

    def test_option_after_name_is_rejected_not_spawned(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()) as err:
            self.spawned({"niri_socket": "/x.sock", "host_bus": None}, ["run", "web", "--host-bus", "--", "kitty"])
        self.assertIn("options go before the name", err.getvalue())


if __name__ == "__main__":
    unittest.main()
