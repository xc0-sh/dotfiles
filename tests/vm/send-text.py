#!/usr/bin/env python3
"""Type test input into a named VM without printing credentials or their keycodes.

Only disposable xc0 test domains are accepted. Keys are mapped for the Norwegian
layout selected by preseed. Use --file for passwords; do not put secrets in argv.
"""
import argparse
import string
import subprocess
from pathlib import Path

KEYS = {c: [f"KEY_{c.upper()}"] for c in string.ascii_lowercase + string.digits}
KEYS.update({c: ["KEY_LEFTSHIFT", f"KEY_{c}"] for c in string.ascii_uppercase})
KEYS.update({" ": ["KEY_SPACE"], "-": ["KEY_SLASH"], ".": ["KEY_DOT"],
             "_": ["KEY_LEFTSHIFT", "KEY_SLASH"], "/": ["KEY_LEFTSHIFT", "KEY_7"]})


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("domain")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--file", type=Path)
    p.add_argument("--layout", choices=("no", "us"), default="no",
                   help="current guest console layout, not the intended installed layout")
    p.add_argument("--enter", action="store_true")
    a = p.parse_args()
    if a.layout == "us":
        KEYS.update({"-": ["KEY_MINUS"], "/": ["KEY_SLASH"],
                     "_": ["KEY_LEFTSHIFT", "KEY_MINUS"]})
    if not a.domain.startswith("xc0-e2e-"):
        p.error("only disposable xc0-e2e- domains are eligible")
    text = a.file.read_text().rstrip("\n") if a.file else a.text
    if any(c not in KEYS for c in text):
        p.error("unsupported input characters; nothing was typed")
    prefix = ["sudo", "-n", "virsh", "-c", "qemu:///system", "send-key", a.domain]
    for c in text:
        subprocess.run(prefix + KEYS[c], check=True, stdout=subprocess.DEVNULL)
    if a.enter:
        subprocess.run(prefix + ["KEY_ENTER"], check=True, stdout=subprocess.DEVNULL)
