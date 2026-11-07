#!/bin/sh
# Select exactly one disk under an explicit policy, never "the first disk".
# Release requires ERASE plus the live serial; VM needs QEMU and a test identity.
# HOSTNAME is supplied by install.conf, not a shell builtin.
# shellcheck disable=SC3028
set -eu
# The layout must come from this image, not an inherited caller environment.
LAYOUT=''
# shellcheck source=/dev/null
. /xc0/install.conf
case "${1:-}" in ''|--validate-authorized) : ;; *) exit 1 ;; esac
case "$LAYOUT" in thor|loki) : ;; *) exit 1 ;; esac
case "$HOSTNAME" in thor|loki) [ "$LAYOUT" = "$HOSTNAME" ] || exit 1 ;; esac
expected_recipe=xc0-$LAYOUT-crypto-btrfs
sysfs=${XC0_SYSFS:-/sys}
product=$(cat "$sysfs/class/dmi/id/product_name")
[ -n "$product" ] || exit 1
case "$MODE:${TARGET_MODE:-}" in
    vm-test:vm-test)
        [ "$TARGET_SERIAL" = XC0_TEST_DISK ] && [ "$TARGET_DMI" = QEMU ] &&
            [ -z "${TARGET_MODEL:-}${PROTECTED_MODEL:-}" ] || exit 1
        case "$product" in "Standard PC"*|*QEMU*|*KVM*) : ;; *) exit 1 ;; esac
        ;;
    release:serial-dmi)
        [ -n "${TARGET_SERIAL:-}" ] && [ -n "${TARGET_DMI:-}" ] &&
            [ -z "${TARGET_MODEL:-}${PROTECTED_MODEL:-}" ] &&
            [ "$product" = "$TARGET_DMI" ] || exit 1
        ;;
    release:model-confirm)
        [ "$HOSTNAME" = thor ] && [ -n "${TARGET_MODEL:-}" ] &&
            [ -n "${PROTECTED_MODEL:-}" ] && [ "$TARGET_MODEL" != "$PROTECTED_MODEL" ] &&
            [ -z "${TARGET_SERIAL:-}${TARGET_DMI:-}" ] || exit 1
        for value in "$TARGET_MODEL" "$PROTECTED_MODEL"; do
            case "$value" in *'
'*) exit 1 ;; esac
            printf '%s\n' "$value" | LC_ALL=C grep -Eq '^[a-zA-Z0-9]([a-zA-Z0-9 ._:+/-]*[a-zA-Z0-9])?$' &&
                [ "${#value}" -le 128 ] || exit 1
        done
        case "$(printf '%s' "$product" | tr '[:lower:]' '[:upper:]')" in
            *QEMU*|*KVM*|*"STANDARD PC"*) exit 1 ;;
        esac
        ;;
    *) exit 1 ;;
esac
[ -n "$STAGE2_REF" ] || exit 1
found='' count=0 matched_serial='' matched_model='' protected_count=0
for dev in "$sysfs"/block/*; do
    name=${dev##*/}
    case "$name" in loop*|ram*|sr*|dm-*|md*|zram*|fd*|*boot*) continue ;; esac
    model=$(cat "$dev/device/model" 2>/dev/null || cat "$dev/model" 2>/dev/null || true)
    model=$(printf '%s' "$model" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    serial=$(cat "$dev/serial" 2>/dev/null || cat "$dev/device/serial" 2>/dev/null || true)
    serial=$(printf '%s' "$serial" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    sectors=$(cat "$dev/size" 2>/dev/null || true)
    sectors=$(printf '%s' "$sectors" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    protected=false
    # The known 2 TB data disk is never an install target, even under serial-dmi.
    case "$model" in *WDS200T1X0E*) protected=true ;; esac
    if [ "$TARGET_MODE" = model-confirm ]; then
        case "$model" in *"$PROTECTED_MODEL"*) protected=true ;; esac
    fi
    if [ "$protected" = true ]; then
        protected_count=$((protected_count + 1))
        logger -t xc0 "protected disk=/dev/$name model=$model serial=$serial sectors=$sectors; excluded"
        continue
    fi
    case "$(readlink -f "$dev")" in */usb*) continue ;; esac
    [ "$(cat "$dev/removable")" = 0 ] || continue
    case "$sectors" in ''|*[!0-9]*) continue ;; esac
    if [ "$TARGET_MODE" = model-confirm ]; then
        case "$model" in *"$TARGET_MODEL"*) : ;; *) continue ;; esac
        if [ "$sectors" -lt 1750000000 ] || [ "$sectors" -gt 2100000000 ]; then
            continue
        fi
    else
        [ "$sectors" -ge 33554432 ] || continue
        [ "$serial" = "$TARGET_SERIAL" ] || continue
    fi
    found=/dev/$name
    matched_serial=$serial matched_model=$model
    count=$((count + 1))
