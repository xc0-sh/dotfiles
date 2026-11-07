#!/bin/bash
# Optional real apt + Lua parser check in a rootless, disposable container.
# No host root, GPU, devices, secrets, service bus or writable repo mount.
set -Eeuo pipefail
if [[ $# != 2 || ! $1 =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo 'Usage: container-smoke.sh CACHED_DEBIAN_IMAGE@sha256:DIGEST {generic|loki}' >&2
    exit 2
fi
case $2 in generic|loki) ;; *) echo 'Only generic or loki smoke profiles are supported' >&2; exit 2 ;; esac
if [[ $(id -u) == 0 ]]; then echo 'Run this test with rootless Podman, not host root' >&2; exit 2; fi
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
podman run --rm --pull=never --user=0 --security-opt=no-new-privileges \
    --cap-add=SYS_NICE \
    --memory=2g --cpus=2 --pids-limit=512 \
    --mount "type=bind,src=$repo,dst=/source,ro=true" \
    --entrypoint /bin/bash -i "$1" -s -- "$2" <<'CONTAINER'
set -Eeuo pipefail
profile=$1
# Freeze source before long apt operations. Editing a bind-mounted shell script
# while Bash is executing it can invalidate its read offset after a function ends.
install -d /var/tmp/bootstrap-source
cp -a /source/setup /source/config /var/tmp/bootstrap-source/
bootstrap=/var/tmp/bootstrap-source/setup/bootstrap.sh
useradd -m -u 2000 -s /bin/bash desktop-test
install -d -o desktop-test -g desktop-test /home/desktop-test/.config /home/desktop-test/.config/foot
printf 'original user data\n' > /home/desktop-test/.config/foot/foot.ini
chown desktop-test:desktop-test /home/desktop-test/.config/foot/foot.ini
install -d /etc/network/interfaces.d
printf 'source /etc/network/interfaces.d/*\nauto lo\niface lo inet loopback\nallow-hotplug enp1s0\niface enp1s0 inet dhcp\n' > /etc/network/interfaces
cp /etc/network/interfaces /var/tmp/interfaces-before
# Model a pre-existing site service policy, so restoration is exercised by apt.
printf '#!/bin/sh\n# site policy\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 0755 /usr/sbin/policy-rc.d
cp /usr/sbin/policy-rc.d /var/tmp/policy-before
# Slim OCI images omit init; d-i's real base already contains these commands.
apt-get -o APT::Update::Error-Mode=any update -qq
DEBIAN_FRONTEND=noninteractive apt-get --assume-yes --no-install-recommends \
    install systemd-sysv ca-certificates util-linux
sh "$bootstrap" --profile "$profile" --user desktop-test --dry-run
sh "$bootstrap" --profile "$profile" --user desktop-test
sh "$bootstrap" --profile "$profile" --user desktop-test
cmp /etc/network/interfaces /var/tmp/interfaces-before
cmp /usr/sbin/policy-rc.d /var/tmp/policy-before
test -L /home/desktop-test/.config/foot/foot.ini
test "$(cat /home/desktop-test/.config/foot/foot.ini.xc0-backup)" = 'original user data'
test ! -e /home/desktop-test/.config/foot/foot.ini.xc0-backup.1
test "$(readlink /etc/systemd/system/networking.service)" = /dev/null
test "$(readlink /etc/systemd/system/ifup@.service)" = /dev/null
systemctl --root=/ is-enabled sddm.service NetworkManager.service bluetooth.service
test -s /var/lib/xc0-bootstrap/last-success
test -x /usr/libexec/polkit-mate-authentication-agent-1
test -s /etc/pam.d/hyprlock
for unit in waybar.service mako.service hypridle.service; do
    test -e "/etc/systemd/user/graphical-session.target.wants/$unit"
done
uwsm start --help >/dev/null
uwsm finalize --help >/dev/null
packages=$(dpkg-query -W -f='${binary:Package}\n')
while IFS= read -r package; do
    [[ $package != plasma-workspace* ]] || { echo 'Unexpected full Plasma dependency' >&2; exit 1; }
done <<< "$packages"
if [[ $profile == generic ]]; then
    version=$(dpkg-query -W -f='${Version}' pipewire)
    [[ $version != *bpo* ]]
    while IFS= read -r package; do
        [[ $package != linux-image-* ]] || { echo 'Unexpected kernel dependency' >&2; exit 1; }
    done <<< "$packages"
fi
dpkg-query -W -f='${binary:Package}\t${Version}\n' hyprland hypridle hyprlock uwsm pipewire sddm foot fuzzel waybar
echo 'CONTAINER SMOKE OK (package/config checks only; no graphical session was started)'
CONTAINER
