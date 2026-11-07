#!/usr/bin/env python3
"""Verify an upstream ISO against the reviewed vendor-signed checksum pin."""
import hashlib
import json
import sys
from pathlib import Path


def verify(path: Path, pin: dict) -> None:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != pin["sha512"]:
        raise ValueError("ISO does not match the pinned Debian release checksum")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: iso/verify-source.py <netinst.iso>")
    try:
        pin = json.loads((Path(__file__).parent / "upstream.json").read_text())
        verify(Path(sys.argv[1]), pin)
    except (OSError, ValueError) as error:
        sys.exit(str(error))
    print(f"verified Debian {pin['version']} SHA512")
