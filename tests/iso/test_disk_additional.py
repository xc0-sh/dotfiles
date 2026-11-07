"""Additional disk-gate regressions using fake sysfs and debconf only."""

import os
import shlex
import unittest
from unittest import mock

import test_iso_safety as safety


CONFIRMATIONS = (
    "partman-lvm/confirm",
    "partman-lvm/confirm_nooverwrite",
    "partman-partitioning/confirm_write_new_label",
    "partman-crypto/confirm",
    "partman-crypto/confirm_nooverwrite",
    "partman/confirm",
    "partman/confirm_nooverwrite",
)
PERSISTENT_ANSWERS = (
    "partman-auto/method",
    "partman-auto/choose_recipe",
    "partman-auto/disk",
    "grub-installer/bootdev",
    "partman-lvm/device_remove_lvm",
    "partman-md/device_remove_md",
    "partman/choose_partition",
)


class DiskAdditionalTests(unittest.TestCase):
    def setUp(self):
        self.disk = safety.DiskScriptTests("test_duplicate_serial_is_rejected")
        self.disk.setUp()
        self.addCleanup(self.disk.tearDown)
        self.configure()

    def set_layout(self, layout):
        config = self.disk.xc0 / "install.conf"
        lines = [line for line in config.read_text().splitlines()
                 if not line.startswith("LAYOUT=")]
        if layout is not None:
            lines.append("LAYOUT=" + shlex.quote(layout))
        config.write_text("\n".join(lines) + "\n")

    def set_answer(self, question, value):
        with self.disk.debconf_log.open("a") as log:
            log.write(question + "=" + value + "\n")

    def configure(self, layout="loki", **kwargs):
        kwargs.setdefault("hostname", layout)
        self.disk.configure(**kwargs)
        self.set_layout(layout)
        self.set_answer("partman-auto/method", "crypto")
        self.set_answer("partman-auto/choose_recipe", f"xc0-{layout}-crypto-btrfs")

    def authorize(self):
        self.disk.add_disk("nvme0n1", "TARGET")
        result = self.disk.run_select(confirmation="ERASE TARGET")
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.disk.marker.read_text()

    def assert_fresh_rejection(self, **kwargs):
        before = self.disk.answers()
        result = self.disk.run_select(confirmation="ERASE TARGET", **kwargs)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.disk.marker.exists())
        self.assertEqual(self.disk.answers(), before)
        self.assertNotIn("INPUT critical xc0/confirm-erase", self.disk.debconf_log.read_text())

    def assert_cached_rejection(self, marker, **kwargs):
        before = self.disk.answers()
        prompts = self.disk.debconf_log.read_text().count("INPUT critical xc0/confirm-erase")
        for args in (("--validate-authorized",), ()):
            with self.subTest(args=args):
                result = self.disk.run_select(*args, **kwargs)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.disk.answers(), before)
                self.assertEqual(self.disk.marker.read_text(), marker)
        self.assertEqual(
            self.disk.debconf_log.read_text().count("INPUT critical xc0/confirm-erase"), prompts
        )

    def test_serial_dmi_protected_disk_disables_both_removals_in_either_order(self):
        for target, protected in (("nvme0n1", "nvme1n1"), ("nvme1n1", "nvme0n1")):
            with self.subTest(target=target):
                self.disk.clear_disks()
                self.disk.clear_results()
                self.configure()
                self.disk.add_disk(target, "TARGET")
                self.disk.add_disk(protected, "KEEP", model=safety.PROTECTED_MODEL,
                                   sectors=3907029168)
                result = self.disk.run_select(confirmation="ERASE TARGET")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"partman-auto/disk=/dev/{target}", self.disk.answers())
                for question in ("partman-lvm/device_remove_lvm", "partman-md/device_remove_md"):
                    self.assertIn(question + "=false", self.disk.answers())
                    self.assertNotIn(question + "=true", self.disk.answers())
                self.assertEqual(self.disk.run_select("--validate-authorized").returncode, 0)
                for question in ("partman-lvm/device_remove_lvm", "partman-md/device_remove_md"):
                    self.set_answer(question, "true")
                    self.assert_cached_rejection(self.disk.marker.read_text())
                    self.set_answer(question, "false")

    def test_both_layouts_require_the_matching_recipe_and_bind_the_marker(self):
        for layout in ("loki", "thor"):
            with self.subTest(layout=layout):
                self.disk.clear_disks()
                self.disk.clear_results()
                self.configure(layout)
                marker = self.authorize()
                self.assertTrue(marker.rstrip().endswith("|" + layout))
                before = self.disk.answers()
                self.assertEqual(self.disk.run_select("--validate-authorized").returncode, 0)
                self.assertEqual(self.disk.run_select().returncode, 0)
                self.assertEqual(self.disk.answers(), before)
                self.assertEqual(self.disk.debconf_log.read_text().count(
                    "INPUT critical xc0/confirm-erase"), 1)

    def test_vm_policy_keeps_its_identity_gate_and_protects_data_under_both_layouts(self):
        for layout in ("loki", "thor"):
            with self.subTest(layout=layout):
                self.disk.clear_disks()
                self.disk.clear_results()
                self.configure(layout, mode="vm-test", hostname="xc0-test")
                self.disk.add_disk("vda", "XC0_TEST_DISK")
                self.disk.add_disk("nvme0n1", "KEEP", model=safety.PROTECTED_MODEL)
                result = self.disk.run_select()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("partman-auto/disk=/dev/vda", self.disk.answers())
                self.assertIn("partman-lvm/device_remove_lvm=false", self.disk.answers())
                self.assertIn("partman-md/device_remove_md=false", self.disk.answers())
                self.assertEqual(self.disk.run_select("--validate-authorized").returncode, 0)

    def test_model_confirm_still_requires_the_exact_live_serial(self):
        self.configure("thor", target_mode="model-confirm", serial="", dmi="",
                       model=safety.TARGET_MODEL, protected=safety.PROTECTED_MODEL)
        self.disk.set_dmi("Physical Thor")
        self.disk.add_disk("nvme0n1", "LIVE", model=safety.FULL_MODEL, sectors=safety.SECTORS)
        before = self.disk.answers()
        for confirmation in (None, "", "ERASE TARGET", "ERASE live", "ERASE LIVE "):
            with self.subTest(confirmation=confirmation):
                result = self.disk.run_select(confirmation=confirmation)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.disk.marker.exists())
                self.assertEqual(self.disk.answers(), before)
        result = self.disk.run_select(confirmation="ERASE LIVE")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("partman-auto/disk=/dev/nvme0n1", self.disk.answers())
        self.assertEqual(self.disk.run_select("--validate-authorized").returncode, 0)

    def test_missing_invalid_and_host_mismatched_layouts_reject_fresh_consent(self):
        self.disk.add_disk("nvme0n1", "TARGET")
        for layout in (None, "", "unknown", "thor"):
            with self.subTest(layout=layout):
                self.set_layout(layout)
                self.assert_fresh_rejection()

    def test_missing_invalid_and_host_mismatched_layouts_reject_cached_consent(self):
        marker = self.authorize()
        for layout in (None, "", "unknown", "thor"):
            with self.subTest(layout=layout):
                self.set_layout(layout)
                self.assert_cached_rejection(marker)

    def test_inherited_layout_cannot_substitute_for_missing_image_configuration(self):
        self.disk.add_disk("nvme0n1", "TARGET")
        self.set_layout(None)
        with mock.patch.dict(os.environ, {"LAYOUT": "loki"}):
            self.assert_fresh_rejection()

    def test_fresh_consent_rejects_missing_or_mismatched_method_and_recipe(self):
        self.disk.add_disk("nvme0n1", "TARGET")
        for question, wrong, expected in (
            ("partman-auto/method", "regular", "crypto"),
            ("partman-auto/choose_recipe", "xc0-thor-crypto-btrfs", "xc0-loki-crypto-btrfs"),
        ):
            for value in ("", wrong):
                with self.subTest(question=question, value=value):
                    self.set_answer(question, value)
                    self.assert_fresh_rejection()
                    self.set_answer(question, expected)

    def test_post_consent_mutation_of_every_newly_validated_answer_is_rejected(self):
        marker = self.authorize()
        answers = (
            ("partman-auto/method", "regular", "crypto"),
            ("partman-auto/choose_recipe", "xc0-thor-crypto-btrfs", "xc0-loki-crypto-btrfs"),
            ("partman-auto/disk", "/dev/nvme1n1", "/dev/nvme0n1"),
            ("grub-installer/bootdev", "/dev/nvme1n1", "/dev/nvme0n1"),
            ("partman-lvm/device_remove_lvm", "false", "true"),
            ("partman-md/device_remove_md", "false", "true"),
            ("partman/choose_partition", "manual", "finish"),
        )
        for question, wrong, expected in answers:
            for value in ("", wrong):
                with self.subTest(question=question, value=value):
                    self.set_answer(question, value)
                    self.assert_cached_rejection(marker)
                    self.set_answer(question, expected)

    def test_partman_consuming_confirmation_answers_does_not_revoke_disk_authorization(self):
        marker = self.authorize()
        for question in CONFIRMATIONS:
            self.set_answer(question, "false")  # partman-base db_reset after accepting the write
        for args in (("--validate-authorized",), ()):
            with self.subTest(args=args):
                result = self.disk.run_select(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.disk.marker.read_text(), marker)

    def test_wrong_get_for_every_newly_validated_answer_is_rejected(self):
        marker = self.authorize()
        for question in PERSISTENT_ANSWERS:
            with self.subTest(question=question):
                self.assert_cached_rejection(marker, wrong_get=question)

    def test_failed_get_cannot_pass_with_a_stale_correct_value(self):
        marker = self.authorize()
        text = self.disk.confmodule.read_text()
        self.assertEqual(text.count("db_get() {"), 1)
        text = text.replace("db_get() {", "xc0_db_get() {", 1)
        text += '''
db_get() {
    if [ "$1" = "${XC0_WRONG_DEBCONF_GET:-}" ]; then
        case "$1" in
            partman-auto/method) RET=crypto ;;
            partman-auto/choose_recipe) RET=$expected_recipe ;;
            partman/choose_partition) RET=finish ;;
            *) RET=true ;;
        esac
        return 1
    fi
    xc0_db_get "$@"
}
'''
        self.disk.confmodule.write_text(text)
        for question in PERSISTENT_ANSWERS:
            with self.subTest(question=question):
                self.assert_cached_rejection(marker, wrong_get=question)

    def test_layout_change_cannot_reuse_a_marker_even_with_a_matching_new_recipe(self):
        self.configure(hostname="xc0-test")
        marker = self.authorize()
        self.set_layout("thor")
        self.set_answer("partman-auto/choose_recipe", "xc0-thor-crypto-btrfs")
        before = self.disk.answers()
        result = self.disk.run_select("--validate-authorized")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.disk.answers(), before)
        self.assertEqual(self.disk.marker.read_text(), marker)
        result = self.disk.run_select()  # A new layout needs fresh exact consent.
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.disk.answers(), before)
        self.assertEqual(self.disk.marker.read_text(), marker)

    def test_fresh_authorization_rechecks_answers_before_publishing_the_marker(self):
        self.disk.add_disk("nvme0n1", "TARGET")
        text = self.disk.confmodule.read_text()
        trigger = '[ "$1" != "${XC0_FAIL_DEBCONF_SET:-}" ];'
        self.assertEqual(text.count(trigger), 1)
        text = text.replace(trigger,
                            'if [ "$1" = partman/choose_partition ]; then '
                            'printf "partman/confirm=false\\n" >> "$XC0_TEST_LOG"; fi; '
                            + trigger)
        self.disk.confmodule.write_text(text)
        result = self.disk.run_select(confirmation="ERASE TARGET")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.disk.marker.exists())


if __name__ == "__main__":
    unittest.main()
