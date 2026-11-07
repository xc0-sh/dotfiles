#!/bin/sh
# preseed/include_command: refuse unknown hardware, then answer the hostname question.
# Prints the path of a generated preseed file for the installer to include.
# A non-zero exit stops the installer before anything is written to disk.

product=$(cat /sys/class/dmi/id/product_name 2>/dev/null)

case "$product" in
    20W8*)                        host=loki ;;      # ThinkPad X13 Yoga Gen 2
    "Standard PC"*|*QEMU*|*KVM*)  host=loki-vm ;;   # libvirt/QEMU test VM
    *)
        logger -t xc0 "refusing to install: unknown hardware '$product'"
        exit 1
        ;;
esac

echo "$host" > /tmp/xc0-host
printf 'd-i netcfg/hostname string %s\nd-i netcfg/get_hostname string %s\n' "$host" "$host" > /tmp/xc0-host.cfg
logger -t xc0 "hardware '$product' -> hostname $host"
echo /tmp/xc0-host.cfg
