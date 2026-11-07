#!/bin/sh
# preseed/late_command, run inside the installed system (in-target, as root).
# Failures here are logged but do not fail the install: the base system is already done.

user=marius
log() { echo "xc0-late: $*"; logger -t xc0-late "$*" 2>/dev/null || true; }

usermod -aG sudo "$user" && log "added $user to sudo"

# Passwordless sudo, so an agent session on this machine can administer it (sudo -n).
printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$user" > /etc/sudoers.d/90-$user
chmod 0440 /etc/sudoers.d/90-$user
if visudo -cf /etc/sudoers.d/90-$user >/dev/null; then
    log "sudoers drop-in ok"
else
    rm -f /etc/sudoers.d/90-$user
    log "sudoers drop-in invalid, removed"
fi

# SSH keys: the public keys published on the operator's GitHub account.
install -d -m 0700 -o "$user" -g "$user" "/home/$user/.ssh"
if curl -fsSL --max-time 30 https://github.com/xc0-marius.keys -o /tmp/keys && [ -s /tmp/keys ]; then
    install -m 0600 -o "$user" -g "$user" /tmp/keys "/home/$user/.ssh/authorized_keys"
    log "installed $(wc -l < /tmp/keys) authorized keys"
else
    log "could not fetch GitHub keys"
fi

# Stage 2 source: the public dotfiles repository.
if su - "$user" -c 'git clone --quiet https://github.com/xc0-sh/dotfiles.git ~/dotfiles'; then
    log "cloned xc0-sh/dotfiles"
else
    log "git clone failed"
fi

# Testing profile: the base came from the trixie CD; move to testing now.
suite=@@SUITE@@
if [ "$suite" = "testing" ]; then
    printf '%s\n' \
        "deb http://deb.debian.org/debian testing main contrib non-free non-free-firmware" \
        "deb http://security.debian.org/debian-security testing-security main contrib non-free non-free-firmware" \
        > /etc/apt/sources.list
    if apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get -y -q \
            -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade; then
        log "upgraded to testing"
    else
        log "upgrade to testing FAILED"
    fi
fi

{
    echo "suite=$suite"
    echo "installed=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "hostname=$(cat /etc/hostname)"
    echo "iso=@@ISO_ID@@"
} > /etc/xc0-install
log "done"
