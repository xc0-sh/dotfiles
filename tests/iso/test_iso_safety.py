import base64
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ISO = ROOT / "iso"
PRESEED = ISO / "preseed"
REF = "a" * 40


def make_executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class DiskScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="xc0-iso-disk-")
        self.dir = Path(self.tempdir.name)
        self.sysfs = self.dir / "sys"
        self.xc0 = self.dir / "xc0"
        self.bin = self.dir / "bin"
        self.marker = self.dir / "authorized"
        self.debconf_log = self.dir / "debconf.log"
        self.confmodule = self.dir / "confmodule"
        for directory in (
            self.sysfs / "block",
            self.sysfs / "class" / "dmi" / "id",
            self.xc0,
            self.bin,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.confmodule.write_text(
            "db_subst() { :; }\n"
            "db_register() { printf 'REGISTER %s %s\\n' \"$1\" \"$2\" >> \"$XC0_TEST_LOG\"; }\n"
            "db_fset() { printf 'FLAG %s %s %s\\n' \"$1\" \"$2\" \"$3\" >> \"$XC0_TEST_LOG\"; }\n"
            "db_set() { printf '%s=%s\\n' \"$1\" \"$2\" >> \"$XC0_TEST_LOG\"; [ \"$1\" != \"${XC0_FAIL_DEBCONF_SET:-}\" ]; }\n"
            "db_input() { :; }\n"
            "db_go() { :; }\n"
            "db_get() { if [ \"$1\" = xc0/confirm-erase ]; then RET=${XC0_CONFIRMATION-}; else RET=$(awk -F= -v key=\"$1\" '$1 == key { value=$2 } END { print value }' \"$XC0_TEST_LOG\"); fi; }\n",
            encoding="utf-8",
        )
        make_executable(self.bin / "debconf-loadtemplate", "#!/bin/sh\nexit 0\n")
        make_executable(self.bin / "logger", "#!/bin/sh\nexit 0\n")
        make_executable(
            self.bin / "debconf-set",
            "#!/bin/sh\n"
            "printf '%s=%s\\n' \"$1\" \"$2\" >> \"$XC0_TEST_LOG\"\n"
            "[ \"$1\" != \"${XC0_FAIL_DEBCONF_SET:-}\" ]\n",
        )
        for name in ("select-disk.sh", "00xc0-guard"):
            text = (PRESEED / name).read_text(encoding="utf-8")
            text = text.replace("/tmp/xc0-authorized", "@@XC0_MARKER@@")
            text = text.replace("/xc0", str(self.xc0))
            text = text.replace("/usr/share/debconf/confmodule", str(self.confmodule))
            text = text.replace("@@XC0_MARKER@@", str(self.marker))
            make_executable(self.xc0 / name, text)
        self.configure()

    def tearDown(self):
        self.tempdir.cleanup()

    def configure(self, mode="release", serial=None, dmi=None):
        serial = serial or ("XC0_TEST_DISK" if mode == "vm-test" else "TARGET")
        dmi = dmi or ("QEMU" if mode == "vm-test" else "Test Product")
        values = {
            "MODE": mode,
            "PROFILE": "testing",
            "HOSTNAME": "xc0-test",
            "TARGET_SERIAL": serial,
            "TARGET_DMI": dmi,
            "STAGE2_REF": REF,
            "ISO_ID": "xc0-test",
        }
        (self.xc0 / "install.conf").write_text(
            "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        self.set_dmi(dmi)

    def set_dmi(self, product):
        (self.sysfs / "class" / "dmi" / "id" / "product_name").write_text(
            product + "\n", encoding="utf-8"
        )

    def add_disk(self, name, serial, *, removable=False, usb=False):
        bus = self.sysfs / "devices" / "pci"
        if usb:
            bus /= "usb1"
        device = bus / name
        (device / "device").mkdir(parents=True)
        (device / "removable").write_text(f"{int(removable)}\n", encoding="utf-8")
        (device / "size").write_text("33554432\n", encoding="utf-8")
        (device / "device" / "model").write_text("Synthetic disk\n", encoding="utf-8")
        if serial is not None:
            (device / "serial").write_text(serial + "\n", encoding="utf-8")
        (self.sysfs / "block" / name).symlink_to(device)

    def clear_disks(self):
        for name in ("block", "devices"):
            path = self.sysfs / name
            if path.exists():
                shutil.rmtree(path)
        (self.sysfs / "block").mkdir()

    def clear_results(self):
        for path in (self.marker, self.debconf_log):
            if path.exists():
                path.unlink()

    def answers(self):
        if not self.debconf_log.exists():
            return []
        return [line for line in self.debconf_log.read_text(encoding="utf-8").splitlines()
                if not line.startswith(("REGISTER ", "FLAG "))]

    def run_hook(self, hook, args=(), confirmation=None, fail_debconf=None):
        env = os.environ.copy()
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["XC0_SYSFS"] = str(self.sysfs)
        env["XC0_TEST_LOG"] = str(self.debconf_log)
        env.pop("XC0_CONFIRMATION", None)
        env.pop("XC0_FAIL_DEBCONF_SET", None)
        if confirmation is not None:
            env["XC0_CONFIRMATION"] = confirmation
        if fail_debconf is not None:
            env["XC0_FAIL_DEBCONF_SET"] = fail_debconf
        return subprocess.run(
            ["sh", "-eu", str(self.xc0 / hook), *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
        )

    def run_select(self, *args, confirmation=None, fail_debconf=None):
        return self.run_hook(
            "select-disk.sh", args, confirmation=confirmation, fail_debconf=fail_debconf
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
            f"/dev/vda|XC0_TEST_DISK|vm-test|{REF}",
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

    def tearDown(self):
        self.tempdir.cleanup()

    def run_build(self, *args):
        env = os.environ.copy()
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["XC0_TOOL_LOG"] = str(self.tool_log)
        return subprocess.run(
            ["sh", str(ISO / "build.sh"), str(self.source), "stable", *args],
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


class LateExecutionTests(unittest.TestCase):
    """Execute the finish script against a temporary fake target, never host /etc."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xc0-late-fixture-")
        self.root = Path(self.temporary.name)
        for name in ("root", "etc/sudoers.d", "var/log", "var/lib", "bin"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.config = self.root / "root/xc0-install.conf"
        self.config.write_text("MODE=release\nPROFILE=testing\nSTAGE2_REF=" + REF + "\nISO_ID=fixture\n")
        (self.root / "root/xc0-authorized_keys").write_text("")
        (self.root / "etc/os-release").write_text("ID=debian\nVERSION_CODENAME=forky\n")
        text = (PRESEED / "late.sh").read_text()
        for path in ("/root/xc0-install.conf", "/root/xc0-authorized_keys",
                     "/var/lib/xc0", "/var/log/xc0-install.log", "/etc/xc0-install",
                     "/etc/xc0-next-steps", "/etc/os-release", "/etc/apt/sources.list",
                     "/etc/sudoers.d", "/home/marius/.ssh"):
            text = text.replace(path, str(self.root) + path)
        (self.root / "etc/apt").mkdir()
        self.script = self.root / "late.sh"
        self.script.write_text(text)
        for command in ("usermod", "update-grub", "dpkg", "dpkg-query", "visudo"):
            make_executable(self.root / "bin" / command, "#!/bin/sh\nexit 0\n")
        make_executable(self.root / "bin/apt-get", """#!/bin/sh
printf '%s\\n' "$*" >> "$XC0_APT_LOG"
case " $* " in *' update '*) [ "${XC0_FAIL_UPDATE:-0}" = 0 ] || exit 100 ;; esac
exit 0
""")

    def tearDown(self):
        self.temporary.cleanup()

    def run_late(self, failed_update=False):
        env = os.environ.copy()
        env["PATH"] = str(self.root / "bin") + os.pathsep + env["PATH"]
        env["XC0_APT_LOG"] = str(self.root / "apt.log")
        env["XC0_FAIL_UPDATE"] = "1" if failed_update else "0"
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
        for path in (self.root / "var/lib/xc0", self.root / "var/lib/xc0/install-status",
                     self.root / "etc/xc0-next-steps"):
            self.assertTrue(path.stat().st_mode & 0o004, f"login user cannot read {path}")


if __name__ == "__main__":
    unittest.main()
