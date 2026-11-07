#!/bin/sh
# partman/early_command: pick the single internal disk to install on.
# Skips USB-attached, removable, virtual (loop/dm/md/zram) and small (<8 GiB) devices.
# Anything other than exactly one candidate stops the install.

found=""
count=0
for dev in /sys/block/*; do
    name=${dev##*/}
    case "$name" in
        loop*|ram*|sr*|dm-*|md*|zram*|fd*|mmcblk*boot*) continue ;;
    esac
    case "$(readlink -f "$dev")" in
        */usb*) continue ;;
    esac
    [ "$(cat "$dev/removable" 2>/dev/null)" = "1" ] && continue
    sectors=$(cat "$dev/size" 2>/dev/null || echo 0)
    [ "$sectors" -lt 16777216 ] && continue
    found="/dev/$name"
    count=$((count + 1))
done

if [ "$count" -ne 1 ]; then
    logger -t xc0 "need exactly one internal disk, found $count"
    echo "xc0: need exactly one internal disk, found $count" >&2
    exit 1
fi

# On real loki the internal disk is NVMe; anything else means unexpected hardware.
if [ "$(cat /tmp/xc0-host 2>/dev/null)" = "loki" ]; then
    case "$found" in
        /dev/nvme*) ;;
        *) logger -t xc0 "loki but disk is $found, not NVMe"; exit 1 ;;
    esac
fi

debconf-set partman-auto/disk "$found"
logger -t xc0 "installing to $found"
