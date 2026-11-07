"""Deterministic artifact/media tests; never access real disks or libvirt."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
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
        self.xorriso_log = self.work / "xorriso.log"
        self.metadata = {
            "schema": 1,
            "iso_id": "xc0-test-release",
            "mode": "release",
            "profile": "stable",
            "config_ref": "a" * 40,
        }
        self.write_embedded(self.metadata)
        self.write_manifest(self.metadata)
        xorriso = self.bin / "xorriso"
        xorriso.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf '%s\\n' \"$*\" >> \"$XC0_XORRISO_LOG\"\n"
            "[ \"$1\" = -osirrox ] && [ \"$2\" = on ]\n"
            "[ \"$3\" = -indev ] && [ \"$5\" = -extract ]\n"
            "[ \"$6\" = /xc0/artifact.json ]\n"
            "cp -- \"$XC0_EMBEDDED\" \"$7\"\n",
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

    def run_verify(self):
        env = os.environ.copy()
        env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        env["XC0_EMBEDDED"] = str(self.embedded_path)
        env["XC0_XORRISO_LOG"] = str(self.xorriso_log)
        return subprocess.run(
            [sys.executable, str(ISO / "verify-artifact.py"), str(self.iso)],
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
            env=env,
        )

    def test_release_requires_bound_manifest_and_only_reads_artifact_metadata(self):
        Path(str(self.iso) + ".sha256").write_text("0" * 64 + "  stale.iso\n")
        Path(str(self.iso) + ".manifest").write_text("mode=vm-test\n")
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        extraction = self.xorriso_log.read_text(encoding="utf-8")
        self.assertIn("/xc0/artifact.json", extraction)
        self.assertNotIn("initrd", extraction)
        self.assertNotIn("secret", extraction)

    def test_stale_release_manifest_cannot_launder_vm_test_iso(self):
        vm_metadata = dict(self.metadata, mode="vm-test")
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
            '"schema": 1', '"schema": 1, "schema": 1', 1
        )
        self.manifest_path.write_text(duplicate, encoding="utf-8")
        result = self.run_verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate JSON field", result.stderr)

        self.manifest_path.write_text(valid_manifest, encoding="utf-8")
        embedded = self.embedded_path.read_text(encoding="utf-8").replace(
            '"schema": 1', '"schema": 1, "schema": 1', 1
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
            {"iso_id": ""},
            {"mode": "Release"},
            {"profile": "unstable"},
            {"config_ref": "A" * 40},
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
    def test_every_libvirt_rejection_precedes_mount_and_no_formatting_exists(self):
        script = (ISO / "copy-to-ventoy.sh").read_text(encoding="utf-8")
        usb_check = script.index('usb-in-use.py')
        first_mount = script.index('sudo -n mount')
        self.assertLess(usb_check, first_mount)
        self.assertIn('verify-artifact.py', script[:usb_check])
        self.assertIn('.manifest.json', script[:usb_check])
        for formatter in ("mkfs", "wipefs", "fdisk", "parted", "dd if="):
            self.assertNotIn(formatter, script)


if __name__ == "__main__":
    unittest.main()
