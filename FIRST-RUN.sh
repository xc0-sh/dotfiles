#!/bin/bash
# Thor-only post-install orchestrator. It never installs NVIDIA drivers or enrolls MOK.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

MODE=check
MODE_SET=0
NO_XCCODE=0
XCCODE_RELEASE=''
XCCODE_KEYRING=''
XCCODE_FINGERPRINT=''
BLOCKED=0
STAGE=''
ROOT_STAGE=''

log() { printf 'xc0-first-run: %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 2; }
block() { log "BLOCKED: $*" >&2; BLOCKED=1; }

usage() {
    cat <<'EOF'
Usage: ./FIRST-RUN.sh [--check|--apply] [--no-xccode]
       ./FIRST-RUN.sh [--check|--apply] --xccode-release TAG \
           --signing-key FILE --signing-fingerprint FINGERPRINT

Thor post-install workflow. Run as the marius login user from the clean,
exact dotfiles checkout named by /etc/xc0-next-steps. Default is --check.

--check       Read-only preflight and, when safe, the existing bootstrap dry-run.
--apply       Explicitly stage/verify the signed xccode release, then offer the
              existing desktop bootstrap after all manual prerequisites pass.
--no-xccode   Skip xccode installation (requires the operator to choose this).

NVIDIA packages, Secure Boot MOK enrollment, and reboot remain manual,
hardware-specific steps. xccode release downloads require authenticated `gh`
access and an operator-confirmed signing-key fingerprint. Provider secrets are
never accepted on this script's command line or written by it.

The script stops before APT changes unless the root Snapper config and APT
pre/post hook are already active. It does not create a Snapper layout.
This is readiness only: Debian's hook suppresses snapshot failures, so preflight
does not prove that a later transaction successfully creates snapshots.
EOF
}

parse_args() {
    while (($#)); do
        case $1 in
            --check|--apply)
                (( MODE_SET == 0 )) || die 'choose only one of --check or --apply'
                MODE=${1#--}; MODE_SET=1; shift ;;
            --no-xccode) NO_XCCODE=1; shift ;;
            --xccode-release|--signing-key|--signing-fingerprint)
                (($# >= 2)) || die "missing value after $1"
                case $1 in
                    --xccode-release) XCCODE_RELEASE=$2 ;;
                    --signing-key) XCCODE_KEYRING=$2 ;;
                    --signing-fingerprint) XCCODE_FINGERPRINT=$2 ;;
                esac
                shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
    done
    if (( NO_XCCODE == 1 )) && [[ -n $XCCODE_RELEASE$XCCODE_KEYRING$XCCODE_FINGERPRINT ]]; then
        die '--no-xccode cannot be combined with xccode release inputs'
    fi
    if [[ -n $XCCODE_RELEASE$XCCODE_KEYRING$XCCODE_FINGERPRINT ]] &&
       [[ -z $XCCODE_RELEASE || -z $XCCODE_KEYRING || -z $XCCODE_FINGERPRINT ]]; then
        die 'xccode release, signing key, and fingerprint must be supplied together'
    fi
    if [[ -n $XCCODE_RELEASE ]] && [[ ! $XCCODE_RELEASE =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        die 'xccode release must be an explicit stable tag like v1.2.3 (never latest/main)'
    fi
    if [[ -n $XCCODE_FINGERPRINT ]] && [[ ! $XCCODE_FINGERPRINT =~ ^([[:xdigit:]]{40}|[[:xdigit:]]{64})$ ]]; then
        die 'signing fingerprint must be 40 or 64 hexadecimal characters'
    fi
}

read_stage2_ref() {
    local instructions=${1:-/etc/xc0-next-steps}
    [[ -r $instructions ]] || return 1
    awk '
        $1 == "git" && $2 == "-C" && $3 == "~/dotfiles" &&
        $4 == "checkout" && $5 == "--detach" {
            count++
            if (length($6) != 40 || $6 !~ /^[0-9a-f]+$/ || NF != 6) bad = 1
            else ref = $6
        }
        END { if (count != 1 || bad) exit 1; print ref }
    ' "$instructions"
}

is_installed() {
    local package=$1 status
    status=$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null) || return 1
    [[ $status == installed ]]
}

check_base_and_checkout() {
    local actual_ref expected_ref remote status
    [[ $EUID -ne 0 ]] || block 'run as the marius login user, not root'
    [[ $(id -un) == marius ]] || block 'the selected login user must be marius'
    [[ $(hostname -s) == thor ]] || block 'this entrypoint is Thor-specific; hostname must be thor'
    grep -qx 'ID=debian' /etc/os-release 2>/dev/null || block 'OS must be Debian'
    grep -qx 'VERSION_CODENAME=trixie' /etc/os-release 2>/dev/null || block 'OS must be Debian 13/trixie'
    for item in 'state=complete' 'stage=base' 'profile=stable' 'hostname=thor' 'layout=thor'; do
        grep -qx "$item" /var/lib/xc0/install-status 2>/dev/null || block "base install status is missing $item"
    done
    if ! expected_ref=$(read_stage2_ref); then
        block 'cannot obtain one exact reviewed commit from /etc/xc0-next-steps'
        return
    fi
    if ! REPO_ROOT=$(git -C "$(dirname -- "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null); then
        block 'run from a Git checkout of xc0-sh/dotfiles'
        return
    fi
    REPO_ROOT=$(cd -- "$REPO_ROOT" && pwd -P)
    [[ $REPO_ROOT == "$HOME/dotfiles" ]] || block "checkout must be $HOME/dotfiles (got $REPO_ROOT)"
    actual_ref=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)
    [[ $actual_ref == "$expected_ref" ]] || block "checkout HEAD must match /etc/xc0-next-steps ($expected_ref)"
    remote=$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)
    case $remote in
        https://github.com/xc0-sh/dotfiles.git|git@github.com:xc0-sh/dotfiles.git) : ;;
        *) block 'origin must be the reviewed xc0-sh/dotfiles repository' ;;
    esac
    if ! status=$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all 2>/dev/null); then
        block 'cannot inspect dotfiles checkout status; no changes will be made'
        return
    fi
    [[ -z $status ]] || block 'dotfiles checkout must be clean; preserve local changes and stop'
}

