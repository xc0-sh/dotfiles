#!/bin/sh
# Emit additional preseed answers only after the image's hardware policy matches.
# base.cfg blocks synchronously via refuse.sh if this script returns an error.
# HOSTNAME is deliberately defined in the sourced install.conf, not a shell builtin.
# shellcheck disable=SC3028
set -eu
# shellcheck source=/dev/null
. /xc0/install.conf
product=$(cat /sys/class/dmi/id/product_name)
case "$MODE" in
    vm-test)
        case "$product" in "Standard PC"*|*QEMU*|*KVM*) : ;; *) exit 1 ;; esac
        ;;
    release)
        [ -n "$TARGET_DMI" ] && [ "$product" = "$TARGET_DMI" ] || exit 1
        ;;
    *) exit 1 ;;
esac
printf '%s\n' "$HOSTNAME" > /tmp/xc0-host
printf 'd-i netcfg/hostname string %s\nd-i netcfg/get_hostname string %s\n' \
    "$HOSTNAME" "$HOSTNAME" > /tmp/xc0-host.cfg
logger -t xc0 "hardware policy accepted; profile=$PROFILE hostname=$HOSTNAME"
printf '%s\n' /tmp/xc0-host.cfg
