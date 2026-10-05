import ctypes
import ctypes.util
import struct
import tempfile
import unittest
from pathlib import Path

import niri_agent as na


class ParseComboTest(unittest.TestCase):
    def test_modifiers_and_aliases(self):
        self.assertEqual(na.parse_combo("Control+Shift+T"), (["ctrl", "shift"], "t"))
        self.assertEqual(na.parse_combo("super+enter"), (["super"], "Return"))
        self.assertEqual(na.parse_combo("pgdn"), ([], "Next"))
        self.assertEqual(na.parse_combo("f5"), ([], "F5"))

    def test_plus_key(self):
        self.assertEqual(na.parse_combo("+"), ([], "U002B"))
        self.assertEqual(na.parse_combo("ctrl++"), (["ctrl"], "U002B"))

    def test_rejects_unknown_modifier_and_empty_key(self):
        with self.assertRaises(na.Error):
            na.parse_combo("hyper+a")
        with self.assertRaises(na.Error):
            na.parse_combo("ctrl+")


class KeymapTest(unittest.TestCase):
    def test_text_keysyms(self):
        self.assertEqual([na.char_keysym(c) for c in "aZ9 é\n\t"],
                         ["a", "Z", "9", "U0020", "U00E9", "Return", "Tab"])

    def test_codes_are_unique_and_match_keycodes(self):
        text, codes = na.build_keymap(["a", "U00E9", "a", "Shift_L"])
        self.assertEqual(len(set(codes.values())), len(codes))
        self.assertEqual(set(codes), {"a", "U00E9", "Shift_L", "Control_L", "Alt_L", "Super_L"})
        for sym, code in codes.items():
            self.assertIn(f"<K{code}> = {code + 8};", text)
            self.assertIn(f"key <K{code}> {{[ {sym} ]}};", text)
        self.assertIn(f"modifier_map Mod1 {{ <K{codes['Alt_L']}> }};", text)

    def test_every_key_is_below_max_keycode(self):
        # GTK3 resolves key bindings via keycodes in [min_keycode, max_keycode); a keysym on
        # max_keycode types text but never triggers BackSpace/Escape/arrow/ctrl bindings.
        lib = ctypes.util.find_library("xkbcommon")
        if not lib:
            self.skipTest("libxkbcommon not available")
        xkb = ctypes.CDLL(lib)
        xkb.xkb_context_new.restype = ctypes.c_void_p
        xkb.xkb_context_new.argtypes = [ctypes.c_int]
        xkb.xkb_keymap_new_from_string.restype = ctypes.c_void_p
        xkb.xkb_keymap_new_from_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        xkb.xkb_keymap_max_keycode.argtypes = [ctypes.c_void_p]
        xkb.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
        xkb.xkb_context_unref.argtypes = [ctypes.c_void_p]
        text, codes = na.build_keymap(["a", "BackSpace"])
        ctx = xkb.xkb_context_new(0)
        keymap = xkb.xkb_keymap_new_from_string(ctx, text.encode(), 1, 0)
        self.assertTrue(keymap, "keymap failed to compile")
        try:
            max_keycode = xkb.xkb_keymap_max_keycode(keymap)
            for sym, code in codes.items():
                self.assertLess(code + 8, max_keycode, sym)
        finally:
            xkb.xkb_keymap_unref(keymap)
            xkb.xkb_context_unref(ctx)


class PrivateBusTest(unittest.TestCase):
    def write_service(self, d: Path, name: str, extra: str = ""):
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.service").write_text(f"[D-BUS Service]\nName={name}\nExec=/usr/libexec/{d.name}-{name}\n{extra}")

    def test_only_flatpak_services_are_activatable_and_never_via_systemd(self):
        with tempfile.TemporaryDirectory() as tmp:
            user, system, dest = Path(tmp, "user"), Path(tmp, "system"), Path(tmp, "dest")
            for name in ("org.freedesktop.portal.Desktop", "org.freedesktop.portal.Documents",
                         "org.freedesktop.impl.portal.desktop.gtk", *na.PRIVATE_BUS_SERVICES):
                self.write_service(system, name, "SystemdService=x.service\n")
            self.write_service(user, "org.freedesktop.portal.Flatpak")
            na.write_private_services(dest, [user, system])
            files = {p.name: p.read_text() for p in dest.iterdir()}
        self.assertEqual(set(files), {f"{n}.service" for n in na.PRIVATE_BUS_SERVICES})
        self.assertTrue(all("SystemdService" not in text for text in files.values()))
        # Like dbus-daemon, the first directory (XDG_DATA_HOME) wins.
        self.assertIn("Exec=/usr/libexec/user-org.freedesktop.portal.Flatpak", files["org.freedesktop.portal.Flatpak.service"])

    def test_unix_address_escapes_unsafe_bytes(self):
        self.assertEqual(na.dbus_unix_address(Path("/run/a b/x,y=z.bus")), "unix:path=/run/a%20b/x%2cy%3dz.bus")


class WireTest(unittest.TestCase):
    def test_string_round_trip_and_padding(self):
        for s in ("", "abc", "wl_seat", "zwlr_virtual_pointer_manager_v1"):
            data = na.wl_string(s)
            self.assertEqual(len(data) % 4, 0)
            decoded, off = na.unpack_string(data + b"tail", 0)
            self.assertEqual((decoded, off), (s, len(data)))

    def test_fixed(self):
        self.assertEqual(struct.unpack("=i", na.wl_fixed(-15))[0], -15 * 256)


if __name__ == "__main__":
    unittest.main()
