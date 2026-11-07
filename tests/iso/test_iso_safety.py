import base64
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
ISO = ROOT / "iso"
PRESEED = ISO / "preseed"
REF = "a" * 40
TEMP_ROOT = Path("/tmp/opencode")
if not TEMP_ROOT.is_dir():
    TEMP_ROOT = Path(tempfile.gettempdir())
TARGET_MODEL = "WDS100T1X0E"
PROTECTED_MODEL = "WDS200T1X0E"
FULL_MODEL = "WDC WDS100T1X0E-00AFY0                  "
SECTORS = 1953525168


def make_executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class DiskScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="xc0-iso-disk-", dir=TEMP_ROOT)
        self.dir = Path(self.tempdir.name)
        self.sysfs = self.dir / "sys"
        self.xc0 = self.dir / "xc0"
        self.bin = self.dir / "bin"
        self.marker = self.dir / "authorized"
        self.debconf_log = self.dir / "debconf.log"
        self.inventory_log = self.dir / "inventory.log"
        self.confmodule = self.dir / "confmodule"
        for directory in (
            self.sysfs / "block",
            self.sysfs / "class" / "dmi" / "id",
            self.xc0,
            self.bin,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.confmodule.write_text(
            "db_subst() { printf 'SUBST %s %s %s\\n' \"$1\" \"$2\" \"$3\" >> \"$XC0_TEST_LOG\"; }\n"
            "db_register() { printf 'REGISTER %s %s\\n' \"$1\" \"$2\" >> \"$XC0_TEST_LOG\"; }\n"
            "db_fset() { printf 'FLAG %s %s %s\\n' \"$1\" \"$2\" \"$3\" >> \"$XC0_TEST_LOG\"; }\n"
            "db_set() { printf '%s=%s\\n' \"$1\" \"$2\" >> \"$XC0_TEST_LOG\"; [ \"$1\" != \"${XC0_FAIL_DEBCONF_SET:-}\" ]; }\n"
            "db_input() { printf 'INPUT %s %s\\n' \"$1\" \"$2\" >> \"$XC0_TEST_LOG\"; }\n"
            "db_go() { :; }\n"
            "db_get() { printf 'GET %s\\n' \"$1\" >> \"$XC0_TEST_LOG\"; if [ \"$1\" = \"${XC0_WRONG_DEBCONF_GET:-}\" ]; then RET=wrong; elif [ \"$1\" = xc0/confirm-erase ]; then RET=${XC0_CONFIRMATION-}; else RET=$(awk -F= -v key=\"$1\" '$1 == key { value=$2 } END { print value }' \"$XC0_TEST_LOG\"); if ! awk -F= -v key=\"$1\" '$1 == key { found=1 } END {exit !found}' \"$XC0_TEST_LOG\"; then case \"$1\" in partman-auto/method) RET=crypto ;; partman-auto/choose_recipe) RET=xc0-$XC0_TEST_LAYOUT-crypto-btrfs ;; esac; fi; fi; }\n",
            encoding="utf-8",
        )
        make_executable(self.bin / "debconf-loadtemplate", "#!/bin/sh\nexit 0\n")
        make_executable(self.bin / "logger", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$XC0_INVENTORY_LOG\"\n")
        make_executable(
            self.bin / "debconf-set",
            "#!/bin/sh\n"
            "printf '%s=%s\\n' \"$1\" \"$2\" >> \"$XC0_TEST_LOG\"\n"
            "[ \"$1\" != \"${XC0_FAIL_DEBCONF_SET:-}\" ]\n",
        )
        for name in ("select-disk.sh", "00xc0-guard", "early.sh"):
            text = (PRESEED / name).read_text(encoding="utf-8")
            text = text.replace("/tmp/xc0-host", "@@XC0_HOST@@")
            text = text.replace("/tmp/xc0-authorized", "@@XC0_MARKER@@")
            text = text.replace("/xc0", str(self.xc0))
            text = text.replace("/usr/share/debconf/confmodule", str(self.confmodule))
            text = text.replace("@@XC0_MARKER@@", str(self.marker))
            text = text.replace("@@XC0_HOST@@", str(self.dir / "host"))
            text = text.replace("/sys/class/dmi/id/product_name", str(self.sysfs / "class/dmi/id/product_name"))
            make_executable(self.xc0 / name, text)
        shutil.copytree(PRESEED / "recipes", self.xc0 / "recipes")
        self.configure()

    def tearDown(self):
        self.tempdir.cleanup()

    def configure(self, mode="release", serial=None, dmi=None, *, target_mode=None,
                  model="", protected="", hostname="xc0-test", ref=REF):
        serial = ("XC0_TEST_DISK" if mode == "vm-test" else "TARGET") if serial is None else serial
        dmi = ("QEMU" if mode == "vm-test" else "Test Product") if dmi is None else dmi
        target_mode = ("vm-test" if mode == "vm-test" else "serial-dmi") if target_mode is None else target_mode
        values = {
            "MODE": mode,
            "PROFILE": "stable" if hostname == "thor" else "testing",
            "LAYOUT": "thor" if hostname == "thor" else "loki",
            "MARIUS_UID": "1001", "MARIUS_GID": "1001",
            "XCLOUD_UID": "1000", "XCLOUD_GID": "1000",
            "HOSTNAME": hostname,
            "TARGET_MODE": target_mode,
            "TARGET_SERIAL": serial,
            "TARGET_DMI": dmi,
            "TARGET_MODEL": model,
            "PROTECTED_MODEL": protected,
            "STAGE2_REF": ref,
            "ISO_ID": "xc0-test",
        }
        (self.xc0 / "install.conf").write_text(
            "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        self.layout = values["LAYOUT"]
        self.set_dmi(dmi)

    def configure_model(self, **overrides):
        values = dict(target_mode="model-confirm", serial="", dmi="",
                      model=TARGET_MODEL, protected=PROTECTED_MODEL, hostname="thor")
        values.update(overrides)
        self.configure(**values)
        self.set_dmi("Physical Thor")

    def set_dmi(self, product):
        (self.sysfs / "class" / "dmi" / "id" / "product_name").write_text(
            product + "\n", encoding="utf-8"
        )

    def add_disk(self, name, serial, *, removable=False, usb=False,
                 model="Synthetic disk", sectors=33554432, serial_in_device=False):
        bus = self.sysfs / "devices" / "pci"
        if usb:
            bus /= "usb1"
        device = bus / name
        (device / "device").mkdir(parents=True)
        (device / "removable").write_text(f"{int(removable)}\n", encoding="utf-8")
        (device / "size").write_text(f"{sectors}\n", encoding="utf-8")
        (device / "device" / "model").write_text(model + "\n", encoding="utf-8")
        if serial is not None:
            serial_path = device / "device" / "serial" if serial_in_device else device / "serial"
            serial_path.write_text(serial + "\n", encoding="utf-8")
        (self.sysfs / "block" / name).symlink_to(device)

    def clear_disks(self):
        for name in ("block", "devices"):
            path = self.sysfs / name
            if path.exists():
                shutil.rmtree(path)
        (self.sysfs / "block").mkdir()

    def clear_results(self):
        for path in (self.marker, self.debconf_log, self.inventory_log):
            if path.exists():
                path.unlink()

    def answers(self):
        if not self.debconf_log.exists():
            return []
        return [line for line in self.debconf_log.read_text(encoding="utf-8").splitlines()
                 if not line.startswith(("REGISTER ", "FLAG ", "SUBST ", "INPUT ", "GET "))]

    def run_hook(self, hook, args=(), confirmation=None, fail_debconf=None, wrong_get=None):
        env = os.environ.copy()
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["XC0_SYSFS"] = str(self.sysfs)
        env["XC0_TEST_LOG"] = str(self.debconf_log)
        env["XC0_INVENTORY_LOG"] = str(self.inventory_log)
        env["XC0_TEST_LAYOUT"] = self.layout
        env.pop("XC0_CONFIRMATION", None)
        env.pop("XC0_FAIL_DEBCONF_SET", None)
        env.pop("XC0_WRONG_DEBCONF_GET", None)
        if confirmation is not None:
            env["XC0_CONFIRMATION"] = confirmation
        if fail_debconf is not None:
            env["XC0_FAIL_DEBCONF_SET"] = fail_debconf
        if wrong_get is not None:
            env["XC0_WRONG_DEBCONF_GET"] = wrong_get
        return subprocess.run(
            ["sh", "-eu", str(self.xc0 / hook), *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
        )

    def run_select(self, *args, confirmation=None, fail_debconf=None, wrong_get=None):
        return self.run_hook(
            "select-disk.sh", args, confirmation=confirmation, fail_debconf=fail_debconf,
            wrong_get=wrong_get,
        )

    def assert_rejected_without_answers(self, result):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.answers(), [])

    def test_wrong_or_missing_serial_never_selects_a_disk(self):
        self.configure(mode="vm-test")
        for serial in ("WRONG", None):
            with self.subTest(serial="missing" if serial is None else "wrong"):
                self.clear_disks()
                self.clear_results()
                self.add_disk("vda", serial)
                self.assert_rejected_without_answers(self.run_select())

    def test_duplicate_serial_is_rejected(self):
        self.add_disk("sda", "TARGET")
        self.add_disk("sdb", "TARGET")
        result = self.run_select(confirmation="ERASE TARGET")
        self.assert_rejected_without_answers(result)

    def test_usb_and_removable_disks_are_excluded(self):
        for kind in ("usb", "removable"):
            with self.subTest(kind=kind):
                self.clear_disks()
                self.clear_results()
                self.add_disk(
                    "sda", "TARGET", usb=kind == "usb", removable=kind == "removable"
                )
                result = self.run_select(confirmation="ERASE TARGET")
                self.assert_rejected_without_answers(result)

    def test_changed_disk_order_still_selects_by_serial(self):
        self.add_disk("sda", "OTHER")
        self.add_disk("sdb", "TARGET")
        first = self.run_select(confirmation="ERASE TARGET")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.clear_disks()
        self.add_disk("sda", "TARGET")
        self.add_disk("sdb", "OTHER")
        second = self.run_select(confirmation="ERASE TARGET")
        self.assertEqual(second.returncode, 0, second.stderr)
        disks = [line for line in self.answers() if line.startswith("partman-auto/disk=")]
        self.assertEqual(disks, ["partman-auto/disk=/dev/sdb", "partman-auto/disk=/dev/sda"])

    def test_release_rejects_wrong_dmi_before_confirmation(self):
        self.add_disk("sda", "TARGET")
        self.set_dmi("Another Machine")
        result = self.run_select(confirmation="ERASE TARGET")
        self.assert_rejected_without_answers(result)

    def test_vm_mode_rejects_real_disk_or_non_qemu_dmi(self):
        self.configure(mode="vm-test")
        for name, product in (("sda", "QEMU"), ("vda", "ThinkPad X13")):
            with self.subTest(name=name, product=product):
                self.clear_disks()
                self.clear_results()
                self.set_dmi(product)
                self.add_disk(name, "XC0_TEST_DISK")
                self.assert_rejected_without_answers(self.run_select())

    def test_vm_mode_accepts_only_the_exact_test_identity(self):
        self.configure(mode="vm-test")
        self.add_disk("vda", "XC0_TEST_DISK")
        result = self.run_select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.marker.read_text(encoding="utf-8").strip(),
            f"/dev/vda|XC0_TEST_DISK|vm-test|{REF}|vm-test|||Synthetic disk|QEMU|xc0-test|loki",
        )

    def test_dynamic_confirmation_aliases_and_seen_flags_are_initialized(self):
        self.configure(mode="vm-test")
        self.add_disk("vda", "XC0_TEST_DISK")
        result = self.run_select()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.debconf_log.read_text().splitlines()
        for subsystem in ("lvm", "crypto"):
            alias = f"partman-{subsystem}/confirm_nooverwrite"
            registration = f"REGISTER partman-{subsystem}/confirm {alias}"
            self.assertLess(calls.index(registration), calls.index(alias + "=true"))
            self.assertIn(f"FLAG {alias} seen true", calls)
        self.assertIn("FLAG partman-auto/disk seen true", calls)
        self.assertIn("FLAG grub-installer/bootdev seen true", calls)
        self.assertIn("FLAG partman/choose_partition seen true", calls)

    def test_failed_debconf_set_does_not_write_authorization_marker(self):
        self.add_disk("sda", "TARGET")
        result = self.run_select(
            confirmation="ERASE TARGET", fail_debconf="partman/confirm"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())
        self.assertNotIn("partman/choose_partition=finish", self.answers())

    def test_confirmation_mismatch_emits_no_destructive_answers(self):
        self.add_disk("sda", "TARGET")
        result = self.run_select(confirmation="ERASE WRONG")
        self.assert_rejected_without_answers(result)

    def test_existing_marker_revalidates_dmi_and_serial_every_time(self):
        self.add_disk("sda", "TARGET")
        accepted = self.run_select(confirmation="ERASE TARGET")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        original_answers = self.answers()
        original_marker = self.marker.read_text(encoding="utf-8")

        self.set_dmi("Changed Machine")
        changed_dmi = self.run_select(confirmation="ERASE TARGET")
        self.assertNotEqual(changed_dmi.returncode, 0)
        self.assertEqual(self.answers(), original_answers)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), original_marker)

        self.set_dmi("Test Product")
        (self.sysfs / "block" / "sda" / "serial").write_text("OTHER\n", encoding="utf-8")
        changed_serial = self.run_select(confirmation="ERASE TARGET")
        self.assertNotEqual(changed_serial.returncode, 0)
        self.assertEqual(self.answers(), original_answers)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), original_marker)

    def test_independent_guard_revalidates_and_fails_closed(self):
        self.configure(mode="vm-test")
        self.add_disk("vda", "XC0_TEST_DISK")
        accepted = self.run_hook("00xc0-guard")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        answers = self.answers()
        self.set_dmi("Physical Machine")
        rejected = self.run_hook("00xc0-guard")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.answers(), answers)

    def test_model_policy_matches_full_padded_kernel_model_and_trimmed_live_serial(self):
        self.configure_model()
        self.add_disk("nvme0n1", "  LIVE-TARGET  ", model=FULL_MODEL,
                      sectors=SECTORS, serial_in_device=True)
        # A later loop iteration must not replace the matched model or serial.
        self.add_disk("nvme1n1", "OTHER", model="Other disk", sectors=SECTORS)
        result = self.run_select(confirmation="ERASE LIVE-TARGET")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("partman-auto/disk=/dev/nvme0n1", self.answers())
        self.assertIn("grub-installer/bootdev=/dev/nvme0n1", self.answers())
        marker = self.marker.read_text().strip()
        self.assertEqual(marker, f"/dev/nvme0n1|LIVE-TARGET|release|{REF}|model-confirm|"
                          f"{TARGET_MODEL}|{PROTECTED_MODEL}|{FULL_MODEL.strip()}||thor|thor")
        calls = self.debconf_log.read_text()
        self.assertIn(f"SUBST xc0/confirm-erase MODEL {FULL_MODEL.strip()}\n", calls)
        self.assertIn("SUBST xc0/confirm-erase SERIAL LIVE-TARGET\n", calls)
        self.assertIn("partman-lvm/device_remove_lvm=true", self.answers())
        self.assertIn("partman-md/device_remove_md=true", self.answers())

    def test_protected_only_is_rejected_and_inventoried(self):
        self.configure_model()
        self.add_disk("nvme0n1", "KEEP", model="WDC WDS200T1X0E-00AFY0", sectors=SECTORS)
        self.assert_rejected_without_answers(self.run_select(confirmation="ERASE KEEP"))
        inventory = self.inventory_log.read_text()
        self.assertIn("protected disk=/dev/nvme0n1", inventory)
        self.assertIn("serial=KEEP", inventory)

    def test_protected_and_target_work_in_either_enumeration_order(self):
        for target, protected in (("nvme0n1", "nvme1n1"), ("nvme1n1", "nvme0n1")):
            with self.subTest(target=target):
                self.clear_disks()
                self.clear_results()
                self.configure_model()
                self.add_disk(target, "LIVE-TARGET", model=FULL_MODEL, sectors=SECTORS)
                self.add_disk(protected, "KEEP", model="WDC WDS200T1X0E-00AFY0",
                              sectors=3907029168)
                result = self.run_select(confirmation="ERASE LIVE-TARGET")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"partman-auto/disk=/dev/{target}", self.answers())
                self.assertIn(f"grub-installer/bootdev=/dev/{target}", self.answers())
                for question in ("partman-lvm/device_remove_lvm", "partman-md/device_remove_md"):
                    self.assertIn(question + "=false", self.answers())
                    self.assertNotIn(question + "=true", self.answers())
                    self.assertIn("GET " + question, self.debconf_log.read_text())
                self.assertIn("partman/confirm=true", self.answers())
                self.assertIn("partman-lvm/confirm=true", self.answers())
                validated = self.run_select("--validate-authorized")
                self.assertEqual(validated.returncode, 0, validated.stderr)

    def test_protected_substring_precedes_even_a_broad_target_match(self):
        self.configure_model(model="WDS", protected="KEEP")
        self.add_disk("nvme0n1", "TARGET", model=FULL_MODEL, sectors=SECTORS)
        self.add_disk("nvme1n1", "PROTECTED", model="WDS KEEP model", sectors=SECTORS)
        # The hard prohibition on the known data model also survives a different policy.
        self.add_disk("nvme2n1", "DATA", model="WDC WDS200T1X0E", sectors=SECTORS)
        result = self.run_select(confirmation="ERASE TARGET")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("partman-auto/disk=/dev/nvme0n1", self.answers())
        self.assertIn("partman-lvm/device_remove_lvm=false", self.answers())

    def test_known_data_model_is_never_selected_even_by_serial(self):
        self.add_disk("nvme0n1", "TARGET", model="WDC WDS200T1X0E", sectors=SECTORS)
        self.assert_rejected_without_answers(self.run_select(confirmation="ERASE TARGET"))

    def test_serial_policy_disables_global_removal_with_a_protected_disk(self):
        for target, protected in (("nvme0n1", "nvme1n1"), ("nvme1n1", "nvme0n1")):
            with self.subTest(target=target):
                self.clear_disks()
                self.clear_results()
                self.configure(target_mode="serial-dmi")
                self.add_disk(target, "TARGET", model=FULL_MODEL, sectors=SECTORS)
                self.add_disk(protected, "KEEP", model="WDC WDS200T1X0E-00AFY0",
                              sectors=3907029168)
                result = self.run_select(confirmation="ERASE TARGET")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"partman-auto/disk=/dev/{target}", self.answers())
                self.assertIn(f"grub-installer/bootdev=/dev/{target}", self.answers())
                questions = ("partman-lvm/device_remove_lvm", "partman-md/device_remove_md")
                for question in questions:
                    self.assertIn(question + "=false", self.answers())
                    self.assertNotIn(question + "=true", self.answers())
                    self.assertIn(f"FLAG {question} seen true", self.debconf_log.read_text())
                    self.assertIn("GET " + question, self.debconf_log.read_text())
                answers = self.answers()
                validated = self.run_select("--validate-authorized")
                self.assertEqual(validated.returncode, 0, validated.stderr)
                reinvoked = self.run_select()
                self.assertEqual(reinvoked.returncode, 0, reinvoked.stderr)
                self.assertEqual(self.answers(), answers)
                for question in questions:
                    with self.subTest(question=question):
                        with self.debconf_log.open("a") as log:
                            log.write(question + "=true\n")
                        answers = self.answers()
                        self.assertNotEqual(self.run_select("--validate-authorized").returncode, 0)
                        self.assertNotEqual(self.run_select().returncode, 0)
                        self.assertEqual(self.answers(), answers)
                        with self.debconf_log.open("a") as log:
                            log.write(question + "=false\n")

    def test_model_capacity_window_is_inclusive_and_not_optional(self):
        for sectors in (1749999999, 1750000000, 2100000000, 2100000001, "", "not-a-size"):
            with self.subTest(sectors=sectors):
                self.clear_disks()
                self.clear_results()
                self.configure_model()
                self.add_disk("nvme0n1", "LIVE", model=FULL_MODEL, sectors=sectors)
                result = self.run_select(confirmation="ERASE LIVE")
                if sectors in (1750000000, 2100000000):
                    self.assertEqual(result.returncode, 0, result.stderr)
                else:
                    self.assert_rejected_without_answers(result)

    def test_model_duplicate_target_with_protected_disk_fails_closed(self):
        self.configure_model()
        self.add_disk("nvme0n1", "FIRST", model=FULL_MODEL, sectors=SECTORS)
        self.add_disk("nvme1n1", "SECOND", model=FULL_MODEL, sectors=SECTORS)
        self.add_disk("nvme2n1", "KEEP", model=PROTECTED_MODEL, sectors=SECTORS)
        self.assert_rejected_without_answers(self.run_select(confirmation="ERASE FIRST"))

    def test_model_requires_nonempty_live_serial_and_case_sensitive_model(self):
        for serial, model in ((None, FULL_MODEL), ("", FULL_MODEL), ("  ", FULL_MODEL),
                              ("LIVE", FULL_MODEL.lower())):
            with self.subTest(serial=serial, model=model):
                self.clear_disks()
                self.clear_results()
                self.configure_model()
                self.add_disk("nvme0n1", serial, model=model, sectors=SECTORS)
                self.assert_rejected_without_answers(self.run_select(confirmation="ERASE LIVE"))

    def test_model_policy_still_excludes_usb_and_removable_disks(self):
        for kind in ("usb", "removable"):
            with self.subTest(kind=kind):
                self.clear_disks()
                self.clear_results()
                self.configure_model()
                self.add_disk("nvme0n1", "LIVE", model=FULL_MODEL, sectors=SECTORS,
                              usb=kind == "usb", removable=kind == "removable")
                self.assert_rejected_without_answers(self.run_select(confirmation="ERASE LIVE"))

    def test_model_confirmation_requires_exact_live_serial(self):
        self.configure_model()
        self.add_disk("nvme0n1", "LIVE", model=FULL_MODEL, sectors=SECTORS)
        for confirmation in (None, "", "ERASE ", "ERASE " + TARGET_MODEL, "ERASE live", "ERASE LIVE "):
            with self.subTest(confirmation=confirmation):
                self.clear_results()
                self.assert_rejected_without_answers(self.run_select(confirmation=confirmation))
        result = self.run_select(confirmation="ERASE LIVE")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_or_unknown_policy_never_becomes_a_wildcard(self):
        policies = (
            dict(serial=""), dict(dmi=""), dict(target_mode=""),
            dict(target_mode="unknown"), dict(mode="unknown"),
            dict(target_mode="model-confirm", serial="", dmi="", hostname="thor"),
            dict(target_mode="model-confirm", serial="", dmi="", hostname="thor",
                 model=TARGET_MODEL, protected=""),
            dict(target_mode="model-confirm", serial="", dmi="", hostname="thor",
                 model=" ", protected=PROTECTED_MODEL),
        )
        self.add_disk("nvme0n1", "TARGET", model=FULL_MODEL, sectors=SECTORS)
        for policy in policies:
            with self.subTest(policy=policy):
                self.clear_results()
                self.configure(**policy)
                self.assert_rejected_without_answers(self.run_select(confirmation="ERASE TARGET"))
                self.assertNotEqual(self.run_hook("early.sh").returncode, 0)

    def test_model_hardware_guards_reject_virtual_products_and_other_hostnames(self):
        self.add_disk("nvme0n1", "LIVE", model=FULL_MODEL, sectors=SECTORS)
        for hostname, product in (("loki", "Physical Thor"), ("thor", ""), ("thor", "QEMU"),
                                  ("thor", "KVM host"), ("thor", "Standard PC (Q35)")):
            with self.subTest(hostname=hostname, product=product):
                self.clear_results()
                self.configure_model(hostname=hostname)
                self.set_dmi(product)
                self.assertNotEqual(self.run_hook("early.sh").returncode, 0)
                self.assert_rejected_without_answers(self.run_select(confirmation="ERASE LIVE"))
        self.configure_model()
        self.assertEqual(self.run_hook("early.sh").returncode, 0)

    def test_early_serial_policy_requires_exact_nonempty_dmi(self):
        self.assertEqual(self.run_hook("early.sh").returncode, 0)
        self.set_dmi("Changed Product")
        self.assertNotEqual(self.run_hook("early.sh").returncode, 0)
        self.configure(dmi="")
        self.assertNotEqual(self.run_hook("early.sh").returncode, 0)

    def test_early_emits_selected_expert_recipe_hostname_and_explicit_uid(self):
        for hostname in ("loki", "thor"):
            with self.subTest(hostname=hostname):
                self.configure(hostname=hostname)
                result = self.run_hook("early.sh")
                self.assertEqual(result.returncode, 0, result.stderr)
                included = Path(result.stdout.strip()).read_text()
                self.assertIn(f"d-i netcfg/hostname string {hostname}\n", included)
                self.assertEqual(included.count("d-i passwd/user-uid string 1001\n"), 1)
                self.assertIn(f"partman-auto/choose_recipe select xc0-{hostname}-crypto-btrfs", included)
                self.assertNotIn("select atomic", included)
    def test_early_vm_policy_retains_its_virtual_hardware_guard(self):
        self.configure(mode="vm-test")
        for product in ("QEMU", "Standard PC (Q35)", "KVM virtual machine"):
            with self.subTest(product=product):
                self.set_dmi(product)
                result = self.run_hook("early.sh")
                self.assertEqual(result.returncode, 0, result.stderr)
        self.set_dmi("Physical Machine")
        self.assertNotEqual(self.run_hook("early.sh").returncode, 0)

    def test_model_authorization_is_bound_to_live_identity_and_all_policy_fields(self):
        self.configure_model()
        self.add_disk("nvme0n1", "LIVE", model=FULL_MODEL, sectors=SECTORS)
        result = self.run_select(confirmation="ERASE LIVE")
        self.assertEqual(result.returncode, 0, result.stderr)
        answers = self.answers()
        self.assertEqual(self.run_select().returncode, 0)  # No second prompt/answer required.
        self.assertEqual(self.answers(), answers)
        self.assertEqual(self.debconf_log.read_text().count("INPUT critical xc0/confirm-erase"), 1)
        for field, value in (("serial", "CHANGED"), ("device/model", FULL_MODEL.strip() + "-rev2"),
                             ("size", "2100000001")):
            with self.subTest(field=field):
                path = self.sysfs / "block/nvme0n1" / field
                original = path.read_text()
                path.write_text(value + "\n")
                self.assertNotEqual(self.run_select("--validate-authorized").returncode, 0)
                path.write_text(original)
                self.assertEqual(self.answers(), answers)
        for changes in (dict(ref="b" * 40), dict(model="WDS100"), dict(protected="ANOTHER")):
            with self.subTest(changes=changes):
                self.configure_model(**changes)
                self.assertNotEqual(self.run_select("--validate-authorized").returncode, 0)
                self.assertEqual(self.answers(), answers)
        self.configure_model()
        self.set_dmi("QEMU")
        self.assertNotEqual(self.run_select("--validate-authorized").returncode, 0)

    def test_grub_and_partman_device_are_get_verified_and_revalidated(self):
        for question in ("partman-auto/disk", "grub-installer/bootdev"):
            with self.subTest(question=question):
                self.clear_disks()
                self.clear_results()
                self.configure()
                self.add_disk("sda", "TARGET")
                failed_get = self.run_select(confirmation="ERASE TARGET", wrong_get=question)
                self.assertNotEqual(failed_get.returncode, 0)
                self.assertFalse(self.marker.exists())
                self.clear_results()
                accepted = self.run_select(confirmation="ERASE TARGET")
                self.assertEqual(accepted.returncode, 0, accepted.stderr)
                self.assertIn(f"FLAG {question} seen true", self.debconf_log.read_text())
                with self.debconf_log.open("a") as log:
                    log.write(question + "=/dev/sdz\n")
                answers = self.answers()
                self.assertNotEqual(self.run_select("--validate-authorized").returncode, 0)
                self.assertNotEqual(self.run_select().returncode, 0)
                self.assertEqual(self.answers(), answers)

    def test_protected_disk_removal_flags_are_verified_and_new_disks_invalidate_consent(self):
        self.configure_model()
        self.add_disk("nvme0n1", "LIVE", model=FULL_MODEL, sectors=SECTORS)
        self.assertEqual(self.run_select(confirmation="ERASE LIVE").returncode, 0)
        self.add_disk("nvme1n1", "KEEP", model=PROTECTED_MODEL, sectors=SECTORS)
        self.assertNotEqual(self.run_select("--validate-authorized").returncode, 0)
        for question in ("partman-lvm/device_remove_lvm", "partman-md/device_remove_md"):
            with self.subTest(question=question):
                self.clear_results()
                result = self.run_select(confirmation="ERASE LIVE", wrong_get=question)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.marker.exists())


