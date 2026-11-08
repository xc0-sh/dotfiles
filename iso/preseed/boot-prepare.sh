#!/bin/sh
# shellcheck disable=SC3028
# d-i post-base-installer hook, BEFORE kernel installation. No desktop setup.
set -eu
umask 077
# shellcheck source=/dev/null
. /xc0/install.conf
exec >> /target/var/log/xc0-boot-prepare.log 2>&1
printf 'xc0 boot preparation started: host=%s layout=%s\n' "$HOSTNAME" "$LAYOUT"
boot_prepare_exit() {
    result=$?
    trap - EXIT
    printf 'xc0 boot preparation exit: status=%s\n' "$result"
    exit "$result"
}
trap boot_prepare_exit EXIT
case "$LAYOUT" in thor|loki) esp_mount=/target/boot/efi ;; *) exit 1 ;; esac
sh -eu /xc0/select-disk.sh --validate-authorized
printf 'authorized OS disk validated\n'
disk=$(cut -d'|' -f1 /tmp/xc0-authorized)
[ -b "$disk" ]
esp=$(awk -v p="$esp_mount" '$2 == p && $3 == "vfat" {print $1; n++} END {if (n != 1) exit 1}' /proc/mounts)
[ -b "$esp" ]
check_partition() {
    parent=$(in-target --pass-stdout lsblk -ndo PKNAME -- "$1")
    [ "$(readlink -f "/dev/$parent")" = "$(readlink -f "$disk")" ]
    [ "$(in-target --pass-stdout blockdev --getsize64 "$1")" = 1073741824 ]
}
[ "$(in-target --pass-stdout blkid -p -s PTTYPE -o value "$disk")" = gpt ]
check_partition "$esp"
printf 'GPT and exact 1 GiB ESP validated: %s\n' "$esp"
boot=$(awk '$2 == "/target/boot" && $3 == "ext4" {print $1; n++} END {if (n != 1) exit 1}' /proc/mounts)
check_partition "$boot"
printf '%s /boot partition validated: %s\n' "$LAYOUT" "$boot"
