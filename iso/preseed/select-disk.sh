#!/bin/sh
# Select exactly the configured serial, never "the first disk". Production also
# requires local typed confirmation. VM mode needs BOTH a QEMU DMI and a test serial.
set -eu
# shellcheck source=/dev/null
. /xc0/install.conf
case "$MODE" in vm-test|release) : ;; *) exit 1 ;; esac
[ -n "$TARGET_SERIAL" ] || exit 1
sysfs=${XC0_SYSFS:-/sys}
found='' count=0 model=''
for dev in "$sysfs"/block/*; do
    name=${dev##*/}
    case "$name" in loop*|ram*|sr*|dm-*|md*|zram*|fd*|*boot*) continue ;; esac
    case "$(readlink -f "$dev")" in */usb*) continue ;; esac
    [ "$(cat "$dev/removable")" = 0 ] || continue
    [ "$(cat "$dev/size")" -ge 33554432 ] || continue
    serial=$(cat "$dev/serial" 2>/dev/null || cat "$dev/device/serial" 2>/dev/null || true)
    serial=$(printf '%s' "$serial" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    [ "$serial" = "$TARGET_SERIAL" ] || continue
    found=/dev/$name
    model=$(cat "$dev/device/model" 2>/dev/null || printf 'virtual disk')
    count=$((count + 1))
done
[ "$count" -eq 1 ] || exit 1
authorization="$found|$TARGET_SERIAL|$MODE|$STAGE2_REF"
if [ "${1:-}" = --validate-authorized ]; then
    [ "$(cat "$sysfs/class/dmi/id/product_name")" = "$TARGET_DMI" ] || {
        [ "$MODE" = vm-test ] || exit 1
        case "$(cat "$sysfs/class/dmi/id/product_name")" in
            "Standard PC"*|*QEMU*|*KVM*) : ;; *) exit 1 ;;
        esac
    }
    [ "$MODE" != vm-test ] || {
        [ "$TARGET_SERIAL" = XC0_TEST_DISK ] && [ "$found" = /dev/vda ] || exit 1
    }
    [ "$(cat /tmp/xc0-authorized 2>/dev/null)" = "$authorization" ]
    exit "$?"
fi
# Partman may call the hook again after its direct init guard. Recheck everything,
# but do not prompt twice for the same exact identity and immutable source.
if [ "$(cat /tmp/xc0-authorized 2>/dev/null)" = "$authorization" ]; then
    exec sh /xc0/select-disk.sh --validate-authorized
fi
case "$MODE" in
    vm-test)
        case "$(cat "$sysfs/class/dmi/id/product_name")" in
            "Standard PC"*|*QEMU*|*KVM*) : ;;
            *) exit 1 ;;
        esac
        [ "$TARGET_SERIAL" = XC0_TEST_DISK ] && [ "$found" = /dev/vda ] || exit 1
        # Both modes use the live cdebconf protocol, not SET-only helpers.
        # shellcheck source=/dev/null
        . /usr/share/debconf/confmodule
        ;;
    release)
        [ "$(cat "$sysfs/class/dmi/id/product_name")" = "$TARGET_DMI" ] || exit 1
        # shellcheck source=/dev/null
        . /usr/share/debconf/confmodule
        debconf-loadtemplate xc0 /xc0/confirm.templates
        db_subst xc0/confirm-erase DISK "$found"
        db_subst xc0/confirm-erase MODEL "$model"
        db_subst xc0/confirm-erase SERIAL "$TARGET_SERIAL"
        db_fset xc0/confirm-erase seen false
        db_input critical xc0/confirm-erase
        db_go
        db_get xc0/confirm-erase
        [ "$RET" = "ERASE $TARGET_SERIAL" ] || exit 1
        ;;
    *) exit 1 ;;
esac

# These answers are not in base.cfg: set them only after the target/consent gate.
# The stock lvm/crypto init scripts register these aliases later than 00xc0-guard.
# Register now using the real loaded template, before setting typed values/flags.
db_register partman-lvm/confirm partman-lvm/confirm_nooverwrite
db_register partman-crypto/confirm partman-crypto/confirm_nooverwrite
db_set partman-auto/disk "$found"
db_fset partman-auto/disk seen true
db_get partman-auto/disk
[ "$RET" = "$found" ] || exit 1
for question in partman-lvm/device_remove_lvm partman-md/device_remove_md \
    partman-lvm/confirm partman-lvm/confirm_nooverwrite \
    partman-partitioning/confirm_write_new_label partman-crypto/confirm partman-crypto/confirm_nooverwrite \
    partman/confirm partman/confirm_nooverwrite; do
    db_set "$question" true
    db_fset "$question" seen true
    db_get "$question"
    [ "$RET" = true ] || exit 1
done
db_set partman/choose_partition finish
db_fset partman/choose_partition seen true
db_get partman/choose_partition
[ "$RET" = finish ] || exit 1
printf '%s\n' "$authorization" > /tmp/xc0-authorized
logger -t xc0 "authorized target=$found serial=$TARGET_SERIAL mode=$MODE"
