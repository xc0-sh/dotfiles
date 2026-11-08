"""Deterministic artifact/media tests; never access real disks or libvirt."""

import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
ISO = ROOT / "iso"
TEMP_ROOT = Path("/tmp/opencode")
if not TEMP_ROOT.is_dir():
    TEMP_ROOT = Path(tempfile.gettempdir())


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


USB = load_script("xc0_test_usb_in_use", ISO / "usb-in-use.py")


class ArtifactVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xc0-media-artifact-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.iso = self.work / "reviewed.iso"
        self.iso.write_bytes(b"synthetic ISO bytes, not a bootable image")
        self.embedded_path = self.work / "embedded.json"
        self.initrd_path = self.work / "initrd.gz"
        self.xorriso_log = self.work / "xorriso.log"
        self.metadata = {
            "schema": 3,
            "iso_id": "xc0-loki-testing-release-" + "a" * 12,
            "hostname": "loki",
            "mode": "release",
            "profile": "testing",
            "layout": "loki",
            "marius_uid": "1001",
            "marius_gid": "1001",
            "xcloud_uid": "1000",
            "xcloud_gid": "1000",
            "target_mode": "serial-dmi",
            "target_serial": "REVIEWED_SERIAL",
            "target_dmi": "Reviewed product",
            "target_model": "",
            "protected_model": "",
            "config_ref": "a" * 40,
        }
        self.write_embedded(self.metadata)
        self.write_manifest(self.metadata)
        self.write_initrd(self.metadata)
        xorriso = self.bin / "xorriso"
        xorriso.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf '%s\\n' \"$*\" >> \"$XC0_XORRISO_LOG\"\n"
            "[ \"$1\" = -osirrox ] && [ \"$2\" = on ]\n"
            "[ \"$3\" = -indev ] && [ \"$5\" = -extract ]\n"
            "case \"$6\" in\n"
            "  /xc0/artifact.json) cp -- \"$XC0_EMBEDDED\" \"$7\" ;;\n"
            "  /xc0/initrd.gz) cp -- \"$XC0_INITRD\" \"$7\" ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        xorriso.chmod(0o755)

    @property
    def manifest_path(self):
        return Path(str(self.iso) + ".manifest.json")

    def write_embedded(self, metadata=None, raw=None):
        self.embedded_path.write_text(
            raw if raw is not None else json.dumps(metadata), encoding="utf-8"
        )

    def write_manifest(self, metadata=None, raw=None):
        if raw is None:
            manifest = dict(metadata)
            manifest["image_sha256"] = hashlib.sha256(self.iso.read_bytes()).hexdigest()
            raw = json.dumps(manifest)
        self.manifest_path.write_text(raw, encoding="utf-8")

    def write_initrd(self, metadata=None, raw=None):
        if raw is None:
            raw = "".join(
                f"{'STAGE2_REF' if key == 'config_ref' else key.upper()}='{value}'\n"
                for key, value in metadata.items() if key != "schema"
            ).encode("ascii")

        def cpio_entry(name, content=b"", mode=stat.S_IFREG | 0o600):
            encoded = name.encode("ascii") + b"\0"
            fields = (0, mode, 0, 0, 1, 0, len(content), 0, 0, 0, 0, len(encoded), 0)
            header = b"070701" + "".join(f"{field:08x}" for field in fields).encode("ascii")
            return (header + encoded + b"\0" * (-(len(header) + len(encoded)) % 4)
                    + content + b"\0" * (-len(content) % 4))

        trailer = cpio_entry("TRAILER!!!", mode=0)
        overlay = (cpio_entry(".", mode=stat.S_IFDIR | 0o755)
                   + cpio_entry("xc0", mode=stat.S_IFDIR | 0o755)
                   + cpio_entry("xc0/install.conf", raw) + trailer)
        # Match the builder's stock gzip member plus a separate overlay member.
        self.initrd_path.write_bytes(gzip.compress(trailer, mtime=0) + gzip.compress(overlay, mtime=0))

    def run_verify(self, expect_hostname=None, expect_profile=None):
        env = os.environ.copy()
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["TMPDIR"] = str(self.work)
        env["XC0_EMBEDDED"] = str(self.embedded_path)
        env["XC0_INITRD"] = str(self.initrd_path)
        env["XC0_XORRISO_LOG"] = str(self.xorriso_log)
        arguments = [sys.executable, str(ISO / "verify-artifact.py"), str(self.iso)]
        if expect_hostname is not None:
            arguments.extend(["--expect-hostname", expect_hostname])
        if expect_profile is not None:
            arguments.extend(["--expect-profile", expect_profile])
        return subprocess.run(
            arguments,
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
            env=env,
        )

    def test_release_requires_bound_manifest_and_checks_artifact_and_initrd_config(self):
        Path(str(self.iso) + ".sha256").write_text("0" * 64 + "  stale.iso\n")
        Path(str(self.iso) + ".manifest").write_text("mode=vm-test\n")
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        extraction = self.xorriso_log.read_text(encoding="utf-8")
        self.assertIn("/xc0/artifact.json", extraction)
        self.assertIn("/xc0/initrd.gz", extraction)
        self.assertEqual(len(extraction.splitlines()), 2)
        self.assertNotIn("secret", extraction)

    def test_stale_release_manifest_cannot_launder_vm_test_iso(self):
        vm_metadata = dict(
            self.metadata, mode="vm-test", target_mode="vm-test", target_serial="XC0_TEST_DISK",
            target_dmi="QEMU", iso_id="xc0-loki-testing-vm-test-" + "a" * 12,
        )
        self.write_embedded(vm_metadata)
        # The sidecar still says release and has the correct digest of these bytes.
        self.write_manifest(self.metadata)
        result = self.run_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)

        # Even an internally consistent VM-test pair is not a publishable release.
        self.write_manifest(vm_metadata)
        result = self.run_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only release", result.stderr)

    def test_duplicate_fields_are_rejected_in_sidecar_and_embedded_json(self):
        valid_manifest = self.manifest_path.read_text(encoding="utf-8")
        duplicate = valid_manifest.replace(
            '"schema": 3', '"schema": 3, "schema": 3', 1
        )
        self.manifest_path.write_text(duplicate, encoding="utf-8")
        result = self.run_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate JSON field", result.stderr)

        self.manifest_path.write_text(valid_manifest, encoding="utf-8")
        embedded = self.embedded_path.read_text(encoding="utf-8").replace(
            '"schema": 3', '"schema": 3, "schema": 3', 1
        )
        self.write_embedded(raw=embedded)
        result = self.run_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate JSON field", result.stderr)

    def test_manifest_digest_is_checked_against_actual_iso_bytes(self):
        self.iso.write_bytes(b"changed after the manifest was written")
        result = self.run_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA256", result.stderr)
        self.assertFalse(self.xorriso_log.exists())

    def test_exact_schema_and_field_values_are_enforced(self):
        bad_values = (
            {"schema": True},
            {"schema": 1},
            {"schema": 2},
            {"iso_id": ""},
            {"hostname": "thor"},
            {"mode": "Release"},
            {"profile": "unstable"},
            {"layout": "thor"},
            {"marius_uid": "1000"},
            {"marius_gid": "1000"},
            {"xcloud_uid": "1001"},
            {"xcloud_gid": "1001"},
            {"config_ref": "A" * 40},
            {"target_serial": ""},
            {"target_dmi": ""},
            {"target_mode": "model-confirm"},
            {"target_model": "WDS100T1X0E"},
        )
        for changes in bad_values:
            with self.subTest(changes=changes):
                self.write_manifest(dict(self.metadata, **changes))
                result = self.run_verify()
                self.assertNotEqual(result.returncode, 0)
        self.write_manifest(dict(self.metadata, unexpected="field"))
        self.assertNotEqual(self.run_verify().returncode, 0)
        self.write_manifest(self.metadata)
        self.write_embedded(dict(self.metadata, unexpected="field"))
        self.assertNotEqual(self.run_verify().returncode, 0)

    def test_missing_json_sidecar_is_not_replaced_by_legacy_manifest(self):
        self.manifest_path.unlink()
        Path(str(self.iso) + ".manifest").write_text("mode=release\n")
        result = self.run_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.xorriso_log.exists())

    def test_expected_hostname_is_checked_against_bound_embedded_identity(self):
        result = self.run_verify(expect_hostname="loki", expect_profile="testing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.xorriso_log.unlink()
        result = self.run_verify(expect_hostname="thor", expect_profile="stable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hostname does not match", result.stderr)
        self.assertNotIn("/xc0/initrd.gz", self.xorriso_log.read_text())

    def test_bound_metadata_cannot_hide_another_target_in_the_actual_initrd(self):
        self.write_initrd(dict(self.metadata, target_serial="OTHER_DISK"))
        result = self.run_verify(expect_hostname="loki")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install.conf does not match", result.stderr)

    def test_expected_profile_is_checked_against_the_bound_embedded_suite(self):
        result = self.run_verify(expect_hostname="loki", expect_profile="testing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.xorriso_log.unlink()
        result = self.run_verify(expect_hostname="loki", expect_profile="stable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("profile does not match expected profile", result.stderr)
        self.assertIn("/xc0/artifact.json", self.xorriso_log.read_text())
        self.assertNotIn("/xc0/initrd.gz", self.xorriso_log.read_text())

    def test_invalid_expected_profile_is_rejected_before_reading_the_iso(self):
        result = self.run_verify(expect_profile="unstable")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)
        self.assertFalse(self.xorriso_log.exists())

    def test_initrd_config_is_parsed_without_executing_shell_text(self):
        marker = self.work / "must-not-exist"
        self.write_initrd(raw=f"HOSTNAME=$(touch {marker})\n".encode("ascii"))
        result = self.run_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nonliteral shell syntax", result.stderr)
        self.assertFalse(marker.exists())


class UsbInUseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xc0-media-usb-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.sys_dev_block = self.work / "sys/dev/block"
        self.sys_dev_block.mkdir(parents=True)
        usb_device = self.work / "sys/devices/usb1/1-2"
        usb_device.mkdir(parents=True)
        (usb_device / "busnum").write_text("7\n")
        (usb_device / "devnum").write_text("9\n")
        (usb_device / "idVendor").write_text("abcd\n")
        self.sda = usb_device / "host/block/sda"
        self.sda.mkdir(parents=True)
        self.sda1 = self.sda / "sda1"
        self.sda1.mkdir()
        (self.sda1 / "partition").write_text("1\n")
        self.sdb = self.work / "sys/devices/pci/block/sdb"
        self.sdb.mkdir(parents=True)
        self.add_device(8, 0, self.sda)
        self.add_device(8, 1, self.sda1)
        self.add_device(8, 16, self.sdb)

        self.stats = {
            "/dev/sda": self.block_stat(8, 0),
            "/dev/sda1": self.block_stat(8, 1),
            "/dev/disk/by-id/usb-reviewed": self.block_stat(8, 0),
            "/dev/disk/by-id/usb-reviewed-part1": self.block_stat(8, 1),
            "/dev/sdb": self.block_stat(8, 16),
        }
        real_stat = os.stat

        def synthetic_stat(path, *args, **kwargs):
            key = os.fspath(path)
            if key in self.stats:
                return self.stats[key]
            return real_stat(path, *args, **kwargs)

        self.patches = [
            mock.patch.object(USB, "SYS_DEV_BLOCK", self.sys_dev_block),
            mock.patch.object(USB.os, "stat", side_effect=synthetic_stat),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.target = USB.target_disk("/dev/sda")

    @staticmethod
    def block_stat(major, minor):
        devno = os.makedev(major, minor)
        return SimpleNamespace(
            st_mode=stat.S_IFBLK | 0o600, st_rdev=devno, st_dev=devno
        )

    def add_device(self, major, minor, path):
        (self.sys_dev_block / f"{major}:{minor}").symlink_to(path)

    @staticmethod
    def disk_xml(source, attribute="dev"):
        return (
            "<domain><devices><disk type='block'>"
            f"<source {attribute}='{source}'/></disk></devices></domain>"
        )

    def test_whole_disk_partition_and_device_aliases_all_match_target(self):
        sources = (
            "/dev/sda",
            "/dev/sda1",
            "/dev/disk/by-id/usb-reviewed",
            "/dev/disk/by-id/usb-reviewed-part1",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(USB.domain_uses_target(self.disk_xml(source), self.target))
        self.assertFalse(USB.domain_uses_target(self.disk_xml("/dev/sdb"), self.target))

    def test_file_backed_disk_is_resolved_to_its_block_filesystem(self):
        source = self.work / "vm.qcow2"
        source.write_bytes(b"synthetic")
        self.stats[str(source)] = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_rdev=0,
            st_dev=os.makedev(8, 1),
        )
        self.assertTrue(
            USB.domain_uses_target(self.disk_xml(source, "file"), self.target)
        )

    def test_unresolvable_disk_source_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "cannot stat VM disk source"):
            USB.domain_uses_target(self.disk_xml("/dev/missing"), self.target)

    def test_usb_passthrough_matches_address_and_unknown_identity_refuses(self):
        matching = (
            "<domain><devices><hostdev mode='subsystem' type='usb'><source>"
            "<address bus='7' device='9'/></source></hostdev></devices></domain>"
        )
        unrelated = matching.replace("device='9'", "device='10'")
        unknown = (
            "<domain><devices><hostdev mode='subsystem' type='usb'><source>"
            "<vendor id='0xabcd'/><product id='0x1234'/>"
            "</source></hostdev></devices></domain>"
        )
        self.assertTrue(USB.domain_uses_target(matching, self.target))
        self.assertFalse(USB.domain_uses_target(unrelated, self.target))
        with self.assertRaisesRegex(ValueError, "identity is uncertain"):
            USB.domain_uses_target(unknown, self.target)

    def test_failed_virsh_list_is_checked_and_fatal(self):
        error = subprocess.CalledProcessError(1, ["virsh"], stderr="synthetic")
        with mock.patch.object(USB.subprocess, "run", side_effect=error) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                USB.attached_domains("/dev/sda")
        run.assert_called_once_with(
            ["virsh", "-c", "qemu:///system", "list", "--name"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_failed_dumpxml_is_checked_and_fatal(self):
        def run(command, **kwargs):
            self.assertTrue(kwargs["check"])
            if command[-2:] == ["list", "--name"]:
                return subprocess.CompletedProcess(command, 0, stdout="vm-one\n", stderr="")
            raise subprocess.CalledProcessError(1, command, stderr="synthetic")

        with mock.patch.object(USB.subprocess, "run", side_effect=run) as checked_run:
            with self.assertRaises(subprocess.CalledProcessError):
                USB.attached_domains("/dev/sda")
        self.assertEqual(checked_run.call_count, 2)
        self.assertEqual(
            checked_run.call_args.args[0],
            ["virsh", "-c", "qemu:///system", "dumpxml", "vm-one"],
        )

    def test_malformed_domain_xml_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "invalid libvirt domain XML"):
            USB.domain_uses_target("<domain>", self.target)


class CopyPreflightTests(unittest.TestCase):
    def test_both_usb_checksums_use_the_renamed_local_basename(self):
        script = (ISO / "copy-to-ventoy.sh").read_text(encoding="utf-8")
        marker = 'python3 - "$2.manifest.json" "$work/$1.sha256" "xc0-$1-installer.iso" <<\'PY\'\n'
        body = script.split(marker, 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            manifest = root / "source.iso.manifest.json"
            digest = "a" * 64
            manifest.write_text(json.dumps({"image_sha256": digest}))
            for host in ("loki", "thor"):
                with self.subTest(host=host):
                    checksum = root / f"{host}.sha256"
                    basename = f"xc0-{host}-installer.iso"
                    subprocess.run(
                        [sys.executable, "-", str(manifest), str(checksum), basename],
                        input=body, text=True, check=True, capture_output=True,
                    )
                    self.assertEqual(checksum.read_text(), digest + "  " + basename + "\n")

    def test_every_libvirt_rejection_precedes_mount_and_no_formatting_exists(self):
        script = (ISO / "copy-to-ventoy.sh").read_text(encoding="utf-8")
        usb_check = script.index('usb-in-use.py')
        first_mount = script.index('sudo -n mount')
        self.assertLess(usb_check, first_mount)
        self.assertIn('verify-artifact.py', script[:usb_check])
        self.assertIn('--expect-hostname loki --expect-profile testing "$loki"', script[:usb_check])
        self.assertIn('--expect-hostname thor --expect-profile stable "$thor"', script[:usb_check])
        self.assertIn('[ -b "$partition" ]', script[:usb_check])
        self.assertIn('[ -b "$parent" ]', script[:usb_check])
        self.assertIn('serial=$(lsblk -ndo SERIAL -- "$parent")', script[:usb_check])
        self.assertIn('[ "$serial" = "$usb_serial" ]', script[:usb_check])
        for formatter in ("mkfs", "wipefs", "fdisk", "parted", "dd if="):
            self.assertNotIn(formatter, script)


class CopyDeploymentTests(unittest.TestCase):
    """Run the real copy control flow against files and fail-closed command mocks."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xc0-media-copy-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.scripts = self.work / "iso"
        self.scripts.mkdir()
        self.temporary = self.work / "temporary"
        self.temporary.mkdir()
        self.media = self.work / "media"
        (self.media / "ventoy").mkdir(parents=True)
        (self.media / "xc0").mkdir()
        self.original_config = '{"previous": "keep the first backup"}\n'
        (self.media / "ventoy/ventoy.json").write_text(self.original_config)
        self.theme = self.work / "theme/xcloud"
        (self.theme / "icons").mkdir(parents=True)
        (self.theme / "theme.txt").write_text('desktop-image: "background.png"\n')
        (self.theme / "background.png").write_bytes(b"synthetic visual asset")
        (self.theme / "icons/debian.png").write_bytes(b"synthetic icon")
        self.loki = self.work / "reviewed-loki.iso"
        self.thor = self.work / "reviewed-thor.iso"
        for host, image in (("loki", self.loki), ("thor", self.thor)):
            image.write_bytes(f"synthetic {host} ISO, never bootable".encode())
            Path(str(image) + ".manifest.json").write_text(json.dumps({
                "schema": 3,
                "hostname": host,
                "profile": "testing" if host == "loki" else "stable",
                "layout": host,
                "marius_uid": "1001",
                "marius_gid": "1001",
                "xcloud_uid": "1000",
                "xcloud_gid": "1000",
                "config_ref": "a" * 40,
                "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            }))
            (self.media / f"xc0/xc0-{host}-installer.iso").write_bytes(b"previous release")
        self.partition = self.work / "partition"
        self.parent = self.work / "parent"
        self.partition.touch()
        self.parent.touch()
        self.log = self.work / "commands.jsonl"
        self.state = self.work / "mount-state"
        self.state.write_text("unmounted")
        self.write_copy_script()
        for name in ("ventoy.json", "ventoy_grub.cfg"):
            shutil.copyfile(ISO / name, self.scripts / name)

        # No production testing override: only these two block predicates are
        # replaced in a private script copy, after asserting their exact count.
        # Every privileged command is handled here; unknown ones fail closed.
        commands = textwrap.dedent(r'''
            import hashlib, json, os
            from pathlib import Path
            import signal, subprocess, sys

            tool = Path(sys.argv[0]).name
            args = sys.argv[1:]
            env = os.environ
            state = Path(env["XC0_MOCK_STATE"])
            mode = state.read_text()

            def record(name, arguments):
                with open(env["XC0_MOCK_LOG"], "a", encoding="utf-8") as log:
                    log.write(json.dumps({"tool": name, "args": arguments, "mode": mode}) + "\n")

            def refuse(message):
                sys.exit("mock refused: " + message)

            def inject_signal(event):
                if event not in env.get("XC0_SIGNAL_AT", "").split(","):
                    return
                marker = state.with_name("signal-" + event)
                if not marker.exists():
                    marker.touch()
                    record("signal", [event, env.get("XC0_SIGNAL", "TERM")])
                    # The mock is a direct child of the foreground copy shell.
                    os.kill(os.getppid(), getattr(signal, "SIG" + env.get("XC0_SIGNAL", "TERM")))

            if tool == "sudo":
                if not args or args.pop(0) != "-n":
                    refuse("sudo must be noninteractive")
                command = args.pop(0)
                if command == "python3":
                    sys.exit(subprocess.run([sys.executable, *args], check=False).returncode)
                record(command, args)
                if command == "blkid":
                    if args[-1] != env["XC0_MOCK_PARTITION"]:
                        refuse("unknown blkid target")
                    print(env.get("XC0_LABEL", "Ventoy") if args[1] == "LABEL"
                          else env.get("XC0_FILESYSTEM", "exfat"))
                elif command == "mount":
                    if (len(args) != 4 or args[0] != "-o" or
                            args[2] != env["XC0_MOCK_PARTITION"] or mode != "unmounted"):
                        refuse("unknown mount or already mounted")
                    target = Path(args[3])
                    target.rmdir()
                    target.symlink_to(env["XC0_MOCK_MEDIA"], target_is_directory=True)
                    access = "ro" if args[1].startswith("ro,") else "rw"
                    state.write_text(access)
                    inject_signal("mount-" + access)
                    if env.get("XC0_MOUNT_ERROR_AFTER_ATTACH") == access:
                        sys.exit(1)
                elif command == "umount":
                    if len(args) != 1 or mode == "unmounted":
                        refuse("unexpected unmount")
                    failed_once = state.with_suffix(".umount-failed")
                    if env.get("XC0_FAIL_UMOUNT_ONCE") and not failed_once.exists():
                        failed_once.touch()
                        sys.exit(1)
                    if env.get("XC0_FAIL_UMOUNT_ALWAYS"):
                        sys.exit(1)
                    if env.get("XC0_UMOUNT_STILL_MOUNTED"):
                        sys.exit(0)
                    target = Path(args[0])
                    target.unlink()
                    target.mkdir()
                    state.write_text("unmounted")
                    inject_signal("umount-" + mode)
                    if env.get("XC0_UMOUNT_ERROR_AFTER_DETACH"):
                        sys.exit(1)
                elif command == "blockdev":
                    if args != ["--flushbufs", env["XC0_MOCK_PARENT"]] or mode != "unmounted":
                        refuse("flush must use the unmounted parent")
                    if env.get("XC0_FAIL_FLUSH"):
                        sys.exit(1)
                    if env.get("XC0_CORRUPT_BACKUP_ON_FLUSH"):
                        (Path(env["XC0_MOCK_MEDIA"]) / "ventoy/ventoy.json.before-xc0").write_text(
                            "corrupted during flush\n")
                else:
                    refuse("unmocked privileged command " + command)
            elif tool == "realpath":
                record(tool, args)
                path = args[-1]
                print(env["XC0_MOCK_PARENT"] if path == "/dev/xc0-media-test-parent"
                      else Path(path).resolve(strict=True))
            elif tool == "lsblk":
                record(tool, args)
                if args[-1] not in (env["XC0_MOCK_PARTITION"], env["XC0_MOCK_PARENT"]):
                    refuse("unknown lsblk target")
                key = args[1]
                values = {
                    "TYPE": (env.get("XC0_PARTITION_TYPE", "part")
                             if args[-1] == env["XC0_MOCK_PARTITION"] else "disk"),
                    "PKNAME": "xc0-media-test-parent",
                    "TRAN": env.get("XC0_TRANSPORT", "usb"),
                    "RM": env.get("XC0_REMOVABLE", "1"),
                    "SERIAL": env.get("XC0_SERIAL", "25030837640037"),
                    "MOUNTPOINTS": env.get("XC0_MOUNTPOINTS", ""),
                }
                if key not in values:
                    refuse("unknown lsblk field")
                print(values[key])
            elif tool == "verify-artifact.py":
                record("verify", args)
                if (len(args) != 5 or args[0] != "--expect-hostname" or args[1] not in ("loki", "thor")
                        or args[2] != "--expect-profile"
                        or args[3] != {"loki": "testing", "thor": "stable"}[args[1]]):
                    refuse("expected host and profile verification is mandatory")
                image = Path(args[4])
                manifest = json.loads(Path(str(image) + ".manifest.json").read_text())
                if manifest["hostname"] != args[1]:
                    refuse("wrong artifact hostname")
                if manifest["profile"] != args[3]:
                    refuse("wrong artifact profile")
                if hashlib.sha256(image.read_bytes()).hexdigest() != manifest["image_sha256"]:
                    refuse("wrong artifact digest")
                if mode == "ro" and env.get("XC0_FAIL_RO_VERIFY") == args[1]:
                    sys.exit(1)
            elif tool == "usb-in-use.py":
                record("usb", args)
                if args != [env["XC0_MOCK_PARENT"]]:
                    refuse("USB check must use parent")
                if env.get("XC0_USB_IN_USE"):
                    sys.exit(1)
            elif tool == "mountpoint":
                record(tool, args)
                if (len(args) != 3 or args[:2] != ["-q", "--"] or
                        Path(args[2]).name != "media" or
                        Path(args[2]).parent.parent != Path(env["TMPDIR"])):
                    refuse("unexpected mount-state query")
                inject_signal("mountpoint-" + mode)
                if env.get("XC0_FAIL_MOUNTPOINT") == mode:
                    sys.exit(1)
                sys.exit(32 if mode == "unmounted" else 0)
            elif tool in ("cmp", "sha256sum", "mv", "cp", "rmdir"):
                record(tool, args)
                if tool == "rmdir" and mode != "unmounted":
                    record("unsafe-rmdir", args)
                    refuse("attempted directory removal while mounted")
                if tool == "cmp" and args[-1].endswith(env.get("XC0_FAIL_STAGE", "\0")):
                    sys.exit(1)
                if tool == "cmp" and mode == "ro" and args[-1].endswith(env.get("XC0_FAIL_RO_CMP", "\0")):
                    sys.exit(1)
                if tool == "sha256sum" and env.get("XC0_FAIL_SHA256", "\0") in args[-1]:
                    sys.exit(1)
                sys.exit(subprocess.run([env["XC0_REAL_" + tool.upper()], *args], check=False).returncode)
            elif tool == "sync":
                record(tool, args)
            else:
                refuse("unmocked command " + tool)
        ''')
        for name in ("sudo", "realpath", "lsblk", "cmp", "sha256sum", "sync", "mv", "cp", "mountpoint", "rmdir"):
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n" + commands)
            executable.chmod(0o755)
        for name in ("verify-artifact.py", "usb-in-use.py"):
            (self.scripts / name).write_text(commands)
        self.environment = os.environ.copy()
        self.environment.update({
            "PATH": str(self.bin) + os.pathsep + self.environment["PATH"],
            "TMPDIR": str(self.temporary),
            "XC0_MOCK_LOG": str(self.log),
            "XC0_MOCK_STATE": str(self.state),
            "XC0_MOCK_PARTITION": str(self.partition),
            "XC0_MOCK_PARENT": str(self.parent),
            "XC0_MOCK_MEDIA": str(self.media),
        })
        for name in ("cmp", "sha256sum", "mv", "cp", "rmdir"):
            self.environment["XC0_REAL_" + name.upper()] = shutil.which(name)

    def write_copy_script(self, theme=None):
        script = (ISO / "copy-to-ventoy.sh").read_text(encoding="utf-8")
        self.assertEqual(script.count('[ -b '), 2)
        self.assertIn('[ -b "$partition" ]', script)
        self.assertIn('[ -b "$parent" ]', script)
        script = script.replace('[ -b ', '[ -f ')
        source = "theme_source=/opt/xcloud/claude/xcloud/projects/ventoy/stick-tree/ventoy/theme/xcloud"
        self.assertEqual(script.count(source), 1)
        script = script.replace(source, "theme_source=" + shlex.quote(str(theme or self.theme)))
        (self.scripts / "copy-to-ventoy.sh").write_text(script, encoding="utf-8")

    def run_copy(self, arguments=None, **changes):
        if arguments is None:
            arguments = ["--loki", str(self.loki), "--thor", str(self.thor),
                         "--usb-serial", "25030837640037", str(self.partition)]
        return subprocess.run(
            ["sh", str(self.scripts / "copy-to-ventoy.sh"), *arguments],
            env=dict(self.environment, **changes), text=True, capture_output=True,
            check=False, timeout=20,
        )

    def events(self, tool=None):
        events = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return events if tool is None else [event for event in events if event["tool"] == tool]

    def assert_unmounted_and_clean(self):
        self.assertEqual(self.state.read_text(), "unmounted")
        self.assertEqual(list(self.temporary.iterdir()), [])
        self.assertEqual(self.events("unsafe-rmdir"), [])

    def assert_mounted_path_retained(self):
        self.assertNotEqual(self.state.read_text(), "unmounted")
        retained = list(self.temporary.glob("*/media"))
        self.assertEqual(len(retained), 1)
        self.assertTrue(retained[0].is_symlink())
        self.assertEqual(self.events("rmdir"), [])
        self.assertEqual(self.events("unsafe-rmdir"), [])

    def test_dual_copy_stages_before_publish_and_rechecks_read_only_media(self):
        result = self.run_copy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_unmounted_and_clean()
        for host, source in (("loki", self.loki), ("thor", self.thor)):
            target = self.media / f"xc0/xc0-{host}-installer.iso"
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(Path(str(target) + ".manifest.json").read_bytes(),
                             Path(str(source) + ".manifest.json").read_bytes())
            self.assertEqual(Path(str(target) + ".sha256").read_text(),
                             hashlib.sha256(source.read_bytes()).hexdigest() + f"  {target.name}\n")
        for name in ("ventoy.json", "ventoy_grub.cfg"):
            self.assertEqual((self.media / "ventoy" / name).read_bytes(), (ISO / name).read_bytes())
        for name in ("theme.txt", "background.png", "icons/debian.png"):
            self.assertEqual((self.media / "ventoy/theme/xcloud" / name).read_bytes(),
                             (self.theme / name).read_bytes())
        self.assertFalse((self.media / "xcloud").exists())
        self.assertFalse((self.media / "ventoy/script").exists())
        self.assertEqual(list(self.media.rglob("*.new")), [])

        events = self.events()
        first_mount = next(i for i, event in enumerate(events) if event["tool"] == "mount")
        first_publish = next(i for i, event in enumerate(events) if event["tool"] == "mv")
        stage_compares = [i for i, event in enumerate(events)
                          if event["tool"] == "cmp" and event["mode"] == "rw"]
        self.assertLess(max(stage_compares), first_publish)
        self.assertLess(next(i for i, event in enumerate(events) if event["tool"] == "usb"), first_mount)
        verifications = self.events("verify")
        self.assertEqual([(event["args"][1], event["args"][3], event["mode"]) for event in verifications],
                         [("loki", "testing", "unmounted"), ("thor", "stable", "unmounted"),
                          ("loki", "testing", "ro"), ("thor", "stable", "ro")])
        self.assertTrue(all(events.index(event) < first_mount for event in verifications[:2]))
        self.assertEqual([Path(event["args"][4]).name for event in verifications[2:]],
                         ["xc0-loki-installer.iso", "xc0-thor-installer.iso"])
        self.assertEqual([(event["args"], event["mode"]) for event in self.events("sha256sum")],
                         [(["-c", "xc0-loki-installer.iso.sha256"], "ro"),
                          (["-c", "xc0-thor-installer.iso.sha256"], "ro"),
                          (["-c", mock.ANY], "ro")])
        flush = next(i for i, event in enumerate(events) if event["tool"] == "blockdev")
        mounts = self.events("mount")
        self.assertEqual(len(mounts), 2)
        self.assertTrue(mounts[1]["args"][1].startswith("ro,"))
        self.assertLess(events.index(self.events("umount")[0]), flush)
        self.assertLess(flush, events.index(mounts[1]))
        ro_compares = [event["args"][-1] for event in self.events("cmp") if event["mode"] == "ro"]
        for suffix in ("xc0-loki-installer.iso", "xc0-loki-installer.iso.manifest.json",
                        "xc0-loki-installer.iso.sha256", "xc0-thor-installer.iso",
                        "xc0-thor-installer.iso.manifest.json", "xc0-thor-installer.iso.sha256",
                        "ventoy.json", "ventoy_grub.cfg", "icons/debian.png"):
            self.assertTrue(any(path.endswith("/" + suffix) for path in ro_compares), suffix)

    def test_first_backup_survives_a_second_deployment(self):
        self.assertEqual(self.run_copy().returncode, 0)
        backup = self.media / "ventoy/ventoy.json.before-xc0"
        self.assertEqual(backup.read_text(), self.original_config)
        (self.media / "ventoy/ventoy.json").write_text('{"different": "second config"}\n')
        self.log.unlink()
        result = self.run_copy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(backup.read_text(), self.original_config)
        self.assertFalse(any("before-xc0" in event["args"][-1] for event in self.events("cp")))
        self.assert_unmounted_and_clean()

    def test_backup_corruption_during_flush_is_detected_after_read_only_remount(self):
        result = self.run_copy(XC0_CORRUPT_BACKUP_ON_FLUSH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("read-only verified", result.stdout)
        self.assert_unmounted_and_clean()
        mounts = self.events("mount")
        self.assertEqual(len(mounts), 2)
        self.assertTrue(mounts[1]["args"][1].startswith("ro,"))
        checksum = [event for event in self.events("sha256sum")
                    if event["args"][0] == "-c"]
        self.assertTrue(any("ventoy.json.before-xc0.sha256" in event["args"][1]
                            and event["mode"] == "ro" for event in checksum))

    def test_only_the_exact_dual_host_interface_is_accepted(self):
        invalid = (
            [], [str(self.loki), str(self.partition)],
            ["--loki", str(self.loki), "--thor", str(self.thor), str(self.partition)],
            ["--thor", str(self.thor), "--loki", str(self.loki), "--usb-serial", "25030837640037", str(self.partition)],
            ["--loki", str(self.loki), "--thor", str(self.thor), "--usb-serial", "25030837640037", str(self.partition), "extra"],
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                result = self.run_copy(arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.events(), [])

    def test_usb_identity_filesystem_mount_and_libvirt_guards_fail_before_mount(self):
        rejected = (
            {"XC0_SERIAL": "25030837640038"}, {"XC0_SERIAL": ""},
            {"XC0_SERIAL": "25030837640037 "}, {"XC0_SERIAL": " 25030837640037"},
            {"XC0_PARTITION_TYPE": "disk"}, {"XC0_TRANSPORT": "nvme"},
            {"XC0_REMOVABLE": "0"}, {"XC0_LABEL": "not-Ventoy"},
            {"XC0_FILESYSTEM": "ext4"}, {"XC0_MOUNTPOINTS": "/other/partition"},
            {"XC0_USB_IN_USE": "1"},
        )
        for changes in rejected:
            with self.subTest(changes=changes):
                self.log.unlink(missing_ok=True)
                result = self.run_copy(**changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.events("mount"), [])
                self.assertEqual(self.events("cp"), [])
                self.assert_unmounted_and_clean()

    def test_nonregular_iso_or_missing_partition_fails_without_mount(self):
        self.loki.unlink()
        self.loki.mkdir()
        self.assertNotEqual(self.run_copy().returncode, 0)
        self.assertEqual(self.events("mount"), [])
        self.assertEqual(self.events("verify"), [])
        self.loki.rmdir()
        self.loki.write_bytes(b"restored")
        self.partition.unlink()
        self.assertNotEqual(self.run_copy().returncode, 0)
        self.assertEqual(self.events("mount"), [])

    def test_wrong_host_or_bad_artifact_digest_fails_before_mount(self):
        manifest = Path(str(self.thor) + ".manifest.json")
        metadata = json.loads(manifest.read_text())
        for changes in ({"hostname": "loki"}, {"image_sha256": "0" * 64}):
            with self.subTest(changes=changes):
                manifest.write_text(json.dumps(dict(metadata, **changes)))
                self.log.unlink(missing_ok=True)
                result = self.run_copy()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.events("mount"), [])
                self.assertEqual(self.events("cp"), [])

    def test_wrong_host_profile_fails_before_mount(self):
        for host, image, profile in (("loki", self.loki, "stable"), ("thor", self.thor, "testing")):
            with self.subTest(host=host):
                manifest = Path(str(image) + ".manifest.json")
                metadata = json.loads(manifest.read_text())
                manifest.write_text(json.dumps(dict(metadata, profile=profile)))
                self.log.unlink(missing_ok=True)
                result = self.run_copy()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("wrong artifact profile", result.stderr)
                self.assertEqual(self.events("mount"), [])
                self.assertEqual(self.events("cp"), [])
                self.assert_unmounted_and_clean()
                manifest.write_text(json.dumps(metadata))

    def test_different_full_reviewed_refs_fail_before_mount_even_with_the_same_prefix(self):
        for host, image in (("loki", self.loki), ("thor", self.thor)):
            with self.subTest(host=host):
                manifest = Path(str(image) + ".manifest.json")
                metadata = json.loads(manifest.read_text())
                manifest.write_text(json.dumps(dict(metadata, config_ref="a" * 12 + "b" * 28)))
                self.log.unlink(missing_ok=True)
                result = self.run_copy()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("config_ref values do not match", result.stderr)
                self.assertEqual(len(self.events("verify")), 2)
                self.assertEqual(self.events("mount"), [])
                self.assertEqual(self.events("cp"), [])
                self.assert_unmounted_and_clean()
                manifest.write_text(json.dumps(metadata))

    def test_symlinked_theme_ancestor_assets_and_nonvisual_scripts_are_rejected(self):
        link = self.work / "linked-theme"
        link.symlink_to(self.theme.parent, target_is_directory=True)
        self.write_copy_script(link / "xcloud")
        result = self.run_copy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events("mount"), [])
        self.assert_unmounted_and_clean()
        self.write_copy_script()
        bad_asset = self.theme / "bad.png"
        bad_asset.symlink_to(self.theme / "background.png")
        self.assertNotEqual(self.run_copy().returncode, 0)
        bad_asset.unlink()
        (self.theme / "install.py").write_text("do not copy obsolete installers")
        self.assertNotEqual(self.run_copy().returncode, 0)
        self.assertEqual(self.events("mount"), [])
        self.assert_unmounted_and_clean()

    def test_existing_symlink_or_directory_file_destinations_fail_before_staging(self):
        paths = ("xc0/xc0-thor-installer.iso.manifest.json",
                 "xc0/xc0-loki-installer.iso.new", "ventoy/ventoy_grub.cfg",
                 "ventoy/ventoy.json.before-xc0", "ventoy/ventoy.json.before-xc0.new",
                 "ventoy/theme/xcloud/background.png")
        for relative in paths:
            path = self.media / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            for kind in ("symlink", "directory"):
                with self.subTest(path=relative, kind=kind):
                    self.log.unlink(missing_ok=True)
                    if kind == "symlink":
                        path.symlink_to(self.work / "missing")
                    else:
                        path.mkdir()
                    result = self.run_copy()
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(self.events("cp"), [])
                    self.assert_unmounted_and_clean()
                    if kind == "symlink":
                        path.unlink()
                    else:
                        path.rmdir()

    def test_symlinked_media_parent_directory_is_not_followed(self):
        outside = self.work / "outside"
        outside.mkdir()
        (self.media / "ventoy/theme").symlink_to(outside, target_is_directory=True)
        result = self.run_copy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.events("cp"), [])
        self.assert_unmounted_and_clean()

    def test_failed_staging_preserves_both_active_releases_and_old_menu(self):
        result = self.run_copy(XC0_FAIL_STAGE="xc0-thor-installer.iso.new")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events("mv"), [])
        for host in ("loki", "thor"):
            self.assertEqual((self.media / f"xc0/xc0-{host}-installer.iso").read_bytes(), b"previous release")
        self.assertEqual((self.media / "ventoy/ventoy.json").read_text(), self.original_config)
        self.assert_unmounted_and_clean()

    def test_flush_failure_is_fatal_and_prevents_read_only_remount(self):
        result = self.run_copy(XC0_FAIL_FLUSH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.events("mount")), 1)
        self.assertEqual(self.events("sha256sum"), [])
        self.assert_unmounted_and_clean()

    def test_failed_unmount_is_fatal_and_cleanup_retries_without_deleting_media(self):
        result = self.run_copy(XC0_FAIL_UMOUNT_ONCE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.events("umount")), 2)
        self.assertEqual(self.events("blockdev"), [])
        self.assertTrue((self.media / "xc0/xc0-loki-installer.iso").is_file())
        self.assert_unmounted_and_clean()

    def test_read_only_comparison_failure_is_fatal_and_still_unmounts(self):
        result = self.run_copy(XC0_FAIL_RO_CMP="xc0-thor-installer.iso.manifest.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("releases copied and read-only verified", result.stdout)
        self.assert_unmounted_and_clean()

    def test_each_signal_after_mount_or_unmount_reconciles_actual_state(self):
        for signame in ("HUP", "INT", "TERM"):
            for transition in ("mount-rw", "umount-rw", "mount-ro", "umount-ro"):
                with self.subTest(signal=signame, transition=transition):
                    self.log.unlink(missing_ok=True)
                    for marker in self.work.glob("signal-*"):
                        marker.unlink()
                    result = self.run_copy(XC0_SIGNAL=signame, XC0_SIGNAL_AT=transition)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertEqual([event["args"] for event in self.events("signal")],
                                     [[transition, signame]])
                    # A completed unmount must not be attempted again; a completed
                    # mount must be detached even before its next shell command.
                    expected_unmounts = 2 if transition.endswith("-ro") else 1
                    self.assertEqual(len(self.events("umount")), expected_unmounts)
                    self.assertEqual(self.events("mountpoint")[-1]["mode"], "unmounted")
                    self.assertNotIn("releases copied and read-only verified", result.stdout)
                    self.assert_unmounted_and_clean()

    def test_repeated_signals_during_cleanup_do_not_interrupt_reconciliation(self):
        transitions = ["mount-rw", "mountpoint-rw", "umount-rw"]
        for signame in ("HUP", "INT", "TERM"):
            with self.subTest(signal=signame):
                self.log.unlink(missing_ok=True)
                for marker in self.work.glob("signal-*"):
                    marker.unlink()
                result = self.run_copy(XC0_SIGNAL=signame, XC0_SIGNAL_AT=",".join(transitions))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([event["args"] for event in self.events("signal")],
                                 [[transition, signame] for transition in transitions])
                self.assertEqual(len(self.events("umount")), 1)
                self.assert_unmounted_and_clean()

    def test_mount_failure_after_attaching_is_reconciled_and_unmounted(self):
        for access in ("rw", "ro"):
            with self.subTest(access=access):
                self.log.unlink(missing_ok=True)
                result = self.run_copy(XC0_MOUNT_ERROR_AFTER_ATTACH=access)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.events("umount")[-1]["mode"], access)
                self.assertTrue(any(event["mode"] == access for event in self.events("mountpoint")))
                self.assert_unmounted_and_clean()

    def test_unmount_failure_after_detaching_does_not_repeat_unmount(self):
        result = self.run_copy(XC0_UMOUNT_ERROR_AFTER_DETACH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.events("umount")), 1)
        self.assert_unmounted_and_clean()

    def test_persistent_unmount_failure_retains_mounted_directory_without_rmdir(self):
        result = self.run_copy(XC0_FAIL_UMOUNT_ALWAYS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot confirm unmounted state", result.stderr)
        self.assertEqual(len(self.events("umount")), 2)
        self.assertEqual(self.events("blockdev"), [])
        self.assert_mounted_path_retained()

    def test_unmount_success_with_mount_remaining_is_not_trusted(self):
        result = self.run_copy(XC0_UMOUNT_STILL_MOUNTED="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.events("umount")), 2)
        self.assertEqual(self.events("blockdev"), [])
        self.assert_mounted_path_retained()

    def test_mountpoint_query_error_is_not_treated_as_an_unmounted_directory(self):
        result = self.run_copy(XC0_FAIL_MOUNTPOINT="rw")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot confirm unmounted state (1)", result.stderr)
        self.assertEqual(self.events("umount"), [])
        self.assert_mounted_path_retained()

    def test_cleanup_rechecks_even_after_unmount_when_query_reports_an_error(self):
        result = self.run_copy(XC0_FAIL_STAGE="xc0-thor-installer.iso.new", XC0_FAIL_MOUNTPOINT="unmounted")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state.read_text(), "unmounted")
        self.assertEqual(len(self.events("umount")), 1)
        self.assertEqual(self.events("mountpoint")[-1]["mode"], "unmounted")
        self.assertIn("cannot confirm unmounted state (1)", result.stderr)
        self.assertEqual(self.events("rmdir"), [])
        self.assertEqual(self.events("unsafe-rmdir"), [])
        self.assertEqual(len(list(self.temporary.glob("*/media"))), 1)

    def test_each_read_only_checksum_and_host_verification_failure_is_fatal(self):
        for changes in ({"XC0_FAIL_SHA256": "loki"}, {"XC0_FAIL_SHA256": "thor"},
                        {"XC0_FAIL_RO_VERIFY": "loki"}, {"XC0_FAIL_RO_VERIFY": "thor"}):
            with self.subTest(changes=changes):
                self.log.unlink(missing_ok=True)
                result = self.run_copy(**changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("releases copied and read-only verified", result.stdout)
                self.assert_unmounted_and_clean()


class VentoyMenuTests(unittest.TestCase):
    def test_only_both_guarded_host_images_are_aliased_without_autoboot_or_injection(self):
        config = json.loads((ISO / "ventoy.json").read_text())
        controls = {key: value for item in config["control"] for key, value in item.items()}
        self.assertEqual(controls, {
            "VTOY_DEFAULT_SEARCH_ROOT": "/xc0", "VTOY_DEFAULT_MENU_MODE": "0",
            "VTOY_MAX_SEARCH_LEVEL": "0", "VTOY_SECONDARY_BOOT_MENU": "1",
        })
        aliases = {item["image"]: item["alias"] for item in config["menu_alias"]}
        self.assertEqual(set(aliases), {"/xc0/xc0-loki-installer.iso", "/xc0/xc0-thor-installer.iso"})
        for host, suite in (("loki", "forky"), ("thor", "trixie")):
            self.assertIn(host, aliases[f"/xc0/xc0-{host}-installer.iso"])
            self.assertIn(suite, aliases[f"/xc0/xc0-{host}-installer.iso"])
            self.assertIn("guarded base", aliases[f"/xc0/xc0-{host}-installer.iso"])
        self.assertEqual(config["theme"]["file"], "/ventoy/theme/xcloud/theme.txt")
        self.assertEqual(config["theme"]["display_mode"], "GUI")
        self.assertNotIn("auto_install", config)
        self.assertNotIn("injection", config)

    def test_f6_help_describes_the_current_base_installer_not_obsolete_templates(self):
        help_text = (ISO / "ventoy_grub.cfg").read_text()
        for required in ("loki testing/forky", "thor stable/trixie", "local marius password",
                         "LUKS passphrase", "ERASE <live serial>", "powers off", "Remove the USB",
                         "base only", "/etc/xc0-next-steps", "stage2", "fwsetup", "halt", "reboot"):
            self.assertIn(required, help_text)
        for obsolete in ("debian_loki", "debian_thor", "debian_generic", "install.py",
                         "password xcloud", "two write", "two 'write", "/xcloud/README"):
            self.assertNotIn(obsolete, help_text)


if __name__ == "__main__":
    unittest.main()
