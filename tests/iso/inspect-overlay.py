#!/usr/bin/env python3
"""Check custom initrd metadata only: never print credential payloads."""
import stat
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path


def entries(data):
    offset = 0
    while offset + 110 <= len(data):
        header = data[offset:offset + 110]
        if header[:6] not in (b"070701", b"070702"):
            raise ValueError("invalid newc header")
        fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
        mode, size, namesize = fields[1], fields[6], fields[11]
        start = offset + 110
        name = data[start:start + namesize - 1].decode()
        start = (start + namesize + 3) & ~3
        if name == "TRAILER!!!":
            return
        yield name.removeprefix("./"), mode
        offset = (start + size + 3) & ~3


def inspect(iso):
    with tempfile.TemporaryDirectory(prefix="xc0-overlay-") as temporary:
        compressed = Path(temporary) / "initrd.gz"
        subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(iso),
                        "-extract", "/xc0/initrd.gz", str(compressed)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        remaining = compressed.read_bytes()
        members = []
        while remaining:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            members.append(decoder.decompress(remaining))
            if not decoder.eof:
                raise ValueError("truncated gzip member")
            remaining = decoder.unused_data
        if len(members) != 2:
            raise ValueError("expected original initrd plus one overlay")
        modes = dict(entries(members[-1]))
        if "lib" in modes or any(name.startswith("lib/") for name in modes):
            raise ValueError("overlay would clobber Debian's /lib symlink")
        for name in (".", "usr", "usr/lib", "usr/lib/partman", "usr/lib/partman/init.d",
                     "usr/lib/post-base-installer.d"):
            if not stat.S_ISDIR(modes.get(name, 0)) or stat.S_IMODE(modes[name]) != 0o755:
                raise ValueError("incorrect shared directory mode: " + name)
        guard = modes.get("usr/lib/partman/init.d/00xc0-guard", 0)
        if not stat.S_ISREG(guard) or not guard & 0o111:
            raise ValueError("missing executable partition guard")
        boot_hook = modes.get("usr/lib/post-base-installer.d/90xc0-boot", 0)
        if not stat.S_ISREG(boot_hook) or not boot_hook & 0o111:
            raise ValueError("missing executable pre-kernel boot hook")
        for name in ("preseed.cfg", "xc0/install.conf", "xc0/early.sh", "xc0/late.sh",
                      "xc0/btrfs.sh", "xc0/accounts.sh", "xc0/boot-prepare.sh"):
            if not stat.S_ISREG(modes.get(name, 0)):
                raise ValueError("missing installer file: " + name)
    print("overlay metadata OK: merged-/usr preserved, shared modes correct, guard executable")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: inspect-overlay.py CUSTOM.iso")
    inspect(Path(sys.argv[1]))
