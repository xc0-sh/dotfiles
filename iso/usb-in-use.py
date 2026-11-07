#!/usr/bin/env python3
"""Fail closed if a running libvirt domain might use a USB disk."""

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET

SYS_DEV_BLOCK = Path("/sys/dev/block")


@dataclass(frozen=True)
class BlockDevice:
    devno: int
    sysfs: Path


def _device_from_devno(devno):
    path = SYS_DEV_BLOCK / f"{os.major(devno)}:{os.minor(devno)}"
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"cannot resolve block device {path}") from error
    if not resolved.is_dir():
        raise ValueError(f"invalid sysfs block device: {resolved}")
    return BlockDevice(devno, resolved)


def _block_device(path):
    try:
        info = os.stat(path)
    except OSError as error:
        raise ValueError(f"cannot stat block device {path}: {error}") from error
    if not stat.S_ISBLK(info.st_mode):
        raise ValueError(f"not a block device: {path}")
    return _device_from_devno(info.st_rdev)


def _parent_disk(device):
    partition = device.sysfs / "partition"
    try:
        info = os.stat(partition)
    except FileNotFoundError:
        return device.sysfs
    except OSError as error:
        raise ValueError(f"cannot inspect {partition}: {error}") from error
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"invalid partition marker: {partition}")
    return device.sysfs.parent


def target_disk(path):
    device = _block_device(path)
    if _parent_disk(device) != device.sysfs:
        raise ValueError(f"expected a whole USB disk, not a partition: {path}")
    return device


def _source_uses_target(source_path, attribute, target):
    try:
        info = os.stat(source_path)
    except OSError as error:
        raise ValueError(f"cannot stat VM disk source {source_path}: {error}") from error
    if stat.S_ISBLK(info.st_mode):
        devno = info.st_rdev
    elif attribute == "file" and stat.S_ISREG(info.st_mode):
        # A file-backed disk still depends on its underlying block filesystem.
        devno = info.st_dev
    else:
        raise ValueError(f"unsupported VM disk source type: {source_path}")
    source = _device_from_devno(devno)
    return _depends_on_target(source, target, set())


def _depends_on_target(source, target, seen):
    root = _parent_disk(source)
    if root == target.sysfs:
        return True
    if root in seen:
        raise ValueError("cyclic block-device backing graph")
    seen = seen | {root}
    try:
        slaves = list((root / "slaves").iterdir())
    except FileNotFoundError:
        slaves = []
    except OSError as error:
        raise ValueError(f"cannot inspect backing devices of {root}") from error
    if not slaves and "/virtual/block/" in str(root):
        raise ValueError(f"unknown virtual block backing: {root}")
    for slave in slaves:
        try:
            resolved = slave.resolve(strict=True)
            major, minor = map(int, (resolved / "dev").read_text().strip().split(":"))
        except (OSError, ValueError) as error:
            raise ValueError(f"cannot resolve backing device {slave}") from error
        if _depends_on_target(BlockDevice(os.makedev(major, minor), resolved), target, seen):
            return True
    return False


def _target_usb_address(target):
    for candidate in (target.sysfs, *target.sysfs.parents):
        try:
            bus = int((candidate / "busnum").read_text(encoding="ascii").strip())
            device = int((candidate / "devnum").read_text(encoding="ascii").strip())
            (candidate / "idVendor").read_text(encoding="ascii")
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError(f"cannot read USB identity below {candidate}: {error}") from error
        return bus, device
    return None


def _usb_hostdev_uses_target(hostdev, target):
    addresses = hostdev.findall("./source/address")
    if len(addresses) != 1:
        raise ValueError("USB hostdev has no unique bus/device address; identity is uncertain")
    address = addresses[0]
    try:
        bus = int(address.attrib["bus"], 10)
        device = int(address.attrib["device"], 10)
    except (KeyError, ValueError) as error:
        raise ValueError("USB hostdev has an invalid bus/device address") from error
    target_address = _target_usb_address(target)
    if target_address is None:
        raise ValueError("cannot establish the target disk's USB identity")
    return (bus, device) == target_address


def domain_uses_target(xml_text, target):
    try:
        domain = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise ValueError(f"invalid libvirt domain XML: {error}") from error

    for disk in domain.findall("./devices/disk"):
        for source in disk.findall("source"):
            attributes = [(name, source.get(name)) for name in ("dev", "file") if source.get(name)]
            if len(attributes) > 1:
                raise ValueError("VM disk source has ambiguous dev/file attributes")
            for attribute, source_path in attributes:
                if _source_uses_target(source_path, attribute, target):
                    return True

    for hostdev in domain.findall("./devices/hostdev"):
        if hostdev.get("type") == "usb" and _usb_hostdev_uses_target(hostdev, target):
            return True
    return False


def _virsh(*arguments):
    return subprocess.run(
        ["virsh", "-c", "qemu:///system", *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def attached_domains(path):
    target = target_disk(path)
    conflicts = []
    for line in _virsh("list", "--name").splitlines():
        name = line.strip()
        if not name:
            continue
        xml_text = _virsh("dumpxml", name)
        try:
            if domain_uses_target(xml_text, target):
                conflicts.append(name)
        except ValueError as error:
            raise ValueError(f"cannot prove domain {name} does not use {path}: {error}") from error
    return conflicts


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) != 2:
        print("usage: iso/usb-in-use.py /dev/USB_DISK", file=sys.stderr)
        return 2
    try:
        conflicts = attached_domains(argv[1])
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"USB safety check failed: {error}", file=sys.stderr)
        return 1
    if conflicts:
        print(
            "USB disk is attached to running VM(s): " + ", ".join(conflicts),
            file=sys.stderr,
        )
        return 1
    print("no running libvirt domain uses the USB disk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
