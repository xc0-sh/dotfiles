#!/bin/sh
# Copy both reviewed host releases to an existing Ventoy partition. No formatting.
set -eu
umask 077
usage() {
    echo 'usage: iso/copy-to-ventoy.sh --loki LOKI.iso --thor THOR.iso --usb-serial 25030837640037 /dev/USB_PARTITION' >&2
}
if [ "$#" -ne 7 ]; then usage; exit 2; fi
if [ "$1" != --loki ] || [ "$3" != --thor ] || [ "$5" != --usb-serial ]; then
    usage
    exit 2
fi
loki=$(realpath -- "$2")
thor=$(realpath -- "$4")
usb_serial=$6
partition=$(realpath -- "$7")
case "$usb_serial" in
    ''|*[!a-zA-Z0-9_.:-]*) echo 'invalid USB serial' >&2; exit 2 ;;
esac
[ -f "$loki" ] && [ -f "$thor" ] && [ -b "$partition" ] || exit 2
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
command -v mountpoint >/dev/null || { echo 'missing tool: mountpoint' >&2; exit 2; }
# Host and suite are bound into each ISO, not inferred from its filename.
python3 "$here/verify-artifact.py" --expect-hostname loki --expect-profile testing "$loki"
python3 "$here/verify-artifact.py" --expect-hostname thor --expect-profile stable "$thor"
# One stick must represent one reviewed post-install checkout.
python3 - "$loki.manifest.json" "$thor.manifest.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    loki_ref = json.load(stream)["config_ref"]
with open(sys.argv[2], encoding="utf-8") as stream:
    thor_ref = json.load(stream)["config_ref"]
if loki_ref != thor_ref:
    sys.exit("release config_ref values do not match; build both from one reviewed checkout")
PY

partition_type=$(lsblk -ndo TYPE -- "$partition")
[ "$partition_type" = part ] || { echo 'specify a partition, not a disk' >&2; exit 2; }
parent_name=$(lsblk -ndo PKNAME -- "$partition")
[ -n "$parent_name" ] || { echo 'cannot identify the USB parent disk' >&2; exit 2; }
parent=$(realpath -- "/dev/$parent_name")
[ -b "$parent" ] || { echo 'USB parent is not a block device' >&2; exit 2; }
parent_type=$(lsblk -ndo TYPE -- "$parent")
[ "$parent_type" = disk ] || { echo 'USB parent is not a disk' >&2; exit 2; }
transport=$(lsblk -ndo TRAN -- "$parent")
[ "$transport" = usb ] || { echo 'parent disk is not attached by USB' >&2; exit 2; }
removable=$(lsblk -ndo RM -- "$parent")
[ "$removable" = 1 ] || { echo 'parent disk is not removable' >&2; exit 2; }
serial=$(lsblk -ndo SERIAL -- "$parent")
[ "$serial" = "$usb_serial" ] || { echo 'USB parent serial does not match --usb-serial' >&2; exit 2; }
label=$(sudo -n blkid -s LABEL -o value "$partition")
[ "$label" = Ventoy ] || {
    echo 'partition is not labelled Ventoy; nothing written' >&2; exit 2;
}
filesystem=$(sudo -n blkid -s TYPE -o value "$partition")
case "$filesystem" in
    exfat|vfat) : ;; *) echo 'unsupported Ventoy data filesystem' >&2; exit 2 ;;
esac
mountpoints=$(lsblk -nro MOUNTPOINTS -- "$parent")
[ -z "$mountpoints" ] || {
    echo 'unmount the USB disk and all its partitions before copying' >&2; exit 2;
}
# This checked libvirt/XML inspection fails closed and always precedes mounting.
sudo -n python3 "$here/usb-in-use.py" "$parent"

