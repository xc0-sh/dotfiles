#!/usr/bin/env python3
"""Accept raw OpenSSH public keys only; never copy a private key into an ISO."""
import base64
import struct
import sys
from pathlib import Path

ALGORITHMS = {
    "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}


def validate(data: str) -> None:
    if "PRIVATE KEY" in data or "\x00" in data:
        raise ValueError("private keys and NUL bytes are not public authorized keys")
    count = 0
    for line in data.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2 or fields[0] not in ALGORITHMS:
            raise ValueError("provide raw OpenSSH public keys, without authorized_keys options")
        try:
            blob = base64.b64decode(fields[1], validate=True)
            size = struct.unpack(">I", blob[:4])[0]
            algorithm = blob[4:4 + size].decode("ascii")
        except (ValueError, struct.error, UnicodeError) as error:
            raise ValueError("malformed SSH public key") from error
        if algorithm != fields[0] or len(blob) <= 4 + size:
            raise ValueError("SSH public key algorithm/body mismatch")
        count += 1
    if count == 0:
        raise ValueError("no SSH public keys found")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: validate-public-keys.py FILE.pub")
    try:
        validate(Path(sys.argv[1]).read_text())
    except (OSError, ValueError, UnicodeError) as error:
        sys.exit(str(error))
