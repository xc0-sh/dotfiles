#!/bin/sh
# Emit additional preseed answers only after the image's hardware policy matches.
# base.cfg blocks synchronously via refuse.sh if this script returns an error.
# HOSTNAME is deliberately defined in the sourced install.conf, not a shell builtin.
# shellcheck disable=SC3028
set -eu
# shellcheck source=/dev/null
. /xc0/install.conf
case "$LAYOUT:$PROFILE:$MARIUS_UID:$MARIUS_GID:$XCLOUD_UID:$XCLOUD_GID" in
    loki:testing:1001:1001:1000:1000|thor:stable:1001:1001:1000:1000) : ;;
    *) exit 1 ;;
esac
case "$HOSTNAME:$LAYOUT" in loki:thor|thor:loki) exit 1 ;; esac
product=$(cat /sys/class/dmi/id/product_name)
[ -n "$product" ] || exit 1
case "$MODE" in
    vm-test)
        [ "${TARGET_MODE:-}" = vm-test ] || exit 1
        case "$product" in "Standard PC"*|*QEMU*|*KVM*) : ;; *) exit 1 ;; esac
        ;;
    release)
        case "${TARGET_MODE:-}" in
            serial-dmi)
                [ -n "${TARGET_SERIAL:-}" ] && [ -n "${TARGET_DMI:-}" ] &&
                    [ -z "${TARGET_MODEL:-}${PROTECTED_MODEL:-}" ] &&
                    [ "$product" = "$TARGET_DMI" ] || exit 1
                ;;
            model-confirm)
                [ "$HOSTNAME" = thor ] && [ -n "${TARGET_MODEL:-}" ] &&
                    [ -n "${PROTECTED_MODEL:-}" ] && [ "$TARGET_MODEL" != "$PROTECTED_MODEL" ] &&
                    [ -z "${TARGET_SERIAL:-}${TARGET_DMI:-}" ] || exit 1
                for value in "$TARGET_MODEL" "$PROTECTED_MODEL"; do
                    case "$value" in *'
'*) exit 1 ;; esac
                    printf '%s\n' "$value" | LC_ALL=C grep -Eq '^[a-zA-Z0-9]([a-zA-Z0-9 ._:+/-]*[a-zA-Z0-9])?$' &&
                        [ "${#value}" -le 128 ] || exit 1
                done
                # d-i BusyBox tr does not implement GNU [:lower:]/[:upper:] classes.
                if printf '%s\n' "$product" | LC_ALL=C grep -Eiq 'qemu|kvm|standard pc'; then
                    exit 1
                fi
                ;;
            *) exit 1 ;;
        esac
        ;;
    *) exit 1 ;;
esac
printf '%s\n' "$HOSTNAME" > /tmp/xc0-host
printf 'd-i netcfg/hostname string %s\nd-i netcfg/get_hostname string %s\n' \
    "$HOSTNAME" "$HOSTNAME" > /tmp/xc0-host.cfg
printf 'd-i passwd/user-uid string %s\n' "$MARIUS_UID" >> /tmp/xc0-host.cfg
cat "/xc0/recipes/$LAYOUT.cfg" >> /tmp/xc0-host.cfg
logger -t xc0 "hardware policy accepted; profile=$PROFILE hostname=$HOSTNAME"
printf '%s\n' /tmp/xc0-host.cfg