class InlineBlockingTests(unittest.TestCase):
    def command(self, prefix):
        line = next(
            line
            for line in (PRESEED / "base.cfg").read_text(encoding="utf-8").splitlines()
            if line.startswith(prefix)
        )
        return line[len(prefix) :]

    def assert_blocks(self, command, directory, stubs):
        bin_dir = directory / "bin"
        bin_dir.mkdir()
        for name, text in stubs.items():
            make_executable(bin_dir / name, text)
        command = command.replace("/xc0", str(directory / "xc0"))
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
        process = subprocess.Popen(
            ["sh", "-c", command],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=0.25)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _, stderr = process.communicate(timeout=2)
        self.assertIn("xc0 BLOCKED", stderr)

    def test_missing_hooks_block_inline_fallbacks(self):
        prefixes = (
            "d-i preseed/include_command string ",
            "d-i partman/early_command string ",
            "d-i preseed/late_command string ",
        )
        for prefix in prefixes:
            with self.subTest(hook=prefix.split()[0]), tempfile.TemporaryDirectory(
                prefix="xc0-iso-hook-"
            ) as temp:
                self.assert_blocks(
                    self.command(prefix),
                    Path(temp),
                    {"cp": "#!/bin/sh\nexit 1\n"},
                )


    def test_failed_late_hook_blocks_inline_fallback(self):
        with tempfile.TemporaryDirectory(prefix="xc0-iso-late-") as temp:
            self.assert_blocks(
                self.command("d-i preseed/late_command string "),
                Path(temp),
                {"cp": "#!/bin/sh\nexit 0\n", "in-target": "#!/bin/sh\nexit 1\n"},
            )


