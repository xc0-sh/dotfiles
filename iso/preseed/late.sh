#!/bin/sh
# Finish the BASE system only. Desktop/Git scripts run after installation and boot.
# Any base upgrade failure returns nonzero and base.cfg blocks completion.
# HOSTNAME is supplied by install.conf, not a shell builtin.
# shellcheck disable=SC3028
set -eu
umask 077
# shellcheck source=/dev/null
. /root/xc0-install.conf
install -d -m 0755 /var/lib/xc0
printf 'state=provisioning\nstage=base\n' > /var/lib/xc0/install-status
chmod 0644 /var/lib/xc0/install-status
# One authoritative record, even if an ancillary write fails later.
ln -sfn /var/lib/xc0/install-status /etc/xc0-install
record_exit() {
    rc=$?
    if [ "$rc" -ne 0 ]; then
        printf 'state=failed\nstage=base\nexit=%s\n' "$rc" > /var/lib/xc0/install-status
    fi
}
trap record_exit EXIT
exec >>/var/log/xc0-install.log 2>&1
printf 'xc0: starting %s, git=%s\n' "$ISO_ID" "$STAGE2_REF"
case "$LAYOUT:$PROFILE:$MARIUS_UID:$MARIUS_GID:$XCLOUD_UID:$XCLOUD_GID" in
    loki:testing:1001:1001:1000:1000|thor:stable:1001:1001:1000:1000) : ;;
    *) echo 'unapproved layout/identity policy' >&2; exit 1 ;;
esac
case "$HOSTNAME:$LAYOUT" in loki:thor|thor:loki) exit 1 ;; esac
sh /root/xc0-accounts.sh

if [ "$PROFILE" = testing ]; then
    # Recent d-i writes deb822 here. Leaving it enabled mixes trixie back in.
    if [ -e /etc/apt/sources.list.d/debian.sources ]; then
        [ -f /etc/apt/sources.list.d/debian.sources ] && [ ! -L /etc/apt/sources.list.d/debian.sources ]
        mv /etc/apt/sources.list.d/debian.sources /var/lib/xc0/debian.sources.installer
    fi
    printf '%s\n' \
        'deb https://deb.debian.org/debian testing main contrib non-free non-free-firmware' \
        'deb https://security.debian.org/debian-security testing-security main contrib non-free non-free-firmware' \
        > /etc/apt/sources.list
    apt-get -o APT::Update::Error-Mode=any -o Acquire::AllowInsecureRepositories=false update
    DEBIAN_FRONTEND=noninteractive apt-get -y \
        -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade
    grep -Eq '^VERSION_CODENAME="?forky"?$' /etc/os-release
else
    grep -Eq '^VERSION_CODENAME="?trixie"?$' /etc/os-release
fi
apt-get check
audit=$(dpkg --audit)
[ -z "$audit" ] || { printf '%s\n' "$audit"; exit 1; }
dpkg-query -W -f='${binary:Package}\t${Version}\n' > /var/lib/xc0/base-packages.tsv
chmod 0644 /var/lib/xc0/base-packages.tsv

if [ -s /root/xc0-authorized_keys ]; then
    install -d -m 0700 -o marius -g marius /home/marius/.ssh
    install -m 0600 -o marius -g marius /root/xc0-authorized_keys /home/marius/.ssh/authorized_keys
fi

