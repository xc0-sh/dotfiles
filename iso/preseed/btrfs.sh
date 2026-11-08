#!/bin/sh
# shellcheck disable=SC3028
# Installer-side adaptation of fenrir's proven @rootfs rename/top-level snapshot
# approach. Only the authorized OS volume is used; never discover/format data.
set -eu
umask 077
die() { echo "xc0 btrfs: $*" >&2; exit 1; }
layout() {
    case "$1" in
        thor)
            options=noatime,compress=zstd:1
            subvolumes='@home:/home @snapshots:/.snapshots @var_log:/var/log @var_tmp:/var/tmp @aios:/var/lib/aios @srv_aios:/srv/aios @xccode:/var/lib/xcloud/xccode'
            esp_mount=/boot/efi ;;
        loki)
            options=noatime,compress=zstd:3
            subvolumes='@home:/home @pkg:/var/cache/pacman/pkg @log:/var/log @swap:/swap'
            esp_mount=/boot ;;
        *) die 'unknown layout' ;;
    esac
}
fstab() {
    for uuid in "$1" "$2" "$3"; do
        case "$uuid" in *[!a-zA-Z0-9-]*) die 'invalid filesystem UUID' ;; esac
    done
    if [ -z "$1" ] || [ -z "$3" ]; then die 'missing filesystem UUID'; fi
    printf '# xc0 %s: UUID-bound Btrfs on LUKS2/LVM; no disk swap\n' "$LAYOUT"
    printf 'UUID=%s / btrfs subvol=@,%s 0 0\n' "$1" "$options"
    for pair in $subvolumes; do
        sv=${pair%%:*} mp=${pair#*:}
        mount_options=$options
        if [ "$sv" = @swap ]; then
            # nodatacow is filesystem-wide as a mount option. +C on this empty
            # subvolume supplies per-directory NOCOW without harming the rest.
            mount_options=noatime
            printf '# /swap has inode attribute +C (NOCOW); no swapfile created.\n'
        fi
        printf 'UUID=%s %s btrfs subvol=%s,%s 0 0\n' "$1" "$mp" "$sv" "$mount_options"
    done
    if [ "$LAYOUT" = thor ]; then
        [ -n "$2" ]
        printf 'UUID=%s /boot ext4 defaults 0 2\n' "$2"
    else
        [ -z "$2" ]
    fi
    printf 'UUID=%s %s vfat umask=0077 0 2\n' "$3" "$esp_mount"
}
# Pure fixture interfaces: no config reads, commands on devices, or mutations.
case "${1:-}" in
    --print-layout)
        [ "$#" -eq 2 ]; layout "$2"
        printf '@:/\n'; for pair in $subvolumes; do printf '%s\n' "$pair"; done
        exit 0 ;;
    --fstab)
        [ "$#" -eq 5 ]; LAYOUT=$2; layout "$LAYOUT"; fstab "$3" "$4" "$5"; exit 0 ;;
    '') [ "$#" -eq 0 ] ;;
    *) exit 2 ;;
esac
# shellcheck source=/dev/null
. /xc0/install.conf
exec >> /target/root/xc0-btrfs-progress.log 2>&1
progress() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
progress "started host=$HOSTNAME layout=$LAYOUT"
case "$LAYOUT:$PROFILE:$MARIUS_UID:$MARIUS_GID:$XCLOUD_UID:$XCLOUD_GID" in
    loki:testing:1001:1001:1000:1000|thor:stable:1001:1001:1000:1000) : ;;
    *) die 'unapproved host policy' ;;
esac
layout "$LAYOUT"
progress 'host policy accepted'
sh -eu /xc0/select-disk.sh --validate-authorized
progress 'disk authorization revalidated'
disk=$(cut -d'|' -f1 /tmp/xc0-authorized)
[ -b "$disk" ] || die 'missing authorized OS disk'
record=$(awk '$5 == "/target" {for (i=7; i<=NF; i++) if ($i == "-") {print $(i+1), $(i+2), $4; n++}} END {if (n != 1) exit 1}' /proc/self/mountinfo)
IFS=' ' read -r fs root_device source_root <<EOF
$record
EOF
progress "root mount parsed fs=$fs device=$root_device source=$source_root"
if [ "$fs" != btrfs ] || [ ! -b "$root_device" ]; then
    die 'target root is not Btrfs'
fi
[ "$(readlink -f "$root_device")" = "$(readlink -f /dev/mapper/xcloud_vg-root)" ] || die 'unexpected root LV'
case "$source_root" in /|/@rootfs|/@) : ;; *) die 'unexpected installed root subvolume' ;; esac
[ "$source_root" != /@ ] || die 'layout already reshaped; refusing a second migration'
lv_names=$(in-target --pass-stdout lvs --noheadings -o lv_name xcloud_vg)
lv_names=$(printf '%s' "$lv_names" | tr -d '[:space:]')
[ "$lv_names" = root ] || die 'expected exactly one root LV'
progress 'root LV identity validated'
if [ "$source_root" = / ]; then
    # A snapshot does not include nested subvolume contents. Never silently
    # accept an ID-5 tree whose descendants would turn into empty placeholders.
    descendants=$(in-target --pass-stdout btrfs subvolume list /)
    [ -z "$descendants" ] || die 'top-level root contains unsupported nested subvolumes'
