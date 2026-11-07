#!/bin/bash
# Internal helper, deliberately run as the LOGIN USER, never root. This keeps
# user-controlled home directories/symlink races outside the root trust boundary.
set -Eeuo pipefail

link_config() {
    local source=$1 home=$2 relative destination parent backup relative_component
    [[ -d $source && -d $home && $home == /* && $home != / ]] || return 1
    while IFS= read -r relative || [[ -n $relative ]]; do
        [[ -z $relative || $relative == \#* ]] && continue
        [[ $relative != /* && $relative != *..* && $relative != *[[:space:]]* ]] || return 1
        [[ -f $source/desktop/$relative ]] || return 1
        destination=$home/.config/$relative
        parent=${destination%/*}
        # Refuse symlinked directories rather than follow them, even as the user.
        local component=$home
        local -a components
        IFS=/ read -r -a components <<< ".config/${relative%/*}"
        for relative_component in "${components[@]}"; do
            component+=/$relative_component
            [[ ! -L $component ]] || { echo "Refusing symlinked config directory: $component" >&2; return 1; }
            [[ ! -e $component || -d $component ]] || return 1
        done
        mkdir -p -- "$parent"
        if [[ -L $destination && $(readlink -- "$destination") == "$source/desktop/$relative" ]]; then
            continue
        fi
        if [[ -e $destination || -L $destination ]]; then
            backup=$destination.xc0-backup
            local n=0
            while [[ -e $backup || -L $backup ]]; do
                ((n += 1))
                backup=$destination.xc0-backup.$n
            done
            mv -T -- "$destination" "$backup"
            printf 'Preserved %s as %s\n' "$destination" "$backup"
        fi
        ln -s -- "$source/desktop/$relative" "$destination"
    done < "$source/manifest.txt"
}

main() {
    [[ $# == 2 && $(id -u) -ne 0 ]] || { echo 'Internal helper: run as login user with SOURCE HOME' >&2; return 1; }
    link_config "$1" "$2"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
