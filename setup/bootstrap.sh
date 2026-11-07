#!/bin/bash
# Support standalone invocation with sh; re-exec before Bash-only syntax is parsed.
if [ -z "${BASH_VERSION:-}" ]; then exec /bin/bash "$0" "$@"; fi
# Post-install only: run as root AFTER Debian has been installed and booted.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_DIR=${SCRIPT_DIR%/setup}
STAGED_CONFIG=/usr/local/share/xc0-desktop
PROFILE='' TARGET_USER='' SUITE='' TARGET_HOME='' TARGET_UID='' TARGET_GID=''
DRY_RUN=0 NVIDIA_PREPARED=0 STEP=preflight POLICY_DIR='' VERIFY_DIR='' LOG_FILE=''
BACKUP_DIR='' POLICY_ACTIVE=0 POLICY_PATH=/usr/sbin/policy-rc.d
COMMON_PACKAGES=() HYPR_PACKAGES=() EXTRA_PACKAGES=() INSTALL_PACKAGES=()

log() { printf 'xc0-bootstrap: %s\n' "$*"; }
die() { log "ERROR stage=$STEP: $*${LOG_FILE:+ (log: $LOG_FILE)}" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: bootstrap.sh --profile PROFILE --user USER [--dry-run] [--nvidia-prepared]
       bootstrap.sh --help

Profiles: testing, stable (hostname-aware aliases), or explicit:
          loki (testing/forky); thor, baldr, generic, qemu (13/trixie).
USER must already exist; no passwords, sudo rules or SSH keys are created.
--dry-run          Root-only preflight and operation plan; no changes or log file.
--nvidia-prepared  Thor only: acknowledge separately prepared Debian NVIDIA stack.
                   Requires nvidia-driver to be installed. Does NOT validate GPU.
No suite upgrade, driver installation, service restart, autologin or reboot.
EOF
}

parse_args() {
    while (($#)); do
        case $1 in
            --profile)
                [[ $# -ge 2 && -z $PROFILE ]] || die 'supply --profile once, with a value'
                PROFILE=$2; shift 2 ;;
            --user)
                [[ $# -ge 2 && -z $TARGET_USER ]] || die 'supply --user once, with a value'
                TARGET_USER=$2; shift 2 ;;
            --dry-run) DRY_RUN=1; shift ;;
            --nvidia-prepared) NVIDIA_PREPARED=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
    done
    local host_name
    case $PROFILE in
        testing)
            host_name=$(hostname); host_name=${host_name%%.*}
            case $host_name in
                thor|baldr) die "$host_name requires the stable profile" ;;
                loki) PROFILE=loki ;;
            esac
            SUITE=forky ;;
        stable)
            host_name=$(hostname); host_name=${host_name%%.*}
            case $host_name in
                loki) die 'loki requires the testing profile' ;;
                thor|baldr) PROFILE=$host_name ;;
                *) PROFILE=generic ;;
            esac
            SUITE=trixie ;;
        loki) SUITE=forky ;;
        thor|baldr|generic|qemu) SUITE=trixie ;;
        *) die 'explicit --profile must be testing, stable, loki, thor, baldr, generic or qemu' ;;
    esac
    [[ $TARGET_USER =~ ^[a-z_][a-z0-9_-]{0,31}$ && $TARGET_USER != root ]] || die 'invalid --user (root is not a desktop user)'
    [[ $NVIDIA_PREPARED == 0 || $PROFILE == thor ]] || die '--nvidia-prepared is only valid with --profile thor'
}

check_os_release() {
    local key value os_id='' codename='' version=''
    [[ -r $1 ]] || die 'cannot read os-release'
    # Do not source shell code, even from test fixtures.
    while IFS='=' read -r key value; do
        value=${value#\"}; value=${value%\"}
        case $key in ID) os_id=$value ;; VERSION_CODENAME) codename=$value ;; VERSION_ID) version=$value ;; esac
    done < "$1"
    [[ $os_id == debian && $codename == "$SUITE" ]] || die "profile $PROFILE requires Debian $SUITE, found ${os_id:-unknown}/${codename:-unknown}; no suite upgrade is performed"
    [[ $SUITE != trixie || $version == 13 || $version == 13.* ]] || die 'stable profiles require Debian 13'
}

