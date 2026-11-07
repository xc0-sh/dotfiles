#!/bin/sh
# preseed/early_command: refuse unknown hardware, then set the hostname.
# A non-zero exit stops the installer before anything is written to disk.

product=$(cat /sys/class/dmi/id/product_name 2>/dev/null)

case "$product" in
    20W8*)                        host=loki ;;      # ThinkPad X13 Yoga Gen 2
    "Standard PC"*|*QEMU*|*KVM*)  host=loki-vm ;;   # libvirt/QEMU test VM
    *)
        logger -t xc0 "refusing to install: unknown hardware '$product'"
        echo "xc0: refusing to install on unknown hardware: $product" >&2
        exit 1
        ;;
esac

debconf-set netcfg/hostname "$host"
debconf-set netcfg/get_hostname "$host"
echo "$host" > /tmp/xc0-host
logger -t xc0 "hardware '$product' -> hostname $host"
