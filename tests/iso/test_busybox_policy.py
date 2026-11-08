import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class BusyBoxPolicyTests(unittest.TestCase):
    def test_virtual_product_guard_does_not_depend_on_gnu_tr_classes(self):
        # Observed in Debian 13.7 d-i BusyBox 1.37: tr leaves "Standard PC"
        # unchanged for '[:lower:]' '[:upper:]', bypassing the former guard.
        for relative in ("iso/preseed/early.sh", "iso/preseed/select-disk.sh"):
            script = (ROOT / relative).read_text()
            self.assertNotIn("tr '[:lower:]' '[:upper:]'", script, relative)
            self.assertIn("grep -Eiq 'qemu|kvm|standard pc'", script, relative)


if __name__ == "__main__":
    unittest.main()