check_user() {
    local account name _password _gecos shell
    account=$(getent passwd "$TARGET_USER") || die "user does not exist: $TARGET_USER"
    IFS=: read -r name _password TARGET_UID TARGET_GID _gecos TARGET_HOME shell <<< "$account"
    [[ $name == "$TARGET_USER" && $TARGET_UID =~ ^[0-9]+$ && $TARGET_UID -ge 1000 && $TARGET_UID -lt 65534 ]] || die 'desktop user must have a normal login UID (1000..65533)'
    [[ $TARGET_HOME == /* && $TARGET_HOME != / && -d $TARGET_HOME && ! -L $TARGET_HOME ]] || die 'user needs a real, absolute home directory'
    [[ $(stat -c %u -- "$TARGET_HOME") == "$TARGET_UID" ]] || die 'home directory must be owned by the login user'
    [[ -x $shell && $shell != */nologin && $shell != */false ]] || die 'user needs a usable login shell'
}

check_ifupdown() {
    # Only the vanilla d-i DHCP/IPv6-auto/WPA-PSK subset is auto-handed-off.
    # Never print any line: interfaces may contain Wi-Fi secrets. Static IPs,
    # bridges, hook commands and external source files require manual conversion.
    [[ -e $1 ]] || return 0
    awk '
        /^[ \t]*#/ || /^[ \t]*$/ { next }
        $1 == "source" && NF == 2 && $2 == "/etc/network/interfaces.d/*" { next }
        $1 == "auto" || $1 == "allow-hotplug" { next }
        $1 == "iface" && NF == 4 {
            if ($2 == "lo" && ($3 == "inet" || $3 == "inet6") && $4 == "loopback") { iface = "lo"; next }
            if ($2 ~ /^[a-zA-Z0-9_-]+$/ && (($3 == "inet" && $4 == "dhcp") || ($3 == "inet6" && $4 == "auto"))) { iface = $2; next }
            exit 1
        }
        iface != "" && iface != "lo" && ($1 == "wpa-ssid" || $1 == "wpa-psk" || ($1 == "wpa-key-mgmt" && $2 == "WPA-PSK")) && NF >= 2 { next }
        { exit 1 }
    ' "$1" || die 'custom ifupdown config: convert to NetworkManager profiles before bootstrap (original left untouched)'
}

check_nvidia_preparation() {
    if [[ $PROFILE == thor ]]; then
        [[ $NVIDIA_PREPARED == 1 ]] || die 'thor blocked: separately prepare the supported NVIDIA driver/kernel pair, then acknowledge with --nvidia-prepared'
        [[ $(dpkg-query -W -f='${db:Status-Status}' nvidia-driver 2>/dev/null) == installed ]] || die 'thor requires a separately installed Debian nvidia-driver package'
    fi
}

preflight() {
    [[ $(id -u) == 0 ]] || die 'run as root in the installed Debian target (also for --dry-run)'
    check_os_release /etc/os-release
    check_user
    check_nvidia_preparation
    local manager
    if [[ -e /etc/systemd/system/display-manager.service || -L /etc/systemd/system/display-manager.service ]]; then
        manager=$(readlink -f /etc/systemd/system/display-manager.service)
        [[ $manager == */sddm.service ]] || die 'another display manager is configured; switch it explicitly before bootstrap'
    fi
    if [[ -s /etc/X11/default-display-manager ]]; then
        read -r manager < /etc/X11/default-display-manager
        [[ $manager == /usr/bin/sddm ]] || die 'another Debian display manager is selected'
    fi
    check_ifupdown /etc/network/interfaces
    if [[ -d /etc/network/interfaces.d ]] && [[ -n $(find /etc/network/interfaces.d -mindepth 1 -print -quit) ]]; then
        die 'nonempty interfaces.d (including symlinks) requires manual NetworkManager preparation first'
    fi
    local command
    for command in apt-get apt-cache dpkg dpkg-query runuser systemctl flock install tee; do
        command -v "$command" >/dev/null || die "required base command missing: $command"
    done
}