done
[ "$count" -eq 1 ] || exit 1
[ -n "$matched_serial" ] || exit 1
# No control characters or field separators may make the marker ambiguous.
case "$matched_serial$matched_model" in *'|'*|*'
'*) exit 1 ;; esac
if printf '%s' "$matched_serial$matched_model" | LC_ALL=C grep -q '[^ -~]'; then exit 1; fi
if [ "$MODE" = vm-test ]; then [ "$found" = /dev/vda ] || exit 1; fi
authorization="$found|$matched_serial|$MODE|$STAGE2_REF|$TARGET_MODE|$TARGET_MODEL|$PROTECTED_MODEL|$matched_model|$TARGET_DMI|$HOSTNAME|$LAYOUT"
remove_existing=true
# d-i's LVM/MD removal can span disks. With a protected disk present, never
# auto-authorize that global removal; stop for operator cleanup if needed.
if [ "$protected_count" -gt 0 ]; then remove_existing=false; fi
# Validation also uses the live cdebconf GET protocol, never a SET-only helper.
# shellcheck source=/dev/null
. /usr/share/debconf/confmodule
confirmation_questions='partman-lvm/confirm partman-lvm/confirm_nooverwrite
partman-partitioning/confirm_write_new_label partman-crypto/confirm partman-crypto/confirm_nooverwrite
partman/confirm partman/confirm_nooverwrite'
validate_layout() {
    db_get partman-auto/method || return 1
    [ "$RET" = crypto ] || return 1
    db_get partman-auto/choose_recipe || return 1
    [ "$RET" = "$expected_recipe" ] || return 1
}
validate_persistent_answers() {
    validate_layout || return 1
    db_get partman-auto/disk || return 1
    [ "$RET" = "$found" ] || return 1
    db_get grub-installer/bootdev || return 1
    [ "$RET" = "$found" ] || return 1
    for question in partman-lvm/device_remove_lvm partman-md/device_remove_md; do
        db_get "$question" || return 1
        [ "$RET" = "$remove_existing" ] || return 1
    done
}
validate_partition_choice() {
    db_get partman/choose_partition || return 1
    [ "$RET" = finish ] || return 1
}
validate_confirmation_answers() {
    for question in $confirmation_questions; do
        db_get "$question" || return 1
        [ "$RET" = true ] || return 1
    done
}
validate_authorized() {
    [ "$(cat /tmp/xc0-authorized 2>/dev/null)" = "$authorization" ] || return 1
    # partman-base resets its one-shot destructive confirmations after accepting
    # them. Authorization and persistent target/layout answers must survive;
    # the consumed confirmations must not be treated as durable state.
    validate_persistent_answers
}
if [ "${1:-}" = --validate-authorized ]; then
    validate_authorized
    exit "$?"
fi
# Partman may call the hook again after its direct init guard. Recheck everything,
# but do not prompt twice for the same exact identity and immutable source.
if [ "$(cat /tmp/xc0-authorized 2>/dev/null)" = "$authorization" ]; then
    validate_authorized
    exit "$?"
fi
validate_layout
case "$MODE" in
    vm-test) : ;;
    release)
        debconf-loadtemplate xc0 /xc0/confirm.templates
        db_subst xc0/confirm-erase DISK "$found"
        db_subst xc0/confirm-erase MODEL "$matched_model"
        db_subst xc0/confirm-erase SERIAL "$matched_serial"
        db_fset xc0/confirm-erase seen false
        db_input critical xc0/confirm-erase
        db_go
        db_get xc0/confirm-erase
        [ "$RET" = "ERASE $matched_serial" ] || exit 1
        ;;
    *) exit 1 ;;
esac

validate_layout
# These answers are not in base.cfg: set them only after the target/consent gate.
# The stock lvm/crypto init scripts register these aliases later than 00xc0-guard.
# Register now using the real loaded template, before setting typed values/flags.
db_register partman-lvm/confirm partman-lvm/confirm_nooverwrite
db_register partman-crypto/confirm partman-crypto/confirm_nooverwrite
db_set partman-auto/disk "$found"
db_fset partman-auto/disk seen true
db_get partman-auto/disk
[ "$RET" = "$found" ] || exit 1
db_set grub-installer/bootdev "$found"
db_fset grub-installer/bootdev seen true
db_get grub-installer/bootdev
[ "$RET" = "$found" ] || exit 1
for question in partman-lvm/device_remove_lvm partman-md/device_remove_md; do
    db_set "$question" "$remove_existing"
    db_fset "$question" seen true
    db_get "$question"
    [ "$RET" = "$remove_existing" ] || exit 1
done
for question in $confirmation_questions; do
    db_set "$question" true
    db_fset "$question" seen true
    db_get "$question"
    [ "$RET" = true ] || exit 1
done
    db_set partman/choose_partition finish
db_fset partman/choose_partition seen true
db_get partman/choose_partition
[ "$RET" = finish ] || exit 1
validate_confirmation_answers
validate_partition_choice
validate_persistent_answers
umask 077
printf '%s\n' "$authorization" > /tmp/xc0-authorized
logger -t xc0 "authorized target=$found model=$matched_model serial=$matched_serial mode=$MODE target_mode=$TARGET_MODE"
