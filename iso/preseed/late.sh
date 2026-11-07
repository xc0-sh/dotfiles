#!/bin/sh
# Finish the BASE system only. Desktop/Git scripts run after installation and boot.
# Any base upgrade failure returns nonzero and base.cfg blocks completion.
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

if [ "$PROFILE" = testing ]; then
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

usermod -aG sudo marius
# VM-only disposable convenience; never grant production passwordless root.
if [ "$MODE" = vm-test ]; then
    printf 'marius ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/90-xc0-vm-test
    chmod 0440 /etc/sudoers.d/90-xc0-vm-test
    visudo -cf /etc/sudoers.d/90-xc0-vm-test
fi
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
    printf '  sudo sh setup/bootstrap.sh --profile %s --user marius\n' "$PROFILE"
} > /etc/xc0-next-steps
chmod 0644 /etc/xc0-next-steps
update-grub
status=$(mktemp /var/lib/xc0/.status.XXXXXX)
{
    printf 'state=complete\nstage=base\nprofile=%s\nmode=%s\n' "$PROFILE" "$MODE"
    printf 'recommended_config_ref=%s\niso=%s\n' "$STAGE2_REF" "$ISO_ID"
    printf 'installed=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$status"
chmod 0644 "$status"
printf 'xc0: BASE installation complete; configuration scripts remain post-install\n'
mv -f "$status" /var/lib/xc0/install-status
