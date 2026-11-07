#!/usr/bin/env python3
"""Verify that an ISO and its sidecar describe the same release artifact."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zlib

COMMON_KEYS = {
    "schema", "iso_id", "hostname", "mode", "profile", "target_mode",
    "target_serial", "target_dmi", "target_model", "protected_model", "config_ref",
    "layout", "marius_uid", "marius_gid", "xcloud_uid", "xcloud_gid",
}
MANIFEST_KEYS = COMMON_KEYS | {"image_sha256"}
CONFIG_FIELDS = {key.upper(): key for key in COMMON_KEYS - {"schema", "config_ref"}}
CONFIG_FIELDS["STAGE2_REF"] = "config_ref"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
SERIAL = re.compile(r"[a-zA-Z0-9_.:-]{1,128}\Z")
DMI = re.compile(r"[a-zA-Z0-9 ._:()+/-]{1,256}\Z")
MODEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9 ._:+/-]*[a-zA-Z0-9])?\Z")
MAX_METADATA_BYTES = 64 * 1024
MAX_INITRD_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_CPIO_ENTRIES = 200000


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _open_regular(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"not a regular file: {path}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _read_json(path):
    descriptor = _open_regular(path)
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > MAX_METADATA_BYTES:
            raise ValueError(f"metadata is too large: {path}")
        data = stream.read(MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata is too large: {path}")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON in {path}: {error}") from error


def _validate_common(metadata, path):
    if not isinstance(metadata, dict) or set(metadata) != COMMON_KEYS:
        raise ValueError(f"unexpected embedded metadata schema in {path}")
    if type(metadata["schema"]) is not int or metadata["schema"] != 3:
        raise ValueError("artifact schema must be integer 3")
    if any(not isinstance(metadata[key], str) for key in COMMON_KEYS - {"schema"}):
        raise ValueError("artifact fields other than schema must be strings")
    if not HOSTNAME.fullmatch(metadata["hostname"]):
        raise ValueError("invalid hostname")
    if metadata["mode"] not in ("release", "vm-test"):
        raise ValueError("mode must be release or vm-test")
    if metadata["profile"] not in ("stable", "testing"):
        raise ValueError("profile must be stable or testing")
    if (metadata["layout"], metadata["profile"]) not in (("loki", "testing"), ("thor", "stable")):
        raise ValueError("layout requires loki/testing or thor/stable")
    if metadata["hostname"] in ("loki", "thor") and metadata["hostname"] != metadata["layout"]:
        raise ValueError("hostname does not match its host layout/profile")
    # Operator-approved deployed IDs, not the conflicting older thor prose.
    # Canonical decimal strings also match the literal initrd assignment format.
    if tuple(metadata[key] for key in ("marius_uid", "marius_gid", "xcloud_uid", "xcloud_gid")) != (
        "1001", "1001", "1000", "1000"
    ):
        raise ValueError("identity map must be marius 1001:1001 and xcloud 1000:1000")
    if not isinstance(metadata["config_ref"], str) or not HEX40.fullmatch(
        metadata["config_ref"]
    ):
        raise ValueError("config_ref must be exactly 40 lowercase hexadecimal characters")
    expected_id = "xc0-{hostname}-{profile}-{mode}-{ref}".format(
        **metadata, ref=metadata["config_ref"][:12]
    )
    if metadata["iso_id"] != expected_id:
        raise ValueError("iso_id does not identify its hostname, profile, mode and ref")

    target_mode = metadata["target_mode"]
    serial, dmi = metadata["target_serial"], metadata["target_dmi"]
    model, protected = metadata["target_model"], metadata["protected_model"]
    if metadata["mode"] == "vm-test":
        if (target_mode, serial, dmi, model, protected) != (
            "vm-test", "XC0_TEST_DISK", "QEMU", "", ""
        ):
            raise ValueError("invalid vm-test target policy")
    elif target_mode == "serial-dmi":
        if not SERIAL.fullmatch(serial) or not DMI.fullmatch(dmi) or not re.search(
            r"[a-zA-Z0-9]", dmi
        ) or model or protected:
            raise ValueError("serial-dmi requires exact serial and DMI, with no model policy")
    elif target_mode == "model-confirm":
        if metadata["hostname"] != "thor" or serial or dmi or model == protected or any(
            len(value) > 128 or not MODEL.fullmatch(value) for value in (model, protected)
        ):
            raise ValueError("model-confirm requires thor and distinct nonempty model policies only")
    else:
        raise ValueError("unknown release target_mode")


def _validate_manifest(metadata, path):
    if not isinstance(metadata, dict) or set(metadata) != MANIFEST_KEYS:
        raise ValueError(f"unexpected manifest schema in {path}")
    _validate_common({key: metadata[key] for key in COMMON_KEYS}, path)
    digest = metadata["image_sha256"]
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        raise ValueError("image_sha256 must be exactly 64 lowercase hexadecimal characters")


def _image_sha256(path):
    digest = hashlib.sha256()
    descriptor = _open_regular(path)
    with os.fdopen(descriptor, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_file(iso, member, destination):
    try:
        subprocess.run(
            [
                "xorriso",
                "-osirrox",
                "on",
                "-indev",
                str(iso),
                "-extract",
                member,
                str(destination),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError(f"xorriso could not extract {member}") from error


class _InitrdReader:
    """Bound both decompression and allocation, including discarded archive files."""

    def __init__(self, stream):
        self.stream = stream
        self.total = 0

    def read(self, size):
        if self.total + size > MAX_UNPACKED_BYTES:
            raise ValueError("initrd exceeds decompressed size limit")
        data = self.stream.read(size)
        self.total += len(data)
        return data

    def exact(self, size):
        data = self.read(size)
        if len(data) != size:
            raise ValueError("truncated cpio archive")
        return data

    def padding(self, size):
        if self.exact(size) != b"\0" * size:
            raise ValueError("nonzero cpio padding")

    def discard(self, size):
        while size:
            chunk = min(size, 1024 * 1024)
            self.exact(chunk)
            size -= chunk


def _read_cpio_config(stream):
    reader = _InitrdReader(stream)
    config = None
    entries = 0
    between_archives = True
    while True:
        if between_archives:
            # newc archives can have zero padding up to the next gzip member.
            padding = 0
            first = reader.read(1)
            while first == b"\0":
                padding += 1
                if padding > 4096:
                    raise ValueError("excessive cpio archive padding")
                first = reader.read(1)
            if padding % 4:
                raise ValueError("unaligned cpio archive padding")
            if not first:
                break
            header = first + reader.exact(109)
            between_archives = False
        else:
            header = reader.exact(110)
        entries += 1
        if entries > MAX_CPIO_ENTRIES:
            raise ValueError("too many cpio entries")
        if header[:6] != b"070701" or not re.fullmatch(rb"[0-9a-fA-F]{104}", header[6:]):
            raise ValueError("invalid newc header")
        fields = [int(header[index:index + 8], 16) for index in range(6, 110, 8)]
        mode, links, size, namesize, checksum = (
            fields[1], fields[4], fields[6], fields[11], fields[12]
        )
        if not 1 <= namesize <= 4096 or checksum != 0:
            raise ValueError("invalid newc name size or checksum")
        raw_name = reader.exact(namesize)
        if raw_name[-1:] != b"\0" or b"\0" in raw_name[:-1]:
            raise ValueError("invalid cpio pathname")
        try:
            name = raw_name[:-1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("invalid cpio pathname encoding") from error
        reader.padding(-(110 + namesize) % 4)
        if name == "TRAILER!!!":
            if size:
                raise ValueError("invalid cpio trailer")
            between_archives = True
            continue
        name = name[2:] if name.startswith("./") else name
        if name != "." and any(part in ("", ".", "..") for part in name.split("/")):
            raise ValueError("ambiguous cpio pathname")
        if name in (".", "xc0") and not stat.S_ISDIR(mode):
            raise ValueError("install.conf parent is not a directory")
        if name.startswith("xc0/install.conf/"):
            raise ValueError("install.conf is used as a directory")
        if name == "xc0/install.conf":
            if config is not None:
                raise ValueError("duplicate install.conf in initrd")
            if not stat.S_ISREG(mode) or links != 1 or size > MAX_METADATA_BYTES:
                raise ValueError("install.conf must be a bounded regular file, not a link")
            config = reader.exact(size)
        else:
            reader.discard(size)
        reader.padding(-size % 4)
    if config is None:
        raise ValueError("missing /xc0/install.conf in initrd")
    return config


def _read_initrd_config(path):
    descriptor = _open_regular(path)
    with os.fdopen(descriptor, "rb") as compressed:
        if os.fstat(compressed.fileno()).st_size > MAX_INITRD_BYTES:
            raise ValueError("compressed initrd is too large")
        try:
            # GzipFile streams across concatenated members, including the overlay.
            with gzip.GzipFile(fileobj=compressed) as stream:
                data = _read_cpio_config(stream)
        except (OSError, EOFError, zlib.error) as error:
            raise ValueError(f"invalid gzip initrd: {error}") from error
    try:
        lines = data.decode("ascii").splitlines(keepends=True)
    except UnicodeDecodeError as error:
        raise ValueError("install.conf must be ASCII assignments") from error
    values = {}
    for line in lines:
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)='([^'\r\n]*)'\n?", line)
        if not match:
            raise ValueError("install.conf contains nonliteral shell syntax")
        key, value = match.groups()
        if key in values:
            raise ValueError(f"duplicate install.conf field: {key}")
        values[key] = value
    if set(values) != set(CONFIG_FIELDS):
        raise ValueError("unexpected install.conf fields")
    return {CONFIG_FIELDS[key]: value for key, value in values.items()}


def verify_iso(iso, expect_hostname=None, expect_profile=None):
    iso = Path(iso)
    if expect_hostname is not None and not HOSTNAME.fullmatch(expect_hostname):
        raise ValueError("invalid expected hostname")
    if expect_profile is not None and expect_profile not in ("stable", "testing"):
        raise ValueError("invalid expected profile")
    manifest_path = Path(str(iso) + ".manifest.json")
    manifest = _read_json(manifest_path)
    _validate_manifest(manifest, manifest_path)

    actual_digest = _image_sha256(iso)
    if actual_digest != manifest["image_sha256"]:
        raise ValueError("ISO SHA256 does not match its bound manifest")

    with tempfile.TemporaryDirectory(prefix="xc0-artifact-") as tempdir:
        artifact_path = Path(tempdir) / "artifact.json"
        _extract_file(iso, "/xc0/artifact.json", artifact_path)
        embedded = _read_json(artifact_path)
        _validate_common(embedded, "/xc0/artifact.json")

        bound_metadata = {key: manifest[key] for key in COMMON_KEYS}
        if embedded != bound_metadata:
            raise ValueError("embedded artifact metadata does not match the manifest")
        if expect_hostname is not None and embedded["hostname"] != expect_hostname:
            raise ValueError("artifact hostname does not match expected hostname")
        if expect_profile is not None and embedded["profile"] != expect_profile:
            raise ValueError("artifact profile does not match expected profile")
        if embedded["mode"] != "release":
            raise ValueError("only release artifacts may be copied or published")
        initrd_path = Path(tempdir) / "initrd.gz"
        _extract_file(iso, "/xc0/initrd.gz", initrd_path)
        config = _read_initrd_config(initrd_path)
        if config != {key: embedded[key] for key in COMMON_KEYS - {"schema"}}:
            raise ValueError("initrd install.conf does not match embedded artifact metadata")
    return embedded


def main(argv=None):
    argv = sys.argv if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso")
    parser.add_argument("--expect-hostname", metavar="NAME")
    parser.add_argument("--expect-profile", choices=("stable", "testing"))
    args = parser.parse_args(argv[1:])
    try:
        metadata = verify_iso(args.iso, args.expect_hostname, args.expect_profile)
    except (OSError, ValueError) as error:
        print(f"artifact verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified release artifact {metadata['iso_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
