#!/bin/sh
# Copy a reviewed RELEASE ISO to an existing Ventoy data partition. No formatting.
set -eu
umask 077
if [ "$#" -ne 2 ]; then
    echo 'usage: iso/copy-to-ventoy.sh RELEASE.iso /dev/USB_PARTITION' >&2
    exit 2
fi
iso=$(realpath -- "$1")
partition=$(realpath -- "$2")
[ -f "$iso" ] && [ -b "$partition" ] || exit 2
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
manifest=$iso.manifest.json
python3 "$here/verify-artifact.py" "$iso"

partition_type=$(lsblk -ndo TYPE -- "$partition")
[ "$partition_type" = part ] || { echo 'specify a partition, not a disk' >&2; exit 2; }
parent_name=$(lsblk -ndo PKNAME -- "$partition")
[ -n "$parent_name" ] || { echo 'cannot identify the USB parent disk' >&2; exit 2; }
parent=$(realpath -- "/dev/$parent_name")
[ -b "$parent" ] || { echo 'USB parent is not a block device' >&2; exit 2; }
transport=$(lsblk -ndo TRAN -- "$parent")
[ "$transport" = usb ] || { echo 'parent disk is not attached by USB' >&2; exit 2; }
removable=$(lsblk -ndo RM -- "$parent")
[ "$removable" = 1 ] || { echo 'parent disk is not removable' >&2; exit 2; }
label=$(sudo -n blkid -s LABEL -o value "$partition")
[ "$label" = Ventoy ] || {
    echo 'partition is not labelled Ventoy; nothing written' >&2; exit 2;
}
filesystem=$(sudo -n blkid -s TYPE -o value "$partition")
case "$filesystem" in
    exfat|vfat) : ;; *) echo 'unsupported Ventoy data filesystem' >&2; exit 2 ;;
esac
mountpoints=$(lsblk -ndo MOUNTPOINTS -- "$partition")
[ -z "$mountpoints" ] || {
    echo 'unmount the Ventoy partition before copying' >&2; exit 2;
}
# This checked libvirt/XML inspection fails closed and always precedes mounting.
sudo -n python3 "$here/usb-in-use.py" "$parent"

mountpoint=$(mktemp -d)
mounted=false
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$mounted" = true ]; then sudo -n umount "$mountpoint" || result=$?; fi
    rmdir -- "$mountpoint" || result=$?
    exit "$result"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

copy_verified() {
    [ ! -d "$2" ] && [ ! -d "$2.new" ] || {
        echo "refusing directory destination: $2" >&2; exit 2;
    }
    cp --remove-destination -- "$1" "$2.new"
    sync
    cmp -- "$1" "$2.new"
    mv -- "$2.new" "$2"
}

sudo -n mount -o "uid=$(id -u),gid=$(id -g),umask=077" "$partition" "$mountpoint"
mounted=true
for directory in xc0 ventoy; do
    [ ! -L "$mountpoint/$directory" ] || {
        echo "refusing symlink on Ventoy partition: $directory" >&2; exit 2;
    }
    mkdir -p -- "$mountpoint/$directory"
done

target_iso=$mountpoint/xc0/xc0-installer.iso
target_manifest=$target_iso.manifest.json
target_sha256=$target_iso.sha256
copy_verified "$iso" "$target_iso"
copy_verified "$manifest" "$target_manifest"
copy_sha256=false
if [ -f "$iso.sha256" ]; then
    copy_verified "$iso.sha256" "$target_sha256"
    copy_sha256=true
fi

ventoy_config=$mountpoint/ventoy/ventoy.json
[ ! -L "$ventoy_config" ] || {
    echo 'refusing symlink Ventoy config' >&2; exit 2;
}
if [ -e "$ventoy_config" ]; then
    [ -f "$ventoy_config" ] || { echo 'invalid existing Ventoy config' >&2; exit 2; }
    copy_verified "$ventoy_config" "$mountpoint/ventoy/ventoy.json.before-xc0"
fi
copy_verified "$here/ventoy.json" "$ventoy_config"
sync
sudo -n umount "$mountpoint"
mounted=false

# Reopen read-only and revalidate the actual bound pair that reached the media.
sudo -n mount -o "ro,uid=$(id -u),gid=$(id -g),umask=077" "$partition" "$mountpoint"
mounted=true
cmp -- "$iso" "$target_iso"
cmp -- "$manifest" "$target_manifest"
cmp -- "$here/ventoy.json" "$ventoy_config"
if [ "$copy_sha256" = true ]; then cmp -- "$iso.sha256" "$target_sha256"; fi
python3 "$here/verify-artifact.py" "$target_iso"
echo 'release copied and verified; physical disk has not been repartitioned'
