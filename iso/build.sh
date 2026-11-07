#!/bin/sh
# Build the stage-1 installer ISO: the official Debian netinst plus our preseed,
# with an automated install entry as the boot default (UEFI).
#
# Usage: iso/build.sh <netinst.iso> [profile]
#   <netinst.iso>: the signed Debian 13 release netinst (debian-13.x.y-amd64-netinst.iso)
#   profile: testing (default; loki) or stable (trixie + backports)
#
# Secrets come from $XC0_SECRETS (default ~/.config/xc0-iso), never from the repo:
#   luks-passphrase      LUKS passphrase (one line)
#   user-password-hash   crypt(3) hash for marius, e.g. `openssl passwd -6`
#   wifi.env             optional: WIFI_SSID=... and WIFI_PSK=...
# The output ISO therefore contains secrets: keep it off git and public storage.
set -eu

src=${1:?usage: iso/build.sh <netinst.iso> [testing|stable]}
profile=${2:-testing}
secrets=${XC0_SECRETS:-$HOME/.config/xc0-iso}
here=$(cd "$(dirname "$0")" && pwd)
out_dir=${OUT_DIR:-$here/../build}

case "$profile" in
    testing) suite=testing; services="security" ;;
    stable)  suite=trixie;  services="security, updates, backports" ;;
    *) echo "unknown profile: $profile" >&2; exit 2 ;;
esac

for f in luks-passphrase user-password-hash; do
    [ -s "$secrets/$f" ] || { echo "missing $secrets/$f" >&2; exit 2; }
done
for t in xorriso cpio gzip; do
    command -v "$t" >/dev/null || { echo "missing tool: $t" >&2; exit 2; }
done

rev=$(git -C "$here" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
iso_id="xc0-$profile-$rev"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
umask 077
mkdir -p "$work/root/xc0" "$out_dir"

# preseed: profile values, then secrets appended (never substituted into the template)
sed -e "s/@@SERVICES@@/$services/" "$here/preseed/base.cfg" > "$work/root/preseed.cfg"
{
    echo "### appended by build.sh from $secrets"
    printf 'd-i partman-crypto/passphrase password %s\n' "$(cat "$secrets/luks-passphrase")"
    printf 'd-i partman-crypto/passphrase-again password %s\n' "$(cat "$secrets/luks-passphrase")"
    printf 'd-i passwd/user-password-crypted password %s\n' "$(cat "$secrets/user-password-hash")"
    if [ -s "$secrets/wifi.env" ]; then
        # shellcheck disable=SC1091
        . "$secrets/wifi.env"
        printf 'd-i netcfg/wireless_essid string %s\n' "$WIFI_SSID"
        printf 'd-i netcfg/wireless_essid_again string %s\n' "$WIFI_SSID"
        printf 'd-i netcfg/wireless_security_type select wpa\n'
        printf 'd-i netcfg/wireless_wpa string %s\n' "$WIFI_PSK"
    fi
} >> "$work/root/preseed.cfg"

cp "$here/preseed/early.sh" "$here/preseed/select-disk.sh" "$work/root/xc0/"
sed -e "s/@@ISO_ID@@/$iso_id/" -e "s/@@SUITE@@/$suite/" "$here/preseed/late.sh" > "$work/root/xc0/late.sh"

# initrd: original + an appended cpio carrying /preseed.cfg and /xc0
xorriso -osirrox on -indev "$src" \
    -extract /install.amd/gtk/initrd.gz "$work/initrd.orig.gz" \
    -extract /boot/grub/grub.cfg "$work/grub.orig.cfg" >/dev/null 2>&1
( cd "$work/root" && find . | cpio -H newc -o -R 0:0 --quiet ) | gzip -9 > "$work/extra.gz"
cat "$work/initrd.orig.gz" "$work/extra.gz" > "$work/initrd.gz"

# grub.cfg: our entry first and default, 5 s timeout; the stock entries stay below it
awk -v id="$iso_id" '
    !done && /^menuentry/ {
        print "set default=0"
        print "set timeout=5"
        print "menuentry \"xc0 automated install (" id ") - WIPES the internal disk\" {"
        print "    set background_color=black"
        print "    linux    /install.amd/vmlinuz auto=true priority=critical vga=788 --- quiet"
        print "    initrd   /install.amd/gtk/initrd.gz"
        print "}"
        done = 1
    }
    { print }
' "$work/grub.orig.cfg" > "$work/grub.cfg"

out="$out_dir/$iso_id.iso"
rm -f "$out"
xorriso -indev "$src" -outdev "$out" \
    -map "$work/initrd.gz" /install.amd/gtk/initrd.gz \
    -map "$work/grub.cfg" /boot/grub/grub.cfg \
    -boot_image any replay >/dev/null 2>&1
chmod 0600 "$out"
echo "$out"