theme_source=/opt/xcloud/claude/ventoy/stick-tree/ventoy/theme/xcloud
work=$(mktemp -d)
mountpoint=$work/media
read_mount_state() {
    # util-linux: 0 = mounted, 32 = not mounted; other results are errors.
    if mountpoint -q -- "$mountpoint"; then mount_state=0; else mount_state=$?; fi
}
require_mount_state() {
    read_mount_state
    if [ "$mount_state" -ne "$1" ]; then
        echo "unexpected mount state ($mount_state): $mountpoint" >&2
        exit 1
    fi
}
cleanup() {
    # Mask first: a second signal must not interrupt teardown or an EXIT trap.
    trap '' HUP INT TERM
    trap - EXIT
    result=$1
    if [ -d "$mountpoint" ]; then
        read_mount_state
        if [ "$mount_state" -eq 0 ]; then
            sudo -n umount "$mountpoint" || { [ "$result" -ne 0 ] || result=1; }
            # Reconcile even if umount reported failure after detaching.
            read_mount_state
        fi
        if [ "$mount_state" -eq 32 ]; then
            rmdir -- "$mountpoint" || { [ "$result" -ne 0 ] || result=1; }
        else
            echo "cannot confirm unmounted state ($mount_state); retaining $mountpoint" >&2
            [ "$result" -ne 0 ] || result=1
        fi
    fi
    rm -f -- "$work/loki.sha256" "$work/thor.sha256" \
        "$work/ventoy.json.before-xc0.sha256" \
        "$work/theme-dirs" "$work/theme-files" || { [ "$result" -ne 0 ] || result=1; }
    if [ ! -e "$mountpoint" ] && [ ! -L "$mountpoint" ]; then
        rmdir -- "$work" || { [ "$result" -ne 0 ] || result=1; }
    else
        [ "$result" -ne 0 ] || result=1
    fi
    exit "$result"
}
trap 'cleanup "$?"' EXIT
trap 'cleanup 1' HUP INT TERM
mkdir -- "$mountpoint"
write_checksum() {
    # Renamed images need checksums with their destination's local basename.
    python3 - "$2.manifest.json" "$work/$1.sha256" "xc0-$1-installer.iso" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    digest = json.load(stream)["image_sha256"]
with open(sys.argv[2], "w", encoding="utf-8") as stream:
    stream.write(digest + "  " + sys.argv[3] + "\n")
PY
}
write_file_checksum() {
    python3 - "$1" "$2" ventoy.json.before-xc0 <<'PY'
import hashlib, sys
digest = hashlib.sha256()
with open(sys.argv[1], "rb") as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
with open(sys.argv[2], "w", encoding="utf-8") as stream:
    stream.write(digest.hexdigest() + "  " + sys.argv[3] + "\n")
PY
}
write_checksum loki "$loki"
write_checksum thor "$thor"

# Only the visual theme is imported; no old scripts, templates or instructions.
# lstat every ancestor as well as every asset: realpath alone would hide links.
python3 - "$theme_source" "$work/theme-dirs" "$work/theme-files" <<'PY'
from pathlib import Path
import re, stat, sys

root = Path(sys.argv[1])
try:
    for path in (root, *root.parents):
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError(f"theme source is not a real directory: {path}")
    directories, files = [], []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if not re.fullmatch(r"[A-Za-z0-9_. /-]+", relative):
            raise ValueError(f"unsupported theme asset name: {relative}")
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            directories.append(relative)
        elif stat.S_ISREG(mode) and (
            relative == "theme.txt" or path.suffix.lower() in (".png", ".jpg", ".jpeg", ".tga", ".pf2")
        ):
            files.append(relative)
        else:
            raise ValueError(f"not a regular visual theme asset: {path}")
    if "theme.txt" not in files:
        raise ValueError("theme source has no regular theme.txt")
    Path(sys.argv[2]).write_text("".join(item + "\n" for item in directories), encoding="utf-8")
    Path(sys.argv[3]).write_text("".join(item + "\n" for item in files), encoding="utf-8")
except (OSError, ValueError) as error:
    sys.exit(str(error))
PY
for source in "$here/ventoy.json" "$here/ventoy_grub.cfg"; do
    if [ ! -f "$source" ] || [ -L "$source" ]; then
        echo "not a regular media config: $source" >&2; exit 2
    fi
done
python3 -m json.tool "$here/ventoy.json" >/dev/null