fi
progress "root LV validated source_subvolume=$source_root"
esp_device=$(awk -v p="/target$esp_mount" '$2 == p && $3 == "vfat" {print $1; n++} END {if (n != 1) exit 1}' /proc/mounts)
boot_device=''
if [ "$LAYOUT" = thor ]; then
    boot_device=$(awk '$2 == "/target/boot" && $3 == "ext4" {print $1; n++} END {if (n != 1) exit 1}' /proc/mounts)
fi
for device in "$esp_device" ${boot_device:+"$boot_device"}; do
    [ -b "$device" ] || die 'missing boot device'
    parent=$(in-target --pass-stdout lsblk -ndo PKNAME -- "$device")
    [ "$(readlink -f "/dev/$parent")" = "$(readlink -f "$disk")" ] || die 'boot partition is not on the authorized disk'
done
progress "boot partitions validated esp=$esp_device boot=${boot_device:-none}"
work=$(mktemp -d /mnt/xc0-reshape.XXXXXX)
top=$work/top
mkdir "$top" "$work/park"
top_mounted=false copy_mounted=false
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    progress "reshape cleanup status=$result"
    if [ "$copy_mounted" = true ]; then umount "$top/@/mnt/xc0-subvol" || result=1; fi
    for parked in "$work"/park/*; do
        [ -e "$parked" ] || continue
        if awk -v p="$parked" '$2 == p {found=1} END {exit !found}' /proc/mounts; then
            umount "$parked" || result=1
        fi
    done
    if [ "$top_mounted" = true ]; then umount "$top" || result=1; fi
    if [ "$result" -eq 0 ]; then rm -rf -- "$work"; else echo "reshape blocked; diagnostics retained: $work" >&2; fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
root_uuid=$(blkid -s UUID -o value "$root_device")
esp_uuid=$(blkid -s UUID -o value "$esp_device")
boot_uuid=''
if [ -n "$boot_device" ]; then boot_uuid=$(blkid -s UUID -o value "$boot_device"); fi
old_crypt=$(awk '!/^#/ && NF {print $1; n++} END {if (n != 1) exit 1}' /target/etc/crypttab)
case "$old_crypt" in ''|*[!a-zA-Z0-9_.+-]*) die 'invalid old mapper name' ;; esac
cryptsetup status "$old_crypt" > "$work/crypto"
grep -Eq '^[[:space:]]*type:[[:space:]]+LUKS2$' "$work/crypto"
crypt_device=$(awk '$1 == "device:" {print $2; n++} END {if (n != 1) exit 1}' "$work/crypto")
parent=$(in-target --pass-stdout lsblk -ndo PKNAME -- "$crypt_device")
[ "$(readlink -f "/dev/$parent")" = "$(readlink -f "$disk")" ] || die 'LUKS device is not on the authorized disk'
crypt_uuid=$(blkid -s UUID -o value "$crypt_device")
case "$crypt_uuid" in ''|*[!a-zA-Z0-9-]*) die 'invalid LUKS UUID' ;; esac
# lvs reports one PV extent range for this recipe. Reject extra PVs/segments.
pv=$(in-target --pass-stdout lvs --noheadings -o devices "$root_device")
pv=$(printf '%s\n' "$pv" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/([0-9][0-9]*)$//')
[ "$(readlink -f "$pv")" = "$(readlink -f "/dev/mapper/$old_crypt")" ] || die 'root LV has an unexpected encrypted PV'
# Validate the complete persistent table before tearing down anything.
fstab "$root_uuid" "$boot_uuid" "$esp_uuid" > "$work/fstab"
awk '!/^#/ && $2 ~ /^\/media\// {print}' /target/etc/fstab > "$work/media.fstab"

# Park EACH nested mount separately, preserving bind roots, devpts, efivars,
# /run and installation media, not just source device names from /proc/mounts.
# Bind parking keeps tmpfs contents alive across the root reshape.
awk '$5 ~ /^\/target\// && $5 != "/target/boot" && $5 != "/target/boot/efi" {
    if ($5 ~ /[\\|]/) exit 1
    print $5, $6
}' /proc/self/mountinfo > "$work/nested.raw"
sort "$work/nested.raw" > "$work/nested.sorted"
awk '{print NR, $0}' "$work/nested.sorted" > "$work/nested"
while IFS=' ' read -r number path flags; do
    if [ -d "$path" ]; then mkdir "$work/park/$number"; else : > "$work/park/$number"; fi
    mount --bind "$path" "$work/park/$number"
    mount -o "remount,bind,$flags" "$work/park/$number"
done < "$work/nested"
awk '$5 ~ /^\/target\// {if ($5 ~ /[\\|]/) exit 1; print $5}' /proc/self/mountinfo > "$work/unmount.raw"
sort -r "$work/unmount.raw" > "$work/unmount"
while IFS= read -r path; do umount "$path"; done < "$work/unmount"
umount /target
mount -o "subvolid=5,$options" "$root_device" "$top"
top_mounted=true
mount -o "remount,$options" "$top"
[ ! -e "$top/@" ] || die 'destination @ already exists'
if [ "$source_root" = / ]; then
    btrfs subvolume snapshot "$top" "$top/@"
else
    # A sibling rename preserves all installed metadata without copy/delete.
    mv "$top/${source_root#/}" "$top/@"
fi
[ -d "$top/@/etc" ] || die 'installed tree missing after root migration'
for pair in $subvolumes; do
    sv=${pair%%:*}
    [ ! -e "$top/$sv" ] || die 'destination subvolume already exists'
    btrfs subvolume create "$top/$sv"
done
progress 'root and sibling subvolumes created'

for pair in $subvolumes; do
    sv=${pair%%:*} mp=${pair#*:}
    [ ! -L "$top/@$mp" ] || die 'symlinked subvolume source'
    mkdir -p "$top/@$mp" "$top/@/mnt/xc0-subvol"
    mount -o "subvol=$sv,$options" "$root_device" "$top/@/mnt/xc0-subvol"
    copy_mounted=true
    if [ "$sv" = @swap ]; then
        chroot "$top/@" chattr +C /mnt/xc0-subvol
    fi
    # Explicit xattr preservation is fatal on failure (plain cp -a is not).
    # GNU tools in the TARGET preserve hidden files, ACLs, ownership and caps.
    # shellcheck disable=SC2016
    chroot "$top/@" /bin/sh -eu -c '
        before=$(stat -c "%u:%g:%a:%Y" "$1")
        cp -a --preserve=mode,ownership,timestamps,links,xattr -- "$1/." /mnt/xc0-subvol/
        diff -qr --no-dereference -- "$1" /mnt/xc0-subvol
        [ "$(stat -c "%u:%g:%a:%Y" /mnt/xc0-subvol)" = "$before" ]
        find "$1" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    ' sh "$mp"
    umount "$top/@/mnt/xc0-subvol"
    copy_mounted=false
    rmdir "$top/@/mnt/xc0-subvol"
done
umount "$top"
top_mounted=false
mount -o "subvol=@,$options" "$root_device" /target
progress 'root remounted on @; mounting sibling subvolumes'
for pair in $subvolumes; do
    sv=${pair%%:*} mp=${pair#*:}
    mkdir -p "/target$mp"
    mount -o "subvol=$sv,$options" "$root_device" "/target$mp"
done
mkdir -p "/target$esp_mount"
if [ -n "$boot_device" ]; then
    mount "$boot_device" /target/boot
    mkdir -p /target/boot/efi
fi
mount -t vfat -o umask=0077 "$esp_device" "/target$esp_mount"
while IFS=' ' read -r number path flags; do
    # Check every component before mkdir/redirection: bind restoration must not
    # follow symlinks or truncate a real backing file hidden by the old mount.
    relative=${path#/target/}
    [ "$relative" != "$path" ] || die 'bind destination escaped target'
    component=/target
    remaining=$relative
    while :; do
        part=${remaining%%/*}
        component=$component/$part
        [ ! -L "$component" ] || die 'symlinked bind destination'
        case "$remaining" in */*) remaining=${remaining#*/} ;; *) break ;; esac
    done
    if [ -d "$work/park/$number" ]; then
        mkdir -p "$path"
    elif [ -e "$path" ]; then
        [ -f "$path" ] || die 'non-regular backing file for bind mount'
    else
        : > "$path"
    fi
    mount --bind "$work/park/$number" "$path"
    mount -o "remount,bind,$flags" "$path"
    umount "$work/park/$number"
done < "$work/nested"
for path in /dev /proc /sys /run; do
    if ! awk -v p="/target$path" '$2 == p {found=1} END {exit !found}' /proc/mounts; then
        mkdir -p "/target$path"
        mount --bind "$path" "/target$path"
    fi
done
cat "$work/fstab" "$work/media.fstab" > /target/etc/fstab
chmod 0644 /target/etc/fstab
progress 'target subvolumes and UUID fstab installed'

# The initramfs cryptroot hook needs the configured name to be ACTIVE too.
if [ "$old_crypt" != xcloud_root_crypt ]; then
    if dmsetup info xcloud_root_crypt >/dev/null 2>&1; then die 'fixed mapper name already in use'; fi
    dmsetup rename "$old_crypt" xcloud_root_crypt
fi
dmsetup info xcloud_root_crypt >/dev/null
printf 'xcloud_root_crypt UUID=%s none luks,initramfs\n' "$crypt_uuid" > /target/etc/crypttab
chmod 0600 /target/etc/crypttab
install -d -m 0755 /target/var/lib/xc0
printf '%s\n' "$LAYOUT" > /target/var/lib/xc0/layout
chmod 0644 /target/var/lib/xc0/layout
progress 'fixed crypttab mapper and UUID layout prepared'
cp /target/root/xc0-btrfs-progress.log /target/var/log/xc0-btrfs.log
echo 'Btrfs layout and live LUKS mapping prepared; boot verification follows in-target.'