load_packages() {
    local file=$1 name=$2 line
    local -n packages=$name
    packages=()
    [[ -r $file ]] || die "missing package list: $file"
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%%#*}
        local -a words=()
        read -r -a words <<< "$line"
        ((${#words[@]})) || continue
        [[ ${#words[@]} == 1 && ${words[0]} =~ ^[a-z0-9][a-z0-9+.-]+$ ]] || die "invalid package entry in $file"
        packages+=("${words[0]}")
    done < "$file"
    ((${#packages[@]})) || die "empty package list: $file"
}

plan_packages() {
    EXTRA_PACKAGES=()
    load_packages "$SCRIPT_DIR/packages/common.txt" COMMON_PACKAGES
    load_packages "$SCRIPT_DIR/packages/hyprland.txt" HYPR_PACKAGES
    if [[ $PROFILE == loki ]]; then load_packages "$SCRIPT_DIR/packages/loki.txt" EXTRA_PACKAGES; fi
    INSTALL_PACKAGES=("${COMMON_PACKAGES[@]}" "${EXTRA_PACKAGES[@]}")
    local package
    for package in "${HYPR_PACKAGES[@]}"; do
        if [[ $SUITE == trixie ]]; then INSTALL_PACKAGES+=("$package/trixie-backports")
        else INSTALL_PACKAGES+=("$package"); fi
    done
}

run() {
    printf 'xc0-bootstrap: +'; printf ' %q' "$@"; printf '\n'
    if [[ $DRY_RUN == 0 ]]; then "$@"; fi
}

system_file() {
    local source=$1 destination=$2 mode=${3:-0644} parent
    [[ -f $source && ! -L $source ]] || die "not a regular source file: $source"
    if [[ $DRY_RUN == 1 ]]; then log "stage $source -> $destination ($mode; preserve previous contents)"; return; fi
    parent=${destination%/*}
    [[ ! -L $destination && (! -e $destination || -f $destination) ]] || die "refusing non-regular system destination: $destination"
    local component='' part
    local -a parts
    IFS=/ read -r -a parts <<< "$parent"
    for part in "${parts[@]}"; do
        [[ -n $part ]] || continue
        component+=/$part
        [[ ! -L $component ]] || die "refusing symlinked system directory: $component"
    done
    install -d -m 0755 -- "$parent"
    if [[ -f $destination ]] && cmp -s -- "$source" "$destination"; then
        chmod "$mode" -- "$destination"
        return
    fi
    if [[ -e $destination ]]; then
        install -d -m 0700 -- "$BACKUP_DIR$parent"
        cp -a -- "$destination" "$BACKUP_DIR$destination"
        log "preserved $destination in $BACKUP_DIR"
    fi
    local temporary
    temporary=$(mktemp "$parent/.xc0-stage.XXXXXXXX")
    install -m "$mode" -- "$source" "$temporary"
    mv -T -- "$temporary" "$destination"
}

block_service_starts() {
    # Maintainer scripts must not grab the current network or start a greeter.
    # Keep/restore a pre-existing policy exactly, including symlinks and its mode.
    local temporary_parent=${1:-/var/tmp}
    [[ ! -d $POLICY_PATH ]] || die 'policy-rc.d must not be a directory'
    POLICY_DIR=$(mktemp -d "$temporary_parent/xc0-policy.XXXXXXXX")
    if [[ -e $POLICY_PATH || -L $POLICY_PATH ]]; then
        cp -a -- "$POLICY_PATH" "$POLICY_DIR/original"
    fi
    printf '#!/bin/sh\nexit 101\n' > "$POLICY_DIR/blocked"
    chmod 0755 -- "$POLICY_DIR/blocked"
    POLICY_ACTIVE=1
    # Atomic replacement, so a pre-existing symlink's TARGET is never overwritten.
    mv -T -- "$POLICY_DIR/blocked" "$POLICY_PATH"
}

restore_service_policy() {
    [[ -n $POLICY_DIR ]] || return 0
    if [[ $POLICY_ACTIVE == 1 ]]; then
        if [[ -e $POLICY_DIR/original || -L $POLICY_DIR/original ]]; then
            mv -T -- "$POLICY_DIR/original" "$POLICY_PATH" || return 1
        else rm -f -- "$POLICY_PATH" || return 1; fi
    fi
    rm -f -- "$POLICY_DIR/blocked" "$POLICY_DIR/original" || return 1
    rmdir -- "$POLICY_DIR" || return 1
    POLICY_DIR='' POLICY_ACTIVE=0
}

cleanup() {
    local result=$?
    trap - EXIT
    restore_service_policy || { log "ERROR restoring package service-start policy; backup retained at $POLICY_DIR" >&2; result=1; }
    if [[ -n $VERIFY_DIR ]]; then
        # The check runs as the user; never recursively remove user-controlled data.
        rmdir -- "$VERIFY_DIR" 2>/dev/null || log "config-check runtime retained: $VERIFY_DIR"
    fi
    exit "$result"
}

archive_keyring() {
    local key=/usr/share/keyrings/debian-archive-keyring.gpg
    # Newer Debian archive-keyring packages use .pgp; both are signed archive keys.
    if [[ ! -r $key && -r ${key%.gpg}.pgp ]]; then key=${key%.gpg}.pgp; fi
    [[ -r $key ]] || die 'Debian archive keyring is missing'
    printf '%s\n' "$key"
}

configure_apt() {
    [[ $SUITE == trixie ]] || return 0
    local key source
    key=$(archive_keyring)
    if [[ $DRY_RUN == 1 ]]; then
        log "add signed trixie-backports source using $key"
    else
        source=$(mktemp /var/tmp/xc0-source.XXXXXXXX)
        sed "s|/usr/share/keyrings/debian-archive-keyring.gpg|$key|" "$SCRIPT_DIR/system/trixie-backports.sources" > "$source"
        system_file "$source" /etc/apt/sources.list.d/xc0-backports.sources
        rm -f -- "$source"
    fi
    system_file "$SCRIPT_DIR/system/backports.pref" /etc/apt/preferences.d/xc0-backports
}

check_apt_suites() {
    local line field origin codename base_found=0
    local -a fields
    while IFS= read -r line; do
        [[ $line == *'release '* ]] || continue
        origin='' codename=''
        IFS=, read -r -a fields <<< "${line#*release }"
        for field in "${fields[@]}"; do
            case $field in o=*) origin=${field#o=} ;; n=*) codename=${field#n=} ;; esac
        done
        [[ -n $origin || -n $codename ]] || continue # installed dpkg/status
        if [[ $origin == Debian && $codename == "$SUITE" ]]; then base_found=1; fi
        case "$origin/$codename" in
            "Debian/$SUITE"|"Debian/$SUITE-updates"|"Debian/$SUITE-security") ;;
            'Debian Backports/trixie-backports') [[ $SUITE == trixie ]] || die 'backports configured on testing' ;;
            *) die "unsupported apt origin/suite: $origin/$codename; prepare only this profile's signed Debian sources" ;;
        esac
    done
    [[ $base_found == 1 ]] || die "apt has no Debian $SUITE base index"
}

install_packages() {
    run apt-get -o APT::Update::Error-Mode=any -o Acquire::AllowInsecureRepositories=false update
    if [[ $DRY_RUN == 0 ]]; then
        local policy
        policy=$(apt-cache policy)
        check_apt_suites <<< "$policy"
        block_service_starts
    fi
    DEBIAN_FRONTEND=noninteractive run apt-get --assume-yes --no-install-recommends --no-remove \
        -o APT::Get::AllowUnauthenticated=false \
        -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
        install "${INSTALL_PACKAGES[@]}"
    if [[ $DRY_RUN == 0 ]]; then restore_service_policy; fi
}

stage_config() {
    local relative
    system_file "$REPO_DIR/config/manifest.txt" "$STAGED_CONFIG/manifest.txt"
    while IFS= read -r relative || [[ -n $relative ]]; do
        [[ -z $relative || $relative == \#* ]] && continue
        [[ $relative != /* && $relative != *..* && $relative != *[[:space:]]* ]] || die 'unsafe config manifest entry'
        system_file "$REPO_DIR/config/desktop/$relative" "$STAGED_CONFIG/desktop/$relative"
    done < "$REPO_DIR/config/manifest.txt"
    system_file "$SCRIPT_DIR/link-config.sh" /usr/local/libexec/xc0-link-config 0755
    system_file "$SCRIPT_DIR/session.sh" /usr/local/bin/xc0-hyprland-session 0755
    system_file "$SCRIPT_DIR/system/sddm.conf" /etc/sddm.conf.d/20-xc0.conf
    system_file "$SCRIPT_DIR/system/default-display-manager" /etc/X11/default-display-manager
    system_file "$SCRIPT_DIR/system/hyprland.desktop" "$STAGED_CONFIG/wayland-sessions/xc0-hyprland.desktop"
    run install -d -m 0755 "$STAGED_CONFIG/xsessions"
    system_file "$SCRIPT_DIR/system/NetworkManager.conf" /etc/NetworkManager/conf.d/20-xc0-ifupdown.conf
    if [[ $DRY_RUN == 1 ]]; then
        log 'verify Hyprland Lua as login user; link configs as login user (conflicts backed up)'
        return
    fi
    local version
    version=$(dpkg-query -W -f='${Version}' hyprland)
    dpkg --compare-versions "$version" ge 0.55.2 || die 'Hyprland Lua config needs version >= 0.55.2'
    [[ -x /usr/libexec/polkit-mate-authentication-agent-1 && -s /etc/pam.d/hyprlock ]] || die 'Debian polkit agent / hyprlock PAM configuration missing'
    VERIFY_DIR=$(mktemp -d /var/tmp/xc0-verify.XXXXXXXX)
    chown "$TARGET_UID:$TARGET_GID" -- "$VERIFY_DIR"
    run runuser -u "$TARGET_USER" -- env -i HOME="$TARGET_HOME" PATH="$PATH" XDG_RUNTIME_DIR="$VERIFY_DIR" \
        Hyprland --verify-config -c "$STAGED_CONFIG/desktop/hypr/hyprland.lua"
    run runuser -u "$TARGET_USER" -- env -i HOME="$TARGET_HOME" PATH="$PATH" \
        /usr/local/libexec/xc0-link-config "$STAGED_CONFIG" "$TARGET_HOME"
    run runuser -u "$TARGET_USER" -- env -i HOME="$TARGET_HOME" PATH="$PATH" xdg-user-dirs-update
}

configure_services() {
    # Offline symlink operations only: no --now, reload, restart, ifdown or nmcli.
    # ifup@.service is udev's hotplug path; disabling networking alone is not enough.
    run systemctl --root=/ mask networking.service ifup@.service
    run systemctl --root=/ enable NetworkManager.service bluetooth.service sddm.service
    if [[ $PROFILE == loki ]]; then run systemctl --root=/ enable power-profiles-daemon.service; fi
    run systemctl --root=/ set-default graphical.target
}

main() {
    parse_args "$@"
    preflight
    plan_packages
    if [[ $DRY_RUN == 0 ]]; then
        # Apt maintainer scripts expect the normal root umask. Private logs and
        # backups use mktemp plus explicit directory modes instead of inheritance.
        umask 022
        [[ ! -L /run/xc0-bootstrap ]] || die 'refusing symlinked lock directory'
        install -d -m 0700 /run/xc0-bootstrap
        [[ $(stat -c %u /run/xc0-bootstrap) == 0 ]] || die 'lock directory must be root-owned'
        exec 9>/run/xc0-bootstrap/lock
        flock -n 9 || die 'another bootstrap is running'
        install -d -m 0750 /var/log/xc0-bootstrap
        LOG_FILE=$(mktemp /var/log/xc0-bootstrap/run-XXXXXXXX.log)
        exec > >(tee -a "$LOG_FILE") 2>&1
        BACKUP_DIR=$(mktemp -d /var/backups/xc0-bootstrap-XXXXXXXX)
        trap cleanup EXIT
        trap 'result=$?; log "ERROR stage=$STEP line=$LINENO exit=$result; see $LOG_FILE" >&2; exit "$result"' ERR
    fi
    log "profile=$PROFILE suite=$SUITE user=$TARGET_USER dry-run=$DRY_RUN"
    STEP=apt-configuration; configure_apt
    STEP=packages; install_packages
    STEP=desktop-configuration; stage_config
    STEP=next-boot-services; configure_services
    if [[ $DRY_RUN == 0 ]]; then
        install -d -m 0750 /var/lib/xc0-bootstrap
        dpkg-query -W -f='${binary:Package}\t${Version}\n' > /var/lib/xc0-bootstrap/packages.tsv
        apt-cache policy "${COMMON_PACKAGES[@]}" "${HYPR_PACKAGES[@]}" "${EXTRA_PACKAGES[@]}" > /var/lib/xc0-bootstrap/apt-policy.txt
        printf 'profile=%s\nsuite=%s\nuser=%s\ncompleted=%s\n' "$PROFILE" "$SUITE" "$TARGET_USER" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /var/lib/xc0-bootstrap/last-success
        log "complete; reboot when ready. Log: $LOG_FILE; preserved system files: $BACKUP_DIR"
    else log 'dry-run complete; nothing changed (package availability/config parsing need a real target run)'; fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
