#!/bin/sh
# d-i post-base-installer hook, BEFORE kernel installation. No desktop setup.
set -eu
# shellcheck source=/dev/null
. /xc0/install.conf
case "$LAYOUT" in
    thor) esp_mount=/target/boot/efi ;;
    loki) esp_mount=/target/boot/efi ;;
    *) exit 1 ;;
esac
sh -eu /xc0/select-disk.sh --validate-authorized
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
if [ "$LAYOUT" = thor ]; then
    boot=$(awk '$2 == "/target/boot" && $3 == "ext4" {print $1; n++} END {if (n != 1) exit 1}' /proc/mounts)
    check_partition "$boot"
    exit 0
fi
# partman-efi 110 always writes its ESP fstab entry at /boot/efi, even when the
# recipe requests /boot. Move that validated mount before kernel installation.
[ -d /target/boot/efi ] && [ ! -L /target/boot/efi ]
umount /target/boot/efi
[ -z "$(ls -A /target/boot/efi)" ]
rmdir /target/boot/efi
[ -z "$(ls -A /target/boot)" ]
mount -t vfat -o umask=0077 "$esp" /target/boot
# This is the real firmware loader directory, NOT a /boot/efi mount/alias.
# d-i checks for that case-insensitive path before choosing grub-efi over grub-pc.
mkdir -p /target/boot/EFI
awk '$2 == "/boot/efi" {$2 = "/boot"} {print}' /target/etc/fstab > /target/etc/fstab.xc0-new
chmod 0644 /target/etc/fstab.xc0-new
mv /target/etc/fstab.xc0-new /target/etc/fstab
# FAT cannot store kernel-image symlinks.
if [ -f /target/etc/kernel-img.conf ]; then
    sed '/^[[:space:]]*link_in_boot[[:space:]]*=/d' /target/etc/kernel-img.conf > /target/etc/kernel-img.conf.xc0-new
else
    : > /target/etc/kernel-img.conf.xc0-new
fi
printf 'link_in_boot = no\n' >> /target/etc/kernel-img.conf.xc0-new
chmod 0644 /target/etc/kernel-img.conf.xc0-new
mv /target/etc/kernel-img.conf.xc0-new /target/etc/kernel-img.conf
# Debian grub-install defaults to /boot/efi, even when fstab places the ESP at
# /boot. The diversion also covers future signed GRUB/shim package updates.
in-target dpkg-divert --local --add --rename \
    --divert /usr/sbin/grub-install.xc0-real /usr/sbin/grub-install
cp /xc0/grub-install-loki.sh /target/usr/sbin/grub-install
chmod 0755 /target/usr/sbin/grub-install