class LateBaseOnlyTests(unittest.TestCase):
    def setUp(self):
        self.late = (PRESEED / "late.sh").read_text(encoding="utf-8")

    def test_git_clone_and_bootstrap_are_next_step_text_only(self):
        forbidden = re.compile(
            r"(?:^|[\s;])(?:git|curl|wget)(?:\s|$)|setup/bootstrap\.sh"
        )
        printed_instructions = 0
        for line in self.late.splitlines():
            stripped = line.strip()
            if not forbidden.search(stripped):
                continue
            printed_instructions += 1
            self.assertTrue(
                stripped.startswith("printf "),
                f"possible executable network/bootstrap command: {stripped}",
            )
            self.assertNotRegex(stripped, r"[;&|`]|\$\(")
        self.assertGreaterEqual(printed_instructions, 3)
        self.assertIn("> /etc/xc0-next-steps", self.late)

    def test_every_base_install_marker_is_explicit(self):
        for state in ("provisioning", "failed", "complete"):
            self.assertIn(f"state={state}\\nstage=base\\n", self.late)
        self.assertIn("recommended_config_ref=%s", self.late)


class PublicKeyValidationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="xc0-iso-keys-")
        self.key_file = Path(self.tempdir.name) / "fixture.pub"

    def tearDown(self):
        self.tempdir.cleanup()

    def run_validator(self, text):
        self.key_file.write_text(text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(ISO / "validate-public-keys.py"), str(self.key_file)],
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
        )

    def test_generated_public_key_fixture_is_accepted(self):
        algorithm = b"ssh-ed25519"
        # Synthetic OpenSSH wire encoding; no private key was generated or read.
        blob = (
            struct.pack(">I", len(algorithm))
            + algorithm
            + struct.pack(">I", 32)
            + bytes(range(32))
        )
        public_key = "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")
        result = self.run_validator(public_key + " xc0-synthetic-fixture\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_key_header_and_malformed_key_are_rejected(self):
        cases = {
            "private header": (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n"
                "-----END OPENSSH PRIVATE KEY-----\n",
                "private keys",
            ),
            "malformed key": ("ssh-ed25519 %%% synthetic\n", "malformed SSH public key"),
        }
        for name, (text, error) in cases.items():
            with self.subTest(case=name):
                result = self.run_validator(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)


class BuildValidationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="xc0-iso-build-")
        self.dir = Path(self.tempdir.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.tool_log = self.dir / "unexpected-tools.log"
        self.source = self.dir / "source.iso"
        self.source.write_bytes(b"synthetic non-ISO input")
        (self.bin / "python3").symlink_to(sys.executable)
        for tool in ("xorriso", "cpio", "gzip", "git", "md5sum", "sha256sum"):
            make_executable(
                self.bin / tool,
                "#!/bin/sh\n"
                f"printf '%s\\n' '{tool}' >> \"$XC0_TOOL_LOG\"\n"
                "exit 99\n",
            )
        self.target = (
            "--target-serial",
            "TARGET",
            "--target-dmi",
            "Test Product",
        )
        self.pinned = self.target + ("--stage2-ref", REF)
        self.model_target = ("--hostname", "thor", "--target-model", TARGET_MODEL,
                             "--protected-model", PROTECTED_MODEL)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_build(self, *args, extra_env=None, profile="stable"):
        env = os.environ.copy()
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["XC0_TOOL_LOG"] = str(self.tool_log)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["sh", str(ISO / "build.sh"), str(self.source), profile, *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
        )

    def assert_no_repack_tools_ran(self):
        self.assertFalse(self.tool_log.exists())

    def test_release_requires_serial_and_dmi(self):
        options = (
            (),
            ("--target-serial", "TARGET"),
            ("--target-dmi", "Test Product"),
        )
        for extra in options:
            with self.subTest(options=extra):
                result = self.run_build(*extra, "--stage2-ref", REF)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("needs target serial AND DMI", result.stderr)
                self.assert_no_repack_tools_ran()

    def test_release_refuses_embedded_secrets(self):
        result = self.run_build(*self.pinned, "--secrets-dir", str(self.dir / "secrets"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refuses embedded credentials", result.stderr)
        self.assert_no_repack_tools_ran()

    def test_release_rejects_the_wrong_suite_for_each_named_host(self):
        for host, profile in (("loki", "stable"), ("thor", "testing")):
            result = self.run_build(*self.target, "--hostname", host, profile=profile)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("loki requires testing; thor requires stable", result.stderr)
            self.assert_no_repack_tools_ran()

    def test_model_policy_is_thor_only_and_requires_exactly_one_complete_policy(self):
        options = (
            ("--target-model", TARGET_MODEL, "--protected-model", PROTECTED_MODEL),
            ("--hostname", "loki", "--target-model", TARGET_MODEL, "--protected-model", PROTECTED_MODEL),
            ("--hostname", "thor", "--target-model", TARGET_MODEL),
            ("--hostname", "thor", "--protected-model", PROTECTED_MODEL),
            self.model_target + self.target,
            self.model_target + ("--target-serial", "TARGET"),
            self.target + ("--target-model", TARGET_MODEL),
        )
        for extra in options:
            with self.subTest(options=extra):
                result = self.run_build(*extra, "--stage2-ref", REF)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, "requires --hostname thor|exactly one complete policy|loki requires testing")
                self.assert_no_repack_tools_ran()

    def test_empty_values_and_unsafe_models_are_rejected(self):
        for option in ("--target-serial", "--target-dmi", "--target-model", "--protected-model", "--hostname"):
            with self.subTest(option=option):
                result = self.run_build(*self.pinned, option, "")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("empty value", result.stderr)
                self.assert_no_repack_tools_ran()
        for model in (" ", "WDS*", "WDS'100", "WDS\n100", " WDS100", "WDS100 ", "x" * 129):
            with self.subTest(model=model):
                result = self.run_build(*self.model_target, "--target-model", model, "--stage2-ref", REF)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsupported model string", result.stderr)
                self.assert_no_repack_tools_ran()

    def bypass_source_checksum(self):
        (self.bin / "python3").unlink()
        make_executable(self.bin / "python3", "#!/bin/sh\n"
                        "case \"$1\" in */verify-source.py) exit 0 ;; esac\n"
                        f"exec {shlex.quote(sys.executable)} \"$@\"\n")

    def test_explicit_stage2_ref_does_not_bypass_clean_source_requirement(self):
        self.bypass_source_checksum()
        make_executable(self.bin / "git", "#!/bin/sh\n"
                        "[ \"$3\" = status ] || exit 99\n"
                        "printf ' M iso/preseed/select-disk.sh\\n'\n")
        result = self.run_build(*self.pinned)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("even with --stage2-ref", result.stderr)
        self.assert_no_repack_tools_ran()

    def test_failed_git_status_cannot_look_like_a_clean_tree(self):
        self.bypass_source_checksum()
        make_executable(self.bin / "git", "#!/bin/sh\nexit 1\n")
        result = self.run_build(*self.pinned)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot verify source tree cleanliness", result.stderr)
        self.assert_no_repack_tools_ran()

    def test_build_emits_matching_schema3_config_metadata_and_portable_checksum(self):
        # Exercise the real gzip/newc overlay. Only Git and ISO I/O are fixtures.
        for tool in ("cpio", "gzip", "md5sum", "sha256sum"):
            executable = shutil.which(tool)
            if executable is None:
                self.skipTest(f"requires build tool {tool}")
            (self.bin / tool).unlink()
            (self.bin / tool).symlink_to(executable)
        self.bypass_source_checksum()
        make_executable(self.bin / "git", "#!/bin/sh\n"
                        "case \"$3\" in status) exit 0 ;; cat-file) echo commit ;; "
                        f"rev-parse) echo {REF} ;; *) exit 99 ;; esac\n")
        stock = self.dir / "stock"
        stock.mkdir()
        (stock / "initrd.gz").write_bytes(gzip.compress(newc_archive(newc_entry("init", b"fixture"))))
        (stock / "grub.cfg").write_text("menuentry 'Debian' {\n}\n")
        (stock / "md5sum.txt").write_text("0" * 32 + "  ./boot/grub/grub.cfg\n" + "0" * 32 + "  ./stock\n")
        exported = self.dir / "exported"
        make_executable(self.bin / "xorriso", f"#!{sys.executable}\n" + """
import os, pathlib, shutil, sys
args = sys.argv[1:]
stock = pathlib.Path(os.environ['XC0_STOCK'])
exported = pathlib.Path(os.environ['XC0_EXPORTED'])
if '-extract' in args:
    originals = {'/install.amd/gtk/initrd.gz': 'initrd.gz',
                 '/boot/grub/grub.cfg': 'grub.cfg', '/md5sum.txt': 'md5sum.txt'}
    for index, arg in enumerate(args):
        if arg == '-extract':
            member, destination = args[index + 1:index + 3]
            source = stock / originals[member] if member in originals else exported / member.lstrip('/')
            shutil.copyfile(source, destination)
else:
    pathlib.Path(args[args.index('-outdev') + 1]).write_bytes(b'synthetic repacked ISO')
    for index, arg in enumerate(args):
        if arg == '-map':
            source, member = args[index + 1:index + 3]
            destination = exported / member.lstrip('/')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
""")
        credentials = self.dir / "credentials"
        credentials.mkdir()
        (credentials / "luks-passphrase").write_text("synthetic-throwaway-passphrase\n")
        (credentials / "user-password-hash").write_text("$6$synthetic-throwaway-hash\n")
        fixture_env = dict(XC0_STOCK=str(stock), XC0_EXPORTED=str(exported))
        policies = (("loki", "testing", "serial-dmi", self.target + ("--hostname", "loki")),
                    ("thor", "stable", "model-confirm", self.model_target),
                    ("xc0-stable-vm", "stable", "vm-test", ("--vm-test", "--secrets-dir", str(credentials))))
        for hostname, profile, target_mode, options in policies:
            with self.subTest(target_mode=target_mode):
                result = self.run_build(*options, "--stage2-ref", REF, "--output-dir", str(self.dir / hostname),
                                        extra_env=fixture_env, profile=profile)
                self.assertEqual(result.returncode, 0, result.stderr)
                iso = Path(result.stdout.strip())
                mode = "vm-test" if target_mode == "vm-test" else "release"
                self.assertEqual(iso.name, f"xc0-{hostname}-{profile}-{mode}-{REF[:12]}.iso")
                manifest = json.loads(Path(str(iso) + ".manifest.json").read_text())
                embedded = json.loads((exported / "xc0/artifact.json").read_text())
                self.assertEqual(embedded, {key: value for key, value in manifest.items() if key != "image_sha256"})
                self.assertEqual(embedded["target_mode"], target_mode)
                self.assertEqual(embedded["schema"], 3)
                self.assertEqual((embedded["marius_uid"], embedded["marius_gid"],
                                  embedded["xcloud_uid"], embedded["xcloud_gid"]),
                                 ("1001", "1001", "1000", "1000"))
                self.assertEqual(Path(str(iso) + ".sha256").read_text(),
                                 hashlib.sha256(iso.read_bytes()).hexdigest() + "  " + iso.name + "\n")
                spec = importlib.util.spec_from_file_location("xc0_built_artifact", ISO / "verify-artifact.py")
                verifier = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(verifier)
                config = verifier._read_initrd_config(exported / "xc0/initrd.gz")
                self.assertEqual(config, {key: value for key, value in embedded.items() if key != "schema"})
                env = dict(os.environ, **fixture_env)
                env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
                verified = subprocess.run([sys.executable, str(ISO / "verify-artifact.py"), str(iso),
                                           "--expect-hostname", hostname], env=env, text=True, capture_output=True,
                                          timeout=4, check=False)
                if target_mode == "vm-test":
                    self.assertNotEqual(verified.returncode, 0)
                    self.assertIn("only release", verified.stderr)
                else:
                    self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_release_refuses_unpinned_or_malformed_refs(self):
        for ref in ("main", "a" * 39, "A" * 40):
            with self.subTest(ref=ref):
                result = self.run_build(*self.target, "--stage2-ref", ref)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr.lower(), "stage2 ref|invalid commit")
                self.assert_no_repack_tools_ran()

    def test_vm_mode_requires_private_credentials(self):
        result = self.run_build("--vm-test", "--stage2-ref", REF)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs --secrets-dir", result.stderr)
        self.assert_no_repack_tools_ran()

    def test_vm_mode_rejects_missing_credential_files(self):
        credentials = self.dir / "credentials"
        credentials.mkdir()
        result = self.run_build("--vm-test", "--stage2-ref", REF, "--secrets-dir", str(credentials))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing VM credential file", result.stderr)
        self.assert_no_repack_tools_ran()

    def test_source_sha_mismatch_stops_before_repack(self):
        result = self.run_build(*self.pinned)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the pinned Debian release checksum", result.stderr)
        self.assert_no_repack_tools_ran()


def artifact_metadata(*, target_mode="serial-dmi", hostname="loki", mode="release"):
    model_policy = target_mode == "model-confirm"
    profile = "stable" if hostname == "thor" else "testing"
    return {
        "schema": 3,
        "iso_id": f"xc0-{hostname}-{profile}-{mode}-{REF[:12]}",
        "hostname": hostname,
        "mode": mode,
        "profile": profile,
        "layout": "thor" if hostname == "thor" else "loki",
        "marius_uid": "1001", "marius_gid": "1001",
        "xcloud_uid": "1000", "xcloud_gid": "1000",
        "target_mode": target_mode,
        "target_serial": "" if model_policy else ("XC0_TEST_DISK" if mode == "vm-test" else "TARGET"),
        "target_dmi": "" if model_policy else ("QEMU" if mode == "vm-test" else "Test Product"),
        "target_model": TARGET_MODEL if model_policy else "",
        "protected_model": PROTECTED_MODEL if model_policy else "",
        "config_ref": REF,
    }


def install_config(metadata):
    values = {key.upper(): value for key, value in metadata.items() if key not in ("schema", "config_ref")}
    values["STAGE2_REF"] = metadata["config_ref"]
    return "".join(key + "='" + value.replace("'", "'\\''") + "'\n"
                   for key, value in values.items()).encode("ascii")


def newc_entry(name, data=b"", *, mode=stat.S_IFREG | 0o644, links=1):
    name = name.encode("utf-8") + b"\0"
    fields = (1, mode, 0, 0, links, 0, len(data), 0, 0, 0, 0, len(name), 0)
    header = b"070701" + b"".join(f"{field:08x}".encode("ascii") for field in fields)
    return (header + name + b"\0" * (-(110 + len(name)) % 4)
            + data + b"\0" * (-len(data) % 4))


def newc_archive(*entries):
    data = b"".join(entries) + newc_entry("TRAILER!!!", mode=0)
    return data + b"\0" * (-len(data) % 512)


class ArtifactPolicyTests(unittest.TestCase):
    """Use synthetic gzip/newc members; never source config or execute initrd code."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xc0-policy-artifact-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.iso = self.dir / "fixture.iso"
        self.iso.write_bytes(b"synthetic ISO fixture")
        self.embedded = self.dir / "embedded.json"
        self.manifest = Path(str(self.iso) + ".manifest.json")
        self.initrd = self.dir / "initrd.gz"
        self.log = self.dir / "extractions.log"
        self.metadata = artifact_metadata()
        self.write_metadata()
        self.write_initrd()
        make_executable(self.bin / "xorriso", "#!/bin/sh\nset -eu\n"
                        "printf '%s\\n' \"$6\" >> \"$XC0_EXTRACTIONS\"\n"
                        "[ \"$1\" = -osirrox ] && [ \"$2\" = on ] && [ \"$3\" = -indev ] && [ \"$5\" = -extract ]\n"
                        "case \"$6\" in\n"
                        "  /xc0/artifact.json) cp -- \"$XC0_EMBEDDED\" \"$7\" ;;\n"
                        "  /xc0/initrd.gz) cp -- \"$XC0_INITRD\" \"$7\" ;;\n"
                        "  *) exit 99 ;;\nesac\n")

    def write_metadata(self, metadata=None, embedded=None):
        metadata = self.metadata if metadata is None else metadata
        self.embedded.write_text(json.dumps(metadata if embedded is None else embedded))
        self.manifest.write_text(json.dumps(dict(metadata, image_sha256=hashlib.sha256(self.iso.read_bytes()).hexdigest())))

    def write_initrd(self, config=None, *, entries=None, original=None):
        if config is None:
            config = install_config(self.metadata)
        if entries is None:
            entries = [newc_entry("./xc0", mode=stat.S_IFDIR | 0o755),
                       newc_entry("./xc0/install.conf", config)]
        if original is None:
            original = newc_archive(newc_entry(".", mode=stat.S_IFDIR | 0o755),
                                    newc_entry("init", b"never execute this fixture"))
        self.initrd.write_bytes(gzip.compress(original) + gzip.compress(newc_archive(*entries)))

    def run_verify(self, *args):
        env = os.environ.copy()
        env.update(PATH=str(self.bin) + os.pathsep + env["PATH"],
                   XC0_EXTRACTIONS=str(self.log), XC0_EMBEDDED=str(self.embedded), XC0_INITRD=str(self.initrd))
        return subprocess.run([sys.executable, str(ISO / "verify-artifact.py"), str(self.iso), *args],
                              env=env, text=True, capture_output=True, timeout=4, check=False)

    def assert_rejected(self, fragment=None):
        result = self.run_verify()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        if fragment is not None:
            self.assertIn(fragment, result.stderr)

    def test_release_policies_bind_json_and_independently_extracted_config(self):
        for target_mode, hostname in (("serial-dmi", "loki"), ("model-confirm", "thor")):
            with self.subTest(target_mode=target_mode):
                self.metadata = artifact_metadata(target_mode=target_mode, hostname=hostname)
                self.write_metadata()
                self.write_initrd()
                result = self.run_verify("--expect-hostname", hostname)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.run_verify().returncode, 0)
        extractions = self.log.read_text().splitlines()
        self.assertEqual(set(extractions), {"/xc0/artifact.json", "/xc0/initrd.gz"})

    def test_expected_hostname_is_enforced_and_validated(self):
        for hostname in ("thor", "", "../loki", "Loki"):
            with self.subTest(hostname=hostname):
                result = self.run_verify("--expect-hostname", hostname)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("hostname", result.stderr)

    def test_expected_profile_and_fixed_numeric_map_are_enforced(self):
        self.assertEqual(self.run_verify("--expect-profile", "testing").returncode, 0)
        result = self.run_verify("--expect-profile", "stable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected profile", result.stderr)
        for key in ("marius_uid", "marius_gid", "xcloud_uid", "xcloud_gid"):
            for value in ("0", "01001", "1002", 1001, True):
                with self.subTest(key=key, value=value):
                    self.write_metadata(dict(self.metadata, **{key: value}))
                    self.assert_rejected()

    def test_old_schema_unknown_fields_empty_and_mixed_policies_are_rejected(self):
        changes = (
            {"schema": 1}, {"schema": 2}, {"schema": True}, {"extra": "field"}, {"hostname": ""},
            {"iso_id": "xc0-legacy"}, {"mode": "unknown"}, {"profile": "unknown"},
            {"target_mode": "unknown"}, {"target_serial": ""}, {"target_dmi": ""},
            {"target_dmi": "   "}, {"target_model": TARGET_MODEL}, {"config_ref": "A" * 40},
            {"target_serial": False},
        )
        for change in changes:
            with self.subTest(change=change):
                self.write_metadata(dict(self.metadata, **change))
                self.assert_rejected()
        model_metadata = artifact_metadata(target_mode="model-confirm", hostname="thor")
        for change in ({"hostname": "loki"}, {"target_model": ""}, {"protected_model": ""},
                       {"target_model": "*"}, {"protected_model": " "},
                       {"target_serial": "TARGET"}, {"target_dmi": "Test Product"}):
            with self.subTest(change=change):
                self.write_metadata(dict(model_metadata, **change))
                self.assert_rejected()

    def test_manifest_and_embedded_metadata_cannot_disagree(self):
        alternate = artifact_metadata(target_mode="model-confirm", hostname="thor")
        self.write_metadata(embedded=alternate)
        self.assert_rejected("does not match the manifest")

    def test_vm_test_artifacts_are_not_publishable(self):
        self.write_metadata(artifact_metadata(mode="vm-test", target_mode="vm-test"))
        self.assert_rejected("only release")

    def test_duplicate_json_fields_and_wrong_image_digest_are_rejected(self):
        for path in (self.manifest, self.embedded):
            with self.subTest(path=path.name):
                self.write_metadata()
                path.write_text(path.read_text().replace('"schema": 3', '"schema": 3, "schema": 3'))
                self.assert_rejected("duplicate JSON field")
        self.write_metadata()
        self.iso.write_bytes(b"tampered ISO fixture")
        self.assert_rejected("SHA256")

    def test_each_config_field_is_checked_against_metadata(self):
        for key in self.metadata.keys() - {"schema"}:
            with self.subTest(key=key):
                values = dict(self.metadata)
                values[key] += "changed"
                self.write_initrd(install_config(values))
                self.assert_rejected("install.conf does not match")

    def test_config_must_be_unique_literal_assignments_never_shell_code(self):
        sentinel = self.dir / "should-not-exist"
        original = install_config(self.metadata)
        configs = (
            original + b"MODE='release'\n",
            original + b"UNKNOWN='value'\n",
            original.replace(b"TARGET_SERIAL='TARGET'\n", b""),
            original + f"touch {sentinel}\n".encode(),
            original.replace(b"MODE='release'", f"MODE=\"$(touch {sentinel})\"".encode()),
            original.replace(b"MODE='release'", b"MODE=release"),
        )
        for config in configs:
            with self.subTest(config=config):
                self.write_initrd(config)
                self.assert_rejected("install.conf")
                self.assertFalse(sentinel.exists())

    def test_missing_duplicate_linked_and_ambiguous_cpio_entries_are_rejected(self):
        config = install_config(self.metadata)
        config_entry = newc_entry("./xc0/install.conf", config)
        cases = (
            [],
            [config_entry, config_entry],
            [config_entry, newc_entry("xc0/install.conf", config)],
            [newc_entry("xc0/install.conf", config, links=2)],
            [newc_entry("xc0/install.conf", b"/somewhere", mode=stat.S_IFLNK | 0o777)],
            [newc_entry("xc0", b"/elsewhere", mode=stat.S_IFLNK | 0o777), config_entry],
            [config_entry, newc_entry("xc0/install.conf/child")],
        )
        for entries in cases:
            with self.subTest(entries=len(entries)):
                self.write_initrd(entries=entries)
                self.assert_rejected()
        for name in ("/xc0/install.conf", "../xc0/install.conf", "xc0//install.conf", "././xc0/install.conf"):
            with self.subTest(name=name):
                self.write_initrd(entries=[newc_entry(name, config)])
                self.assert_rejected("ambiguous cpio pathname")
        self.write_initrd(original=newc_archive(config_entry))
        self.assert_rejected("duplicate install.conf")

    def test_corrupt_gzip_cpio_and_incomplete_archives_are_rejected(self):
        config = install_config(self.metadata)
        entry = newc_entry("xc0/install.conf", config)
        corrupt = (
            b"not gzip", gzip.compress(entry), gzip.compress(b"070701"),
            gzip.compress(newc_archive(entry).replace(b"070701", b"070702", 1)),
            gzip.compress(newc_archive(entry))[:-4],
            gzip.compress(newc_archive(entry) + b"garbage"),
        )
        for data in corrupt:
            with self.subTest(data=data[:20]):
                self.initrd.write_bytes(data)
                self.assert_rejected()

    def test_initrd_parsing_is_size_and_entry_bounded(self):
        spec = importlib.util.spec_from_file_location("xc0_safety_artifact", ISO / "verify-artifact.py")
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        for limit, value in (("MAX_INITRD_BYTES", 1), ("MAX_UNPACKED_BYTES", 128),
                             ("MAX_CPIO_ENTRIES", 1), ("MAX_METADATA_BYTES", 1)):
            with self.subTest(limit=limit), mock.patch.object(verifier, limit, value):
                with self.assertRaises(ValueError):
                    verifier._read_initrd_config(self.initrd)


class LateExecutionTests(unittest.TestCase):
    """Execute the finish script against a temporary fake target, never host /etc."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xc0-late-fixture-")
        self.root = Path(self.temporary.name)
        for name in ("root", "etc/sudoers.d", "etc/apt/sources.list.d", "var/log", "var/lib", "var/tmp",
                     "boot/grub", "boot/EFI/debian", "boot/efi/EFI/debian", "usr/lib/shim",
                     "usr/lib/grub/x86_64-efi-signed", "bin"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.config = self.root / "root/xc0-install.conf"
        self.write_config()
        (self.root / "root/xc0-authorized_keys").write_text("")
        (self.root / "root/xc0-accounts.sh").write_text(
            '#!/bin/sh\nprintf "%s\\n" "${1:-prepare}" >> "$XC0_ACCOUNT_LOG"\n')
        (self.root / "boot/initrd.img-fixture").write_bytes(b"synthetic initramfs")
        (self.root / "boot/grub/grub.cfg").write_text("linux /vmlinuz rootflags=subvol=@ quiet\n")
        for directory in ("boot/EFI/debian", "boot/efi/EFI/debian"):
            for image in ("shimx64.efi", "grubx64.efi"):
                (self.root / directory / image).write_bytes(b"synthetic signed-image fixture")
        for payload in ("usr/lib/shim/shimx64.efi.signed", "usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed"):
            (self.root / payload).write_bytes(b"synthetic signed-image fixture")
        (self.root / "etc/os-release").write_text("ID=debian\nVERSION_CODENAME=forky\n")
        text = (PRESEED / "late.sh").read_text()
        paths = ("/root/xc0-install.conf", "/root/xc0-authorized_keys", "/root/xc0-accounts.sh",
                 "/var/lib/xc0", "/var/log/xc0-install.log", "/etc/xc0-install",
                 "/etc/xc0-next-steps", "/etc/os-release", "/etc/apt/sources.list.d/debian.sources", "/etc/apt/sources.list",
                 "/etc/sudoers.d", "/home/marius/.ssh", "/boot", "/var/tmp/",
                 "/usr/lib/shim/shimx64.efi.signed", "/usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed")
        text = re.sub(r"(?<![a-zA-Z0-9_.-])(?:" + "|".join(re.escape(path) for path in sorted(paths, key=len, reverse=True)) + ")",
                      lambda match: str(self.root) + match[0], text)
        self.script = self.root / "late.sh"
        self.script.write_text(text)
        for command in ("usermod", "update-grub", "update-initramfs", "dpkg", "dpkg-query", "visudo"):
            make_executable(self.root / "bin" / command, "#!/bin/sh\nexit 0\n")
        for command in ("grub-probe", "findmnt"):
            make_executable(self.root / "bin" / command, "#!/bin/sh\nprintf 'fixture-boot-uuid\\n'\n")
        make_executable(self.root / "bin/unmkinitramfs", '''#!/bin/sh
set -eu
mkdir -p "$2/main/cryptroot"
if [ "${XC0_MISSING_CRYPTROOT:-0}" = 0 ]; then
    printf 'xcloud_root_crypt UUID=fixture none luks\\n' > "$2/main/cryptroot/crypttab"
fi
''')
        make_executable(self.root / "bin/apt-get", """#!/bin/sh
printf '%s\\n' "$*" >> "$XC0_APT_LOG"
case " $* " in *' update '*) [ "${XC0_FAIL_UPDATE:-0}" = 0 ] || exit 100 ;; esac
exit 0
""")

    def tearDown(self):
        self.temporary.cleanup()

    def write_config(self, hostname="fixture", profile="testing", layout="loki"):
        self.config.write_text(f"MODE=release\nPROFILE={profile}\nHOSTNAME={hostname}\nLAYOUT={layout}\n"
                               f"MARIUS_UID=1001\nMARIUS_GID=1001\nXCLOUD_UID=1000\nXCLOUD_GID=1000\n"
                               f"STAGE2_REF={REF}\nISO_ID=fixture\n")

    def run_late(self, failed_update=False, **changes):
        env = os.environ.copy()
        env["PATH"] = str(self.root / "bin") + os.pathsep + env["PATH"]
        env["XC0_APT_LOG"] = str(self.root / "apt.log")
        env["XC0_FAIL_UPDATE"] = "1" if failed_update else "0"
        env["XC0_ACCOUNT_LOG"] = str(self.root / "accounts.log")
        env.update(changes)
        return subprocess.run(["sh", str(self.script)], env=env, capture_output=True,
                              text=True, timeout=5)

    def assert_failed_without_success_alias(self, result):
        self.assertNotEqual(result.returncode, 0)
        status = (self.root / "var/lib/xc0/install-status").read_text()
        self.assertIn("state=failed\nstage=base\n", status)
        self.assertNotIn("state=complete", status)
        self.assertEqual((self.root / "etc/xc0-install").read_text(), status)

    def test_index_failure_cannot_report_testing_complete(self):
        self.assert_failed_without_success_alias(self.run_late(failed_update=True))
        commands = (self.root / "apt.log").read_text()
        self.assertIn("APT::Update::Error-Mode=any", commands)
        self.assertNotIn("full-upgrade", commands)

    def test_upgrade_without_expected_installed_suite_fails(self):
        (self.root / "etc/os-release").write_text("ID=debian\nVERSION_CODENAME=trixie\n")
        self.assert_failed_without_success_alias(self.run_late())

    def test_next_steps_write_failure_cannot_leave_a_complete_alias(self):
        (self.root / "etc/xc0-next-steps").mkdir()
        self.assert_failed_without_success_alias(self.run_late())

    def test_base_success_is_published_after_next_steps(self):
        result = self.run_late()
        self.assertEqual(result.returncode, 0, result.stderr)
        status = (self.root / "var/lib/xc0/install-status").read_text()
        self.assertIn("state=complete\nstage=base\n", status)
        self.assertTrue((self.root / "etc/xc0-install").is_symlink())
        self.assertEqual((self.root / "etc/xc0-install").read_text(), status)
        self.assertIn(REF, (self.root / "etc/xc0-next-steps").read_text())
        self.assertIn("layout=loki\nmarius=1001:1001\nxcloud=1000:1000\n", status)
        self.assertEqual((self.root / "accounts.log").read_text().splitlines(), ["prepare", "--check"])
        self.assertEqual((self.root / "boot/EFI/debian/grub.cfg").read_text(),
                         "search --no-floppy --fs-uuid --set=root fixture-boot-uuid\n"
                         "set prefix=($root)/grub\nconfigfile $prefix/grub.cfg\n")
        for path in (self.root / "var/lib/xc0", self.root / "var/lib/xc0/install-status",
                     self.root / "etc/xc0-next-steps"):
            self.assertTrue(path.stat().st_mode & 0o004, f"login user cannot read {path}")

    def test_next_steps_are_hostname_specific_and_dry_run_precedes_apply(self):
        for hostname, profile in (("loki", "loki"), ("thor", "thor"), ("other", "testing")):
            with self.subTest(hostname=hostname):
                suite = "stable" if hostname == "thor" else "testing"
                layout = "thor" if hostname == "thor" else "loki"
                self.write_config(hostname, suite, layout)
                (self.root / "etc/os-release").write_text(
                    "ID=debian\nVERSION_CODENAME=" + ("trixie" if suite == "stable" else "forky") + "\n")
                result = self.run_late()
                self.assertEqual(result.returncode, 0, result.stderr)
                instructions = (self.root / "etc/xc0-next-steps").read_text()
                command = f"sudo bash setup/bootstrap.sh --profile {profile} --user marius"
                if hostname == "thor":
                    command += " --nvidia-prepared"
                    self.assertIn("PREREQUISITE", instructions)
                    self.assertIn("NVIDIA driver/kernel pair", instructions)
                    self.assertLess(instructions.index("PREREQUISITE"), instructions.index(command))
                else:
                    self.assertNotIn("--nvidia-prepared", instructions)
                self.assertIn(command + " --dry-run\n", instructions)
                self.assertIn(command + "\n", instructions)
                self.assertLess(instructions.index(command + " --dry-run\n"), instructions.index(command + "\n"))

    def test_missing_cryptroot_or_wrong_grub_never_completes(self):
        self.assert_failed_without_success_alias(self.run_late(XC0_MISSING_CRYPTROOT="1"))
        (self.root / "boot/grub/grub.cfg").write_text("linux /vmlinuz rootflags=subvol=@rootfs\n")
        self.assert_failed_without_success_alias(self.run_late())

    def test_testing_disables_the_stock_deb822_source_before_upgrade(self):
        source = self.root / "etc/apt/sources.list.d/debian.sources"
        source.write_text("Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie\n")
        result = self.run_late()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(source.exists())
        self.assertIn("Suites: trixie", (self.root / "var/lib/xc0/debian.sources.installer").read_text())

    def test_wrong_signed_payload_never_completes(self):
        (self.root / "boot/EFI/debian/grubx64.efi").write_bytes(b"wrong loader")
        self.assert_failed_without_success_alias(self.run_late())

    def test_empty_boot_uuid_probes_never_complete(self):
        for command in ("grub-probe", "findmnt"):
            make_executable(self.root / "bin" / command, "#!/bin/sh\nexit 0\n")
        self.assert_failed_without_success_alias(self.run_late())


if __name__ == "__main__":
    unittest.main()
