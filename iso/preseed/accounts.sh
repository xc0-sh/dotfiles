#!/bin/sh
# Target-only identity finalization. d-i collects and sets marius's password.
# Never renumber an existing identity or repair ownership on unrelated data.
set -eu
umask 077
# shellcheck source=/dev/null
. /root/xc0-install.conf
case "$MARIUS_UID:$MARIUS_GID:$XCLOUD_UID:$XCLOUD_GID" in
    1001:1001:1000:1000) : ;; *) echo 'unapproved identity map' >&2; exit 1 ;;
esac
case "${1:-}" in ''|--check) : ;; *) exit 2 ;; esac
die() { echo "xc0 accounts: $*" >&2; exit 1; }

check_collision() {
    entry=$(getent passwd "$1" || :)
    if [ -n "$entry" ]; then
        printf '%s\n' "$entry" | awk -F: -v n="$1" -v u="$2" -v g="$3" \
            'NF != 7 || $1 != n || $3 != u || $4 != g {exit 1} END {if (NR != 1) exit 1}' || die "wrong existing account: $1"
    fi
    entry=$(getent passwd "$2" || :)
    [ -z "$entry" ] || [ "${entry%%:*}" = "$1" ] || die "UID collision for $1"
    entry=$(getent group "$1" || :)
    if [ -n "$entry" ]; then
        printf '%s\n' "$entry" | awk -F: -v n="$1" -v g="$3" \
            'NF != 4 || $1 != n || $3 != g {exit 1} END {if (NR != 1) exit 1}' || die "wrong existing group: $1"
    fi
    entry=$(getent group "$3" || :)
    [ -z "$entry" ] || [ "${entry%%:*}" = "$1" ] || die "GID collision for $1"
    if [ -e "/home/$1" ] || [ -L "/home/$1" ]; then
        [ -d "/home/$1" ] && [ ! -L "/home/$1" ] &&
            [ "$(stat -c '%u:%g' "/home/$1")" = "$2:$3" ] || die "wrong existing home owner: $1"
    fi
}

assert_account() {
    check_collision "$1" "$2" "$3"
    entry=$(getent passwd "$1")
    [ "$(printf '%s\n' "$entry" | cut -d: -f6-7)" = "/home/$1:/bin/bash" ] || die "wrong home/shell: $1"
    [ "$(id -u "$1")" = "$2" ] && [ "$(id -g "$1")" = "$3" ] || die "wrong numeric identity: $1"
    [ "$(getent group "$1" | cut -d: -f3)" = "$3" ] || die "missing named primary group: $1"
    [ -d "/home/$1" ] && [ ! -L "/home/$1" ] &&
        [ "$(stat -c '%u:%g' "/home/$1")" = "$2:$3" ] || die "wrong home: $1"
    mismatch=$(find "/home/$1" -xdev \( ! -uid "$2" -o ! -gid "$3" \) -print -quit)
    [ -z "$mismatch" ] || die "unexpected ownership inside home: $1"
    id -nG "$1" | grep -qw sudo || die "missing authenticated sudo group: $1"
}

# Check all names and numbers before the first mutation. The d-i-created private
# marius group is verified just like any other existing group, never guessed.
check_collision marius "$MARIUS_UID" "$MARIUS_GID"
check_collision xcloud "$XCLOUD_UID" "$XCLOUD_GID"
getent passwd marius >/dev/null || die 'd-i did not create the password-bearing marius account'
if [ "${1:-}" != --check ]; then
    # All named normal groups exist before creating any missing user.
    getent group marius >/dev/null || groupadd --gid "$MARIUS_GID" marius
    getent group xcloud >/dev/null || groupadd --gid "$XCLOUD_GID" xcloud
    if ! getent passwd xcloud >/dev/null; then
        useradd --uid "$XCLOUD_UID" --gid "$XCLOUD_GID" --create-home --shell /bin/bash xcloud
    fi
    passwd --lock xcloud
    usermod -aG sudo marius
    usermod -aG sudo xcloud
    rule=$(mktemp /etc/sudoers.d/.xc0-users.XXXXXX)
    printf 'marius,xcloud ALL=(ALL:ALL) PASSWD: ALL\n' > "$rule"
    chmod 0440 "$rule"
    visudo -cf "$rule"
    mv -f "$rule" /etc/sudoers.d/50-xc0-users
fi
assert_account marius "$MARIUS_UID" "$MARIUS_GID"
assert_account xcloud "$XCLOUD_UID" "$XCLOUD_GID"
case "$(passwd -S xcloud)" in 'xcloud L '*) : ;; *) die 'xcloud password is not locked' ;; esac
case "$(passwd -S marius)" in 'marius P '*) : ;; *) die 'marius has no usable d-i password' ;; esac
# Refuse an old private VM shortcut rather than carrying it into a new install.
for file in /etc/sudoers /etc/sudoers.d/*; do
    [ -f "$file" ] || continue
    awk '{sub(/^[[:space:]]+/, "")} !/^#/ && /NOPASSWD[[:space:]]*:/ {exit 1}' "$file" || die 'passwordless sudo policy present'
done
visudo -c
printf 'marius=%s:%s xcloud=%s:%s (locked)\n' "$MARIUS_UID" "$MARIUS_GID" "$XCLOUD_UID" "$XCLOUD_GID"
