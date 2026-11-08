import unittest
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class BusyBoxPolicyTests(unittest.TestCase):
    def test_lvm_list_requires_exactly_one_root_even_with_padding(self):
        script = (ROOT / 'iso/preseed/btrfs.sh').read_text()
        self.assertNotIn("tr -d '[:space:]'", script)
        command = '''awk 'NF { if (NF != 1 || $1 != "root") exit 1; n++ } END { if (n != 1) exit 1 }' '''
        for output, accepted in [('  root\n', True), ('root\nroot\n', False),
                                 ('root\nswap\n', False), ('\n', False)]:
            with self.subTest(output=output):
                result = subprocess.run(command, shell=True, input=output, text=True)
                self.assertEqual(result.returncode == 0, accepted)

    def test_preseed_late_command_has_no_escaped_newlines(self):
        lines = (ROOT / 'iso/preseed/base.cfg').read_text().splitlines()
        commands = [line for line in lines if line.startswith('d-i preseed/late_command string ')]
        self.assertEqual(len(commands), 1)
        self.assertNotIn(r'\n', commands[0])
        self.assertIn('sh -eu /xc0/btrfs.sh', commands[0])
        self.assertIn('in-target sh /root/xc0-late.sh', commands[0])

    def test_virtual_product_guard_does_not_depend_on_gnu_tr_classes(self):
        # Observed in Debian 13.7 d-i BusyBox 1.37: tr leaves "Standard PC"
        # unchanged for '[:lower:]' '[:upper:]', bypassing the former guard.
        for relative in ("iso/preseed/early.sh", "iso/preseed/select-disk.sh"):
            script = (ROOT / relative).read_text()
            self.assertNotIn("tr '[:lower:]' '[:upper:]'", script, relative)
            self.assertIn("grep -Eiq 'qemu|kvm|standard pc'", script, relative)


if __name__ == "__main__":
    unittest.main()
