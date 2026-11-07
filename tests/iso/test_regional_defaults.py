"""Keep installer and post-boot keyboard/language defaults aligned."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class RegionalDefaultsTests(unittest.TestCase):
    def test_installer_is_english_with_norwegian_keyboard_and_oslo_time(self):
        lines = (ROOT / "iso/preseed/base.cfg").read_text().splitlines()
        for answer in (
            "d-i debian-installer/locale string en_US.UTF-8",
            "d-i debian-installer/language string en",
            "d-i keyboard-configuration/xkb-keymap select no",
            "d-i keyboard-configuration/layoutcode string no",
            "d-i time/zone string Europe/Oslo",
        ):
            self.assertEqual(lines.count(answer), 1)

    def test_post_install_desktop_defaults_do_not_revert_to_us_keyboard(self):
        hypr = (ROOT / "config/desktop/hypr/hyprland.lua").read_text()
        uwsm = (ROOT / "config/desktop/uwsm/env").read_text()
        self.assertIn('kb_layout = os.getenv("XKB_DEFAULT_LAYOUT") or "no"', hypr)
        self.assertIn('${XKBLAYOUT:-no}', uwsm)


if __name__ == "__main__":
    unittest.main()
