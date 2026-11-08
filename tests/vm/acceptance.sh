#!/bin/sh
# Run as root INSIDE a fresh xc0 VM. Base-only by default; --desktop adds stage-2 checks.
# Does not claim real Wi-Fi, speakers, NVIDIA, suspend or Secure Boot validation.
set -eu
profile=${1:?usage: acceptance.sh testing|stable [user] [--host loki|thor] [--desktop]}
shift
user=marius desktop='' expected_host=''
case "${1:-}" in ''|--*) : ;; *) user=$1; shift ;; esac
while [ "$#" -gt 0 ]; do
    case "$1" in
        --desktop) desktop=--desktop; shift ;;
        --host) [ "$#" -ge 2 ] && [ -z "$expected_host" ]; expected_host=$2; shift 2 ;;
        *) exit 2 ;;
    esac
done
case "$expected_host" in ''|loki|thor) : ;; *) exit 2 ;; esac
case "$profile" in testing|stable) : ;; *) exit 2 ;; esac
case "$user" in ''|*[!a-zA-Z0-9_-]*) exit 2 ;; esac
[ "$(id -u)" -eq 0 ] || { echo 'run inside the guest as root' >&2; exit 2; }
systemd-detect-virt --vm >/dev/null || { echo 'VM acceptance script: not a VM' >&2; exit 2; }
pass() { printf 'PASS %s\n' "$*"; }
check_1gib_partition() {
    size=$(blockdev --getsize64 "$1")
    [ "$size" -ge 1073741824 ] && [ "$size" -le 1077936128 ]
}
grep -qx state=complete /var/lib/xc0/install-status
grep -qx stage=base /var/lib/xc0/install-status
grep -qx "profile=$profile" /var/lib/xc0/install-status
pass "Debian BASE installation complete ($profile); desktop is a separate stage"
uid=$(id -u "$user")
home=$(getent passwd "$user" | cut -d: -f6)
[ "$uid" -ne 0 ] && [ -d "$home" ]
pass 'non-root desktop account'
findmnt -rn -o SOURCE / | grep -q /dev/mapper/
lsblk -nr -o TYPE | grep -qx crypt
pass 'encrypted root/LVM boot'
if [ -n "$expected_host" ]; then
    case "$expected_host:$profile" in loki:testing|thor:stable) : ;; *) exit 1 ;; esac
    [ "$(hostname -s)" = "$expected_host" ]
    grep -qx "hostname=$expected_host" /var/lib/xc0/install-status
    grep -qx "layout=$expected_host" /var/lib/xc0/install-status
    grep -qx 'marius=1001:1001' /var/lib/xc0/install-status
    grep -qx 'xcloud=1000:1000' /var/lib/xc0/install-status
    sh /root/xc0-accounts.sh --check
    pass 'exact 1000:1000 xcloud (locked), 1001:1001 marius, named groups and home ownership'
    cryptsetup status xcloud_root_crypt | grep -Eq '^[[:space:]]*type:[[:space:]]+LUKS2$'
    grep -Eq '^xcloud_root_crypt[[:space:]]+UUID=' /etc/crypttab
    root_uuid=$(findmnt -rn -M / -o UUID)
    if [ "$expected_host" = thor ]; then
        level=1
        pairs='@:/ @home:/home @snapshots:/.snapshots @var_log:/var/log @var_tmp:/var/tmp @aios:/var/lib/aios @srv_aios:/srv/aios @xccode:/var/lib/xcloud/xccode'
        esp=/boot/efi
        [ "$(findmnt -rn -M /boot -o FSTYPE)" = ext4 ]
        check_1gib_partition "$(findmnt -rn -M /boot -o SOURCE)"
    else
        level=3
        pairs='@:/ @home:/home @pkg:/var/cache/pacman/pkg @log:/var/log @swap:/swap'
        esp=/boot/efi
        [ "$(findmnt -rn -M /boot -o FSTYPE)" = ext4 ]
        check_1gib_partition "$(findmnt -rn -M /boot -o SOURCE)"
        lsattr -d /swap | awk '$1 !~ /C/ {exit 1}'
        [ ! -e /swap/swapfile ]
    fi
    for pair in $pairs; do
        subvolume=${pair%%:*} mountpoint=${pair#*:}
        [ "$(findmnt -rn -M "$mountpoint" -o FSTYPE)" = btrfs ]
        [ "$(findmnt -rn -M "$mountpoint" -o FSROOT)" = "/$subvolume" ]
        [ "$(findmnt -rn -M "$mountpoint" -o UUID)" = "$root_uuid" ]
        options=$(findmnt -rn -M "$mountpoint" -o OPTIONS)
        printf '%s\n' "$options" | grep -Eq '(^|,)noatime(,|$)'
        if [ "$subvolume" != @swap ]; then
            printf '%s\n' "$options" | grep -Eq "(^|,)compress=zstd:$level(,|\$)"
        fi
    done
    [ "$(findmnt -rn -M "$esp" -o FSTYPE)" = vfat ]
    check_1gib_partition "$(findmnt -rn -M "$esp" -o SOURCE)"
    [ -s "$esp/EFI/debian/shimx64.efi" ] && [ -s "$esp/EFI/debian/grubx64.efi" ]
    awk '!/^#/ && $3 == "swap" {exit 1}' /etc/fstab
    pass 'host-specific UUID/subvolume mounts, boot partitions and LUKS2; no disk swap'
fi
[ "$(timedatectl show -p Timezone --value)" = Europe/Oslo ]
pass 'timezone Europe/Oslo'
grep -Eq '^XKBLAYOUT="?no"?$' /etc/default/keyboard
grep -Eq '^LANG="?en_US\.UTF-8"?$' /etc/default/locale
pass 'Norwegian keyboard and English OS locale (en_US.UTF-8)'
if [ "$profile" = stable ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    [ "$VERSION_ID" = 13 ]
    if apt-cache policy linux-image-amd64 | grep 'Candidate:' | grep -q '~bpo'; then
        echo 'stable profile unexpectedly promotes a backports kernel' >&2; exit 1
    fi
    pass 'Debian 13, stable kernel candidate'
else
    grep -E '^deb .* testing ' /etc/apt/sources.list >/dev/null
    pass 'Debian testing repository'
fi
systemctl is-active --quiet ssh
getent ahostsv4 github.com >/dev/null
pass 'SSH and outbound DNS'
if [ "$desktop" != --desktop ]; then
    if pgrep -x Hyprland >/dev/null || \
        dpkg-query -W -f='${db:Status-Status}' hyprland 2>/dev/null | grep -qx installed; then
        echo 'base unexpectedly installed or runs a desktop' >&2; exit 1
    fi
    cat /etc/xc0-next-steps
    echo 'BASE VM acceptance complete; desktop scripts are not part of this install.'
    exit 0
fi
for service in NetworkManager sddm; do systemctl is-active --quiet "$service"; done
if [ "$profile" = stable ]; then apt-cache policy hyprland | grep -q trixie-backports; fi
as_user() {
    runuser -u "$user" -- env "XDG_RUNTIME_DIR=/run/user/$uid" \
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus" "$@"
}
pgrep -u "$uid" -x Hyprland >/dev/null
pgrep -u "$uid" -x waybar >/dev/null
pass 'actual unprivileged Hyprland and waybar processes'
as_user Hyprland --verify-config -c "$home/.config/hypr/hyprland.lua"
pass 'installed native Lua configuration parses'
instance=$(as_user hyprctl -j instances | jq -er '.[0].instance')
as_user env "HYPRLAND_INSTANCE_SIGNATURE=$instance" hyprctl -j monitors | jq -e 'length > 0' >/dev/null
pass 'compositor exposes a real monitor'
as_user systemctl --user is-active --quiet pipewire wireplumber
as_user wpctl status >/dev/null
pass 'PipeWire and WirePlumber user session'
as_user busctl --user introspect org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop >/dev/null
as_user systemctl --user is-active --quiet xdg-desktop-portal
pass 'session D-Bus and activated desktop portal'
git -C "$home/dotfiles" rev-parse HEAD
dpkg-query -W hyprland sddm pipewire-audio xdg-desktop-portal-hyprland
printf '\nVM acceptance complete. Physical-hardware checks remain separate.\n'
