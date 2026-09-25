import struct
import unittest

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
        self.assertIn(f"maximum = {max(codes.values()) + 8};", text)
        self.assertIn(f"modifier_map Mod1 {{ <K{codes['Alt_L']}> }};", text)


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
