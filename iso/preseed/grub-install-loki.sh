#!/bin/sh
# Installed only on loki. Keep Debian's signed-image implementation and flags;
# argp's last --efi-directory wins over callers' historical /boot/efi default.
set -eu
exec /usr/sbin/grub-install.xc0-real "$@" --efi-directory=/boot