snapper_config_valid() {
    # Parse literals only: the APT hook sources this file, but preflight must not.
    awk -v kind="$1" '
        /^[[:space:]]*(#|$)/ { next }
        {
            line = $0
            sub(/^[[:space:]]*(export[[:space:]]+)?/, "", line)
            if (line !~ /^[A-Za-z_][A-Za-z_0-9]*=/) { bad = 1; next }
            name = line; sub(/=.*/, "", name)
            if (kind == "defaults" && name != "SNAPPER_CONFIGS" && name != "DISABLE_APT_SNAPSHOT") bad = 1
            value = substr(line, length(name) + 2)
            sub(/[[:space:]]+#.*$/, "", value)
            sub(/[[:space:]]+$/, "", value)
            quote = substr(value, 1, 1)
            if (quote == "\"" || quote == "\047") {
                if (length(value) < 2 || substr(value, length(value), 1) != quote) {
                    bad = 1; next
                }
                value = substr(value, 2, length(value) - 2)
            } else if (value ~ /[[:space:]]/) { bad = 1 }
            if (value ~ /[^A-Za-z_0-9 .:\/-]/) bad = 1
            if (name == "DISABLE_APT_SNAPSHOT" && tolower(value) != "no" && value != "") bad = 1
            if (name == "SUBVOLUME") { roots++; if (value != "/") bad = 1 }
        }
        END { exit (bad || (kind == "root" && roots != 1)) ? 1 : 0 }
    '
}

check_snapper() {
    local root_config=${1:-/etc/snapper/configs/root}
    local hook=${2:-/etc/apt/apt.conf.d/80snapper}
    local defaults=${3:-/etc/default/snapper} helper=${4:-/usr/bin/snapper}
    local common pre post effective settings
    if ! is_installed snapper || [[ ! -s $root_config || ! -s $hook || ! -x $helper ]]; then
        block 'root Snapper config and /etc/apt/apt.conf.d/80snapper are required before any APT transaction; prepare them separately'
        return
    fi
    # Debian trixie snapper 0.10.6-1.2 ships inline commands, not apt-snapshot.
    # Require that supported distro form AND its presence in APT effective config;
    # comments, #clear in a later file, and arbitrary replacement shell are not proof.
    # shellcheck disable=SC2016 # Literal distro hook, never executed by preflight.
    common='if [ -e /etc/default/snapper ]; then . /etc/default/snapper; fi; if [ -x /usr/bin/snapper ] && [ ! x$DISABLE_APT_SNAPSHOT = '\''xyes'\'' ]'
    pre="$common && [ -e /etc/snapper/configs/root ]; then rm -f /var/tmp/snapper-apt || true ; snapper create -d apt -c number -t pre -p > /var/tmp/snapper-apt || true ; snapper cleanup number || true ; fi"
    post="$common && [ -e /var/tmp/snapper-apt ]; then snapper create -d apt -c number -t post --pre-number=\`cat /var/tmp/snapper-apt\` || true ; snapper cleanup number || true ; fi"
    if ! awk -v pre="DPkg::Pre-Invoke  { \"$pre\"; };" -v post="DPkg::Post-Invoke { \"$post\"; };" '
        { sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, "") }
        /^(#|\/\/|$)/ { next }
        $0 == pre { before++; next }
        $0 == post { after++; next }
        { bad = 1 }
        END { exit (bad || before != 1 || after != 1) ? 1 : 0 }
    ' "$hook"; then
        block '80snapper must contain the supported Debian pre/post snapshot hooks; custom or disabled hooks require separate review'
        return
    fi
    if ! settings=$(sudo cat -- "$root_config") || ! snapper_config_valid root <<< "$settings"; then
        block 'Snapper root config must use literal assignments and name SUBVOLUME="/" exactly once'
        return
    fi
    if ! effective=$(sudo apt-config dump) ||
       ! grep -Fxq "DPkg::Pre-Invoke:: \"$pre\";" <<< "$effective" ||
       ! grep -Fxq "DPkg::Post-Invoke:: \"$post\";" <<< "$effective"; then
        block 'effective APT configuration does not enable both Debian Snapper hooks'
        return
    fi
    if ! sudo test ! -e "$defaults"; then
        if ! settings=$(sudo cat -- "$defaults") || ! snapper_config_valid defaults <<< "$settings"; then
            block 'Snapper defaults must use safe literal assignments and must not disable APT snapshots'
            return
        fi
    fi
    if [[ ${DISABLE_APT_SNAPSHOT:-} == yes ]] ||
       ! settings=$(sudo sh -c 'printf "%s" "${DISABLE_APT_SNAPSHOT-}"') ||
       [[ $settings == yes ]]; then
        block 'DISABLE_APT_SNAPSHOT=yes inhibits APT snapshots'
        return
    fi
    if ! sudo snapper -c root list >/dev/null 2>&1; then
        block 'Snapper root config is not operational; no package changes will be made'
    fi
}

check_physical_thor() {
    local devices
    if systemd-detect-virt --vm >/dev/null 2>&1; then
        block 'refusing NVIDIA/desktop preparation in a VM'
        return
    fi
    if ! command -v lspci >/dev/null 2>&1; then
        block 'lspci is required to verify the physical NVIDIA device'
        return
    fi
    devices=$(lspci -nn -d 10de: 2>/dev/null || true)
    [[ -n $devices ]] || block 'no physical NVIDIA PCI device detected'
    if [[ -n $devices ]] && ! grep -qi 'RTX 3080' <<< "$devices"; then
        block 'NVIDIA PCI device does not identify as the Thor RTX 3080; inspect hardware before proceeding'
    fi
}

check_nvidia_prepared() {
    local package version signer candidate secureboot dkms_status
    for package in nvidia-driver-pinning-595 nvidia-open nvidia-driver nvidia-settings \
                   nvidia-driver-libs:amd64 nvidia-driver-libs:i386; do
        is_installed "$package" || block "manual NVIDIA preparation incomplete: package not installed: $package"
    done
    if is_installed nvidia-open; then
        version=$(dpkg-query -W -f='${Version}' nvidia-open 2>/dev/null || true)
        [[ $version == 595.* ]] || block "nvidia-open must be the pinned 595 branch (installed: ${version:-unknown})"
    fi
    if command -v apt-cache >/dev/null 2>&1; then
        candidate=$(apt-cache policy nvidia-open 2>/dev/null | awk '/Candidate:/ {print $2; exit}')
        [[ $candidate == 595.* ]] || block "signed APT candidate must remain on branch 595 (candidate: ${candidate:-unknown})"
    fi
    if ! command -v mokutil >/dev/null 2>&1; then
        block 'mokutil is required to verify Secure Boot and MOK enrollment'
    else
        secureboot=$(mokutil --sb-state 2>/dev/null || true)
        grep -qi 'SecureBoot enabled' <<< "$secureboot" || block 'Secure Boot must remain enabled and report enabled'
    fi
    dkms_status=$(dkms status -m nvidia 2>/dev/null || true)
    grep -q 'installed' <<< "$dkms_status" || block 'NVIDIA DKMS module is not installed for a kernel'
    signer=$(modinfo -F signer nvidia 2>/dev/null || true)
    [[ -n $signer ]] || block 'NVIDIA kernel module has no reported signer'
    [[ -r /var/lib/dkms/mok.pub ]] || block 'DKMS MOK public key is missing'
    if command -v mokutil >/dev/null 2>&1 && [[ -r /var/lib/dkms/mok.pub ]]; then
        mokutil --test-key /var/lib/dkms/mok.pub >/dev/null 2>&1 || block 'DKMS MOK is not enrolled; use physical MokManager enrollment, then rerun'
    fi
    if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi -L 2>/dev/null | grep -qi 'RTX 3080'; then
        block 'nvidia-smi does not report the Thor RTX 3080; driver has not passed hardware validation'
    fi
}

check_debian_only_apt_policy() {
    # Reuse the bootstrap's exact APT-origin validator even in --check mode. The
    # bootstrap itself only validates apt-cache policy during its apply path.
    if ! sudo bash -c '
        set -Eeuo pipefail
        source "$1"
        PROFILE=thor SUITE=trixie
        check_apt_suites <<< "$(apt-cache policy)"
    ' bash "$REPO_ROOT/setup/bootstrap.sh"; then
        block 'this stage accepts Debian trixie/backports only; FIRST-RUN will not disable or modify NVIDIA/third-party APT sources'
        return 1
    fi
}

validate_xccode_key() {
    local actual resolved
    [[ -n $XCCODE_KEYRING && -r $XCCODE_KEYRING && -f $XCCODE_KEYRING && ! -L $XCCODE_KEYRING ]] || {
        block 'provide a readable, regular public signing-key file (not a symlink)'; return
    }
    if ! resolved=$(readlink -f -- "$XCCODE_KEYRING"); then
        block 'cannot resolve an absolute xccode signing-key path'; return
    fi
    XCCODE_KEYRING=$resolved
    command -v gpg >/dev/null 2>&1 || { block 'gpg is required to inspect the xccode release signing key'; return; }
    if ! actual=$(gpg --batch --with-colons --show-keys "$XCCODE_KEYRING" 2>/dev/null); then
        block 'cannot inspect the xccode signing key'; return
    fi
    awk -F: -v wanted="${XCCODE_FINGERPRINT^^}" '
        $1 == "pub" { primary = 1; next }
        $1 == "sub" { primary = 0 }
        primary && $1 == "fpr" { if (toupper($10) == wanted) found = 1; primary = 0 }
        END { exit found ? 0 : 1 }
    ' <<< "$actual" ||
        block 'local xccode key fingerprint does not match the out-of-band-confirmed fingerprint'
}

verify_xccode_signature() {
    local keyring=$1 signature=$2 payload=$3 status
    local -a runner=()
    [[ ${4:-} != root ]] || runner=(sudo)
    [[ $keyring == /* ]] || { log 'ERROR: xccode keyring path must be absolute' >&2; return 1; }
    if ! status=$("${runner[@]}" gpgv --status-fd 1 --keyring "$keyring" "$signature" "$payload"); then
        log 'ERROR: xccode release signature verification failed' >&2
        return 1
    fi
    # VALIDSIG names the signing key first and its primary key last (when a
    # subkey signs). gpgv success alone also trusts unrelated keys in a keyring.
    if ! awk -v wanted="${XCCODE_FINGERPRINT^^}" '
        function fingerprint(s) {
            return (length(s) == 40 || length(s) == 64) && s !~ /[^0-9A-Fa-f]/
        }
        $1 != "[GNUPG:]" { bad = 1 }
        $2 ~ /^(BADSIG|ERRSIG|EXPSIG|EXPKEYSIG|REVKEYSIG|NO_PUBKEY|NODATA|FAILURE|ERROR)$/ { bad = 1 }
        $2 == "VALIDSIG" {
            valid++
            if ((NF != 11 && NF != 12) || !fingerprint($3) ||
                $4 !~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$/ ||
                $11 !~ /^[0-9A-Fa-f][0-9A-Fa-f]$/) bad = 1
            for (i = 5; i <= 10; i++) if ($i !~ /^[0-9]+$/) bad = 1
            primary = NF == 12 ? $12 : $3
            if (!fingerprint(primary) || toupper(primary) != wanted) bad = 1
        }
        END { exit (bad || !valid) ? 1 : 0 }
    ' <<< "$status"; then
        log 'ERROR: xccode signature status is malformed or not signed by the confirmed primary key' >&2
        return 1
    fi
}

validate_xccode_manifest() {
    local manifest=$1 asset=$2
    local -a runner=()
    [[ ${3:-} != root ]] || runner=(sudo)
    "${runner[@]}" cat -- "$manifest" | awk -v asset="$asset" '
        /^[[:space:]]*(#|$)/ { next }
        {
            name = $2; sub(/^\*/, "", name)
            if (NF != 2 || length($1) != 64 || $1 ~ /[^0-9a-fA-F]/ ||
                name !~ /^[A-Za-z0-9][A-Za-z0-9_.+-]*$/) bad = 1
            if (name == asset) covered++
        }
        END { exit (bad || covered != 1) ? 1 : 0 }
    '
}

validate_xccode_archive() {
    local archive=$1
    local -a runner=()
    [[ ${2:-} != root ]] || runner=(sudo)
    # fetch-release.sh later extracts this ORIGINAL archive as root, preserving
    # metadata. Accept the release's root:root group-write bits, not user owners,
    # world writes, setid, unsafe paths, links, devices, or ambiguous listings.
    LC_ALL=C "${runner[@]}" tar --numeric-owner --full-time --quoting-style=escape -tvzf "$archive" | awk '
        {
            name = $6; sub(/\/$/, "", name); sub(/^\.\//, "", name)
            if (NF != 6 || $1 !~ /^[-d][r-][w-][x-][r-][w-][x-][r-]-[x-]$/ ||
                $2 != "0/0" || name !~ /^[A-Za-z0-9_.+@\/-]+$/ ||
                name ~ /^\// || name ~ /(^|\/)\.\.?($|\/)/ || name ~ /\/\// ||
                seen[name]++) bad = 1
            count++
        }
        END { exit (bad || !count) ? 1 : 0 }
    ' || return 1
    # Root tar also resolves archive uname/gname by name. Numeric 0/0 must
    # not conceal an unprivileged name that downstream extraction would use.
    LC_ALL=C "${runner[@]}" tar --full-time --quoting-style=escape -tvzf "$archive" | awk '
        $2 !~ /^(root|0)\/(root|0)$/ { bad = 1 }
        END { exit bad ? 1 : 0 }
    '
}

run_xccode_installer() {
    local installer=$1 tei_version=$2
    shift 2
    # Only the installer inherits 022: staging, signatures and archives stay private.
    sudo env XCCODE_SIGNING_KEY="$ROOT_STAGE/signing-key" \
        RELEASE_URL="file://$ROOT_STAGE" \
        TEI_URL="file://$ROOT_STAGE/tei-$tei_version/text-embeddings-router" \
        sh -c 'umask 022; exec sh "$@"' sh "$installer" \
        --operator marius --release "$XCCODE_RELEASE" "$@"
}

check_xccode_tools() {
    local command
    for command in gh gpg gpgv sha256sum tar; do
        command -v "$command" >/dev/null 2>&1 || block "required xccode release tool is missing: $command"
    done
    if command -v gh >/dev/null 2>&1 && ! gh auth status --hostname github.com --no-refresh >/dev/null 2>&1; then
        block 'authenticate gh with read access to private xc0-sh/xccode; do not pass a token to this script'
    fi
}

collect_xccode_inputs() {
    [[ $NO_XCCODE == 1 ]] && return
    if [[ -z $XCCODE_RELEASE || -z $XCCODE_KEYRING || -z $XCCODE_FINGERPRINT ]]; then
        [[ -t 0 ]] || die 'interactive xccode inputs required: pass --xccode-release, --signing-key and --signing-fingerprint'
        [[ -n $XCCODE_RELEASE ]] || read -r -p 'Signed xccode release tag (for example v1.1.5): ' XCCODE_RELEASE
        [[ -n $XCCODE_KEYRING ]] || read -r -p 'Path to the operator-confirmed public signing key: ' XCCODE_KEYRING
        [[ -n $XCCODE_FINGERPRINT ]] || read -r -p 'Fingerprint independently confirmed out of band: ' XCCODE_FINGERPRINT
    fi
    [[ $XCCODE_RELEASE =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'xccode tag must be explicit stable semver (never latest/main)'
    [[ $XCCODE_FINGERPRINT =~ ^([[:xdigit:]]{40}|[[:xdigit:]]{64})$ ]] || die 'invalid xccode key fingerprint'
}

ask_to_continue() {
    local expected=$1 response
    [[ -t 0 ]] || die 'confirmation requires an interactive terminal'
    read -r -p "Type '$expected' to continue: " response
    [[ $response == "$expected" ]] || die 'confirmation did not match; no next stage run'
}

stage_xccode_release() {
    local release_dir tei_dir tarball version_lock tei_version tei_sha tei_asset
    local actual_sha operator_gid installer
    check_xccode_tools
    validate_xccode_key
    (( BLOCKED == 0 )) || return 1
    STAGE=$(mktemp -d "${TMPDIR:-/tmp}/xc0-first-run.XXXXXXXX")
    chmod 0700 "$STAGE"
    [[ $STAGE != *[[:space:]]* ]] || die 'temporary staging path must not contain whitespace'
    release_dir=$STAGE/$XCCODE_RELEASE
    mkdir -m 0700 "$release_dir"
    log "downloading explicitly selected private xccode release $XCCODE_RELEASE using gh"
    gh release download "$XCCODE_RELEASE" --repo xc0-sh/xccode --dir "$release_dir" \
        --pattern "xccode-$XCCODE_RELEASE.tar.gz" --pattern SHA256SUMS --pattern SHA256SUMS.sig
    tarball=$release_dir/xccode-$XCCODE_RELEASE.tar.gz
    [[ -f $tarball && -f $release_dir/SHA256SUMS && -f $release_dir/SHA256SUMS.sig ]] ||
        die 'release assets incomplete'
    verify_xccode_signature "$XCCODE_KEYRING" "$release_dir/SHA256SUMS.sig" "$release_dir/SHA256SUMS"
    validate_xccode_manifest "$release_dir/SHA256SUMS" "xccode-$XCCODE_RELEASE.tar.gz" ||
        die 'signed manifest must cover the exact selected xccode tarball basename once'
    (cd "$release_dir" && sha256sum -c SHA256SUMS)
    validate_xccode_archive "$tarball" ||
        die 'unsafe xccode archive: require root:root files/directories, safe paths, no world-write, setid, links or special files'
    version_lock=$STAGE/versions.lock
    tar -xOf "$tarball" etc/versions.lock > "$version_lock"
    tei_version=$(awk '
        /^\[tei\]$/ { in_section=1; next }
        /^\[/ { in_section=0 }
        in_section && /^version[[:space:]]*=/ { gsub(/["[:space:]]/, "", $3); print $3; exit }
    ' "$version_lock")
    tei_sha=$(awk '
        /^\[tei\]$/ { in_section=1; next }
        /^\[/ { in_section=0 }
        in_section && /^sha256[[:space:]]*=/ { gsub(/["[:space:]]/, "", $3); print $3; exit }
    ' "$version_lock")
    [[ $tei_version =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ && $tei_sha =~ ^[0-9a-f]{64}$ ]] ||
        die 'signed xccode release has invalid TEI version/checksum pins'
    tei_dir=$STAGE/tei-$tei_version
    mkdir -m 0700 "$tei_dir"
    gh release download "tei-$tei_version" --repo xc0-sh/xccode --dir "$tei_dir" \
        --pattern text-embeddings-router
    tei_asset=$tei_dir/text-embeddings-router
    [[ -f $tei_asset ]] || die 'signed xccode release TEI asset missing'
    actual_sha=$(sha256sum "$tei_asset" | awk '{print $1}')
    [[ $actual_sha == "$tei_sha" ]] || die 'TEI checksum does not match the signed xccode release lock'
    # Re-copy to a root-owned staging directory and verify again before running privileged code.
    ROOT_STAGE=$(sudo mktemp -d /var/tmp/xc0-xccode-verified.XXXXXXXX)
    sudo chmod 0700 "$ROOT_STAGE"
    sudo install -d -m 0700 "$ROOT_STAGE/$XCCODE_RELEASE" "$ROOT_STAGE/tei-$tei_version" "$ROOT_STAGE/src"
    sudo install -m 0600 "$tarball" "$ROOT_STAGE/$XCCODE_RELEASE/xccode-$XCCODE_RELEASE.tar.gz"
    sudo install -m 0600 "$release_dir/SHA256SUMS" "$ROOT_STAGE/$XCCODE_RELEASE/SHA256SUMS"
    sudo install -m 0600 "$release_dir/SHA256SUMS.sig" "$ROOT_STAGE/$XCCODE_RELEASE/SHA256SUMS.sig"
    sudo install -m 0600 "$XCCODE_KEYRING" "$ROOT_STAGE/signing-key"
    sudo install -m 0600 "$tei_asset" "$ROOT_STAGE/tei-$tei_version/text-embeddings-router"
    verify_xccode_signature "$ROOT_STAGE/signing-key" \
        "$ROOT_STAGE/$XCCODE_RELEASE/SHA256SUMS.sig" "$ROOT_STAGE/$XCCODE_RELEASE/SHA256SUMS" root
    validate_xccode_manifest "$ROOT_STAGE/$XCCODE_RELEASE/SHA256SUMS" "xccode-$XCCODE_RELEASE.tar.gz" root ||
        die 'root-stage signed manifest does not cover the exact selected xccode tarball basename once'
    sudo sh -c 'cd "$1" && sha256sum -c SHA256SUMS' _ "$ROOT_STAGE/$XCCODE_RELEASE"
    actual_sha=$(sudo sha256sum "$ROOT_STAGE/tei-$tei_version/text-embeddings-router" | awk '{print $1}')
    [[ $actual_sha == "$tei_sha" ]] || die 'root-stage TEI checksum mismatch'
    # Check the root-owned copy, not mutable user-stage metadata. No archive links
    # or special files may bypass ownership/mode normalization or escape src.
    validate_xccode_archive "$ROOT_STAGE/$XCCODE_RELEASE/xccode-$XCCODE_RELEASE.tar.gz" root ||
        die 'unsafe root-stage xccode archive metadata or paths'
    sudo tar --no-same-owner --no-same-permissions \
        -xzf "$ROOT_STAGE/$XCCODE_RELEASE/xccode-$XCCODE_RELEASE.tar.gz" -C "$ROOT_STAGE/src"
    operator_gid=$(id -g marius)
    [[ $operator_gid =~ ^[0-9]+$ && $operator_gid != 0 ]] || die 'marius must have a non-root primary group'
    sudo chown -R "0:$operator_gid" "$ROOT_STAGE/src"
    sudo find "$ROOT_STAGE/src" -type d -exec chmod 0750 {} +
    sudo find "$ROOT_STAGE/src" -type f -perm /111 -exec chmod 0750 {} +
    sudo find "$ROOT_STAGE/src" -type f ! -perm /111 -exec chmod 0640 {} +
    sudo chown "0:$operator_gid" "$ROOT_STAGE"
    sudo chmod 0710 "$ROOT_STAGE"
    installer=$ROOT_STAGE/src/install/install.sh
    [[ -f $installer && -r $installer && -f $ROOT_STAGE/src/install/fetch-release.sh ]] ||
        die 'verified release layout is unexpected or unreadable by marius'

    run_xccode_installer "$installer" "$tei_version" --check
    ask_to_continue "INSTALL XCCODE $XCCODE_RELEASE"
    run_xccode_installer "$installer" "$tei_version"
    log "xccode $XCCODE_RELEASE installed from verified signed assets"
    log 'operator must configure provider secrets interactively; this script never accepts or stores them'
}

cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n $ROOT_STAGE ]]; then
        [[ $ROOT_STAGE == /var/tmp/xc0-xccode-verified.* ]] || {
            log "refusing cleanup outside the generated xccode staging path: $ROOT_STAGE" >&2
            status=1
        }
        if [[ $ROOT_STAGE == /var/tmp/xc0-xccode-verified.* ]]; then
            sudo rm -rf -- "$ROOT_STAGE" || status=1
        fi
    fi
    [[ -z $STAGE ]] || rm -rf -- "$STAGE"
    exit "$status"
}

run_bootstrap() {
    check_debian_only_apt_policy || return 1
    sudo bash "$REPO_ROOT/setup/bootstrap.sh" --profile thor --user marius --nvidia-prepared --dry-run
    [[ $MODE == apply ]] || return 0
    ask_to_continue 'APPLY THOR DESKTOP'
    sudo bash "$REPO_ROOT/setup/bootstrap.sh" --profile thor --user marius --nvidia-prepared
    log 'desktop bootstrap finished; reboot only when the operator is ready'
}

main() {
    parse_args "$@"
    if [[ $EUID == 0 ]]; then die 'run as marius, not root' ; fi
    if [[ $(id -un) != marius ]]; then die 'run as the marius login user' ; fi
    trap cleanup EXIT
    log "mode=$MODE; no driver installation or MOK enrollment is performed by this script"
    check_base_and_checkout
    check_snapper
    check_physical_thor
    (( BLOCKED == 0 )) || { log 'no package changes made; resolve the blockers and rerun'; exit 3; }

    if (( NO_XCCODE == 0 )); then
        check_debian_only_apt_policy || { log 'no xccode install attempted'; exit 3; }
    fi

    if [[ $NO_XCCODE == 0 ]]; then
        if [[ $MODE == apply ]]; then
            collect_xccode_inputs
            stage_xccode_release
        elif [[ -n $XCCODE_RELEASE ]]; then
            check_xccode_tools
            validate_xccode_key
            (( BLOCKED == 0 )) || exit 3
            log "signed xccode release $XCCODE_RELEASE inputs look ready; --check does not download assets"
        else
            log 'xccode release not selected in --check; --apply prompts for an explicit tag, key, and out-of-band-confirmed fingerprint'
        fi
    fi

    BLOCKED=0
    check_nvidia_prepared
    if (( BLOCKED != 0 )); then
        log 'NVIDIA preparation is manual; install/pin branch 595 from the supported Debian 13 NVIDIA repository, keep Secure Boot enabled, enroll the DKMS MOK physically, reboot, then rerun'
        log 'no desktop bootstrap run; no NVIDIA package, repository, kernel, or MOK changes made by FIRST-RUN.sh'
        exit 3
    fi
    run_bootstrap
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
