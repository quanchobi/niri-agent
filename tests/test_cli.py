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


if __name__ == "__main__":
    unittest.main()
