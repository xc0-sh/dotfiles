#!/usr/bin/env python3
"""Verify that an ISO and its sidecar describe the same release artifact."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

COMMON_KEYS = {"schema", "iso_id", "mode", "profile", "config_ref"}
MANIFEST_KEYS = COMMON_KEYS | {"image_sha256"}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_METADATA_BYTES = 64 * 1024


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
    if type(metadata["schema"]) is not int or metadata["schema"] != 1:
        raise ValueError("artifact schema must be integer 1")
    if not isinstance(metadata["iso_id"], str) or not metadata["iso_id"]:
        raise ValueError("iso_id must be a non-empty string")
    if metadata["mode"] not in ("release", "vm-test"):
        raise ValueError("mode must be release or vm-test")
    if metadata["profile"] not in ("stable", "testing"):
        raise ValueError("profile must be stable or testing")
    if not isinstance(metadata["config_ref"], str) or not HEX40.fullmatch(
        metadata["config_ref"]
    ):
        raise ValueError("config_ref must be exactly 40 lowercase hexadecimal characters")


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


def _extract_artifact(iso, destination):
    try:
        subprocess.run(
            [
                "xorriso",
                "-osirrox",
                "on",
                "-indev",
                str(iso),
                "-extract",
                "/xc0/artifact.json",
                str(destination),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise ValueError("xorriso could not extract /xc0/artifact.json") from error


def verify_iso(iso):
    iso = Path(iso)
    manifest_path = Path(str(iso) + ".manifest.json")
    manifest = _read_json(manifest_path)
    _validate_manifest(manifest, manifest_path)

    actual_digest = _image_sha256(iso)
    if actual_digest != manifest["image_sha256"]:
        raise ValueError("ISO SHA256 does not match its bound manifest")

    with tempfile.TemporaryDirectory(prefix="xc0-artifact-") as tempdir:
        artifact_path = Path(tempdir) / "artifact.json"
        _extract_artifact(iso, artifact_path)
        embedded = _read_json(artifact_path)
    _validate_common(embedded, "/xc0/artifact.json")

    bound_metadata = {key: manifest[key] for key in COMMON_KEYS}
    if embedded != bound_metadata:
        raise ValueError("embedded artifact metadata does not match the manifest")
    if embedded["mode"] != "release":
        raise ValueError("only release artifacts may be copied or published")
    return embedded


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) != 2:
        print("usage: iso/verify-artifact.py ISO", file=sys.stderr)
        return 2
    try:
        metadata = verify_iso(argv[1])
    except (OSError, ValueError) as error:
        print(f"artifact verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified release artifact {metadata['iso_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