{
    printf 'Debian base installed. Desktop configuration has NOT been run.\n\n'
    printf 'After first boot, as marius:\n'
    printf '  git clone https://github.com/xc0-sh/dotfiles.git ~/dotfiles\n'
    printf '  git -C ~/dotfiles checkout --detach %s\n' "$STAGE2_REF"
    printf '  cd ~/dotfiles\n'
    case "$HOSTNAME" in
        loki)
            printf '  sudo bash setup/bootstrap.sh --profile loki --user marius --dry-run\n'
            printf '  sudo bash setup/bootstrap.sh --profile loki --user marius\n'
            ;;
        thor)
            printf '\nPREREQUISITE: separately prepare and verify the supported Debian NVIDIA driver/kernel pair.\n'
            printf 'Do not acknowledge --nvidia-prepared until NVIDIA preparation is complete.\n'
            printf '  sudo bash setup/bootstrap.sh --profile thor --user marius --nvidia-prepared --dry-run\n'
            printf '  sudo bash setup/bootstrap.sh --profile thor --user marius --nvidia-prepared\n'
            ;;
        *)
            printf '  sudo bash setup/bootstrap.sh --profile %s --user marius --dry-run\n' "$PROFILE"
            printf '  sudo bash setup/bootstrap.sh --profile %s --user marius\n' "$PROFILE"
            ;;
    esac
} > /etc/xc0-next-steps
chmod 0644 /etc/xc0-next-steps
update-initramfs -u -k all
update-grub
# A renamed crypttab without a renamed LIVE mapping produces an empty initrd
# crypttab. btrfs.sh handles both; verify every installed initramfs after upgrades.
initrds=0
for image in /boot/initrd.img-*; do
    [ -s "$image" ] || { echo 'missing installed initramfs' >&2; exit 1; }
    initrds=$((initrds + 1))
    unpack=$(mktemp -d /var/tmp/xc0-initrd.XXXXXX)
    unmkinitramfs "$image" "$unpack"
    find "$unpack" -type f -path '*/cryptroot/crypttab' > "$unpack/crypttabs"
    found=false
    while IFS= read -r crypttab; do
        if grep -q '^xcloud_root_crypt[[:space:]]' "$crypttab"; then found=true; fi
    done < "$unpack/crypttabs"
    rm -rf -- "$unpack"
    [ "$found" = true ] || { echo 'initramfs missing xcloud_root_crypt' >&2; exit 1; }
done
[ "$initrds" -gt 0 ]
grep -Eq '^[[:space:]]*linux[[:space:]].*rootflags=([^[:space:]]*,)?subvol=@([,[:space:]]|$)' /boot/grub/grub.cfg
esp=/boot/efi
[ -s "$esp/EFI/debian/shimx64.efi" ] && [ -s "$esp/EFI/debian/grubx64.efi" ]
# Match the installed Debian signed payloads, not merely plausible filenames.
cmp /usr/lib/shim/shimx64.efi.signed "$esp/EFI/debian/shimx64.efi"
cmp /usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed "$esp/EFI/debian/grubx64.efi"
# update-grub does not regenerate EFI/debian/grub.cfg. Both layouts have a
# separate /boot filesystem, so bind this small bootstrap to its actual UUID.
boot_uuid=$(grub-probe --target=fs_uuid /boot)
mounted_uuid=$(findmnt -rn -M /boot -o UUID)
case "$boot_uuid:$mounted_uuid" in *[!a-zA-Z0-9:-]*) echo 'invalid boot UUID' >&2; exit 1 ;; esac
if [ -z "$boot_uuid" ] || [ -z "$mounted_uuid" ]; then echo 'missing boot UUID' >&2; exit 1; fi
[ "$(printf '%s' "$boot_uuid" | tr '[:upper:]' '[:lower:]')" = \
  "$(printf '%s' "$mounted_uuid" | tr '[:upper:]' '[:lower:]')" ]
bootstrap=$(mktemp /var/tmp/xc0-grub-bootstrap.XXXXXX)
{
    printf 'search --no-floppy --fs-uuid --set=root %s\n' "$boot_uuid"
    # GRUB variables, deliberately not expanded by the installer shell.
    # shellcheck disable=SC2016
    printf 'set prefix=($root)/grub\nconfigfile $prefix/grub.cfg\n'
} > "$bootstrap"
cat "$bootstrap" > "$esp/EFI/debian/grub.cfg.new"
cmp "$bootstrap" "$esp/EFI/debian/grub.cfg.new"
mv -f "$esp/EFI/debian/grub.cfg.new" "$esp/EFI/debian/grub.cfg"
cmp "$bootstrap" "$esp/EFI/debian/grub.cfg"
rm -f "$bootstrap"
sh /root/xc0-accounts.sh --check
status=$(mktemp /var/lib/xc0/.status.XXXXXX)
{
    printf 'state=complete\nstage=base\nprofile=%s\nmode=%s\n' "$PROFILE" "$MODE"
    printf 'recommended_config_ref=%s\niso=%s\n' "$STAGE2_REF" "$ISO_ID"
    printf 'hostname=%s\nlayout=%s\nmarius=%s:%s\nxcloud=%s:%s\n' \
        "$HOSTNAME" "$LAYOUT" "$MARIUS_UID" "$MARIUS_GID" "$XCLOUD_UID" "$XCLOUD_GID"
    printf 'installed=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$status"
chmod 0644 "$status"
printf 'xc0: BASE installation complete; configuration scripts remain post-install\n'
mv -f "$status" /var/lib/xc0/install-status