check_file() {
    for destination in "$1" "$1.new"; do
        if [ -L "$destination" ] || { [ -e "$destination" ] && [ ! -f "$destination" ]; }; then
            echo "refusing non-regular destination: $destination" >&2; exit 2
        fi
    done
}
ensure_directory() {
    if [ -L "$1" ] || { [ -e "$1" ] && [ ! -d "$1" ]; }; then
        echo "refusing non-directory or symlink: $1" >&2; exit 2
    fi
    mkdir -p -- "$1"
}
stage_file() {
    check_file "$2"
    cp --remove-destination -- "$1" "$2.new"
    cmp -- "$1" "$2.new"
}
publish_file() {
    check_file "$1"
    mv -- "$1.new" "$1"
}
check_set() {
    check_file "$mountpoint/xc0/xc0-$1-installer.iso"
    check_file "$mountpoint/xc0/xc0-$1-installer.iso.manifest.json"
    check_file "$mountpoint/xc0/xc0-$1-installer.iso.sha256"
}
stage_set() {
    stage_file "$2" "$mountpoint/xc0/xc0-$1-installer.iso"
    stage_file "$2.manifest.json" "$mountpoint/xc0/xc0-$1-installer.iso.manifest.json"
    stage_file "$work/$1.sha256" "$mountpoint/xc0/xc0-$1-installer.iso.sha256"
}
publish_set() {
    publish_file "$mountpoint/xc0/xc0-$1-installer.iso"
    publish_file "$mountpoint/xc0/xc0-$1-installer.iso.manifest.json"
    publish_file "$mountpoint/xc0/xc0-$1-installer.iso.sha256"
}
verify_set() {
    cmp -- "$3" "$mountpoint/xc0/xc0-$1-installer.iso"
    cmp -- "$3.manifest.json" "$mountpoint/xc0/xc0-$1-installer.iso.manifest.json"
    cmp -- "$work/$1.sha256" "$mountpoint/xc0/xc0-$1-installer.iso.sha256"
    (cd "$mountpoint/xc0" && sha256sum -c "xc0-$1-installer.iso.sha256")
    python3 "$here/verify-artifact.py" --expect-hostname "$1" --expect-profile "$2" "$mountpoint/xc0/xc0-$1-installer.iso"
}

sudo -n mount -o "uid=$(id -u),gid=$(id -g),umask=077" "$partition" "$mountpoint"
require_mount_state 0
for directory in xc0 ventoy ventoy/theme ventoy/theme/xcloud; do
    ensure_directory "$mountpoint/$directory"
done
while IFS= read -r directory; do
    ensure_directory "$mountpoint/ventoy/theme/xcloud/$directory"
done < "$work/theme-dirs"

ventoy_config=$mountpoint/ventoy/ventoy.json
ventoy_grub=$mountpoint/ventoy/ventoy_grub.cfg
backup=$mountpoint/ventoy/ventoy.json.before-xc0
check_set loki
check_set thor
check_file "$ventoy_config"
check_file "$ventoy_grub"
check_file "$backup"
while IFS= read -r asset; do
    check_file "$mountpoint/ventoy/theme/xcloud/$asset"
done < "$work/theme-files"

save_backup=false
if [ -e "$ventoy_config" ] && [ ! -e "$backup" ]; then
    stage_file "$ventoy_config" "$backup"
    save_backup=true
fi
if [ "$save_backup" = true ]; then
    write_file_checksum "$backup.new" "$work/ventoy.json.before-xc0.sha256"
elif [ -e "$backup" ]; then
    write_file_checksum "$backup" "$work/ventoy.json.before-xc0.sha256"
fi
# Stage and compare ALL content before replacing any active release or menu.
stage_set loki "$loki"
stage_set thor "$thor"
while IFS= read -r asset; do
    stage_file "$theme_source/$asset" "$mountpoint/ventoy/theme/xcloud/$asset"
done < "$work/theme-files"
stage_file "$here/ventoy_grub.cfg" "$ventoy_grub"
stage_file "$here/ventoy.json" "$ventoy_config"
sync
if [ "$save_backup" = true ]; then publish_file "$backup"; fi
publish_set loki
publish_set thor
while IFS= read -r asset; do
    publish_file "$mountpoint/ventoy/theme/xcloud/$asset"
done < "$work/theme-files"
publish_file "$ventoy_grub"
publish_file "$ventoy_config"
sync
sudo -n umount "$mountpoint"
require_mount_state 32

# Invalidate the parent's block cache before rereading the actual media.
sudo -n blockdev --flushbufs "$parent"
sudo -n mount -o "ro,uid=$(id -u),gid=$(id -g),umask=077" "$partition" "$mountpoint"
require_mount_state 0
verify_set loki testing "$loki"
verify_set thor stable "$thor"
cmp -- "$here/ventoy.json" "$ventoy_config"
cmp -- "$here/ventoy_grub.cfg" "$ventoy_grub"
if [ -e "$backup" ]; then
    (cd "$mountpoint/ventoy" && sha256sum -c "$work/ventoy.json.before-xc0.sha256")
fi
while IFS= read -r asset; do
    cmp -- "$theme_source/$asset" "$mountpoint/ventoy/theme/xcloud/$asset"
done < "$work/theme-files"
sudo -n umount "$mountpoint"
require_mount_state 32
echo 'loki and thor releases copied and read-only verified; no repartitioning performed'
