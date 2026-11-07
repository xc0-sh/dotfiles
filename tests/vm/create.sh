#!/bin/sh
# Make a fresh, watchable UEFI base-test VM. Never attach a host block device.
# Optional XC0_VM_3D=1 is for later desktop testing, not required for a base install.
set -eu
usage() {
    echo 'usage: tests/vm/create.sh ISO NAME [DISK_SERIAL [DMI_PRODUCT]]' >&2
    echo 'NAME must start xc0-e2e-. Existing domains/disks are never overwritten.' >&2
}
[ "$#" -ge 2 ] && [ "$#" -le 4 ] || { usage; exit 2; }
iso=$(realpath "$1") name=$2 serial=${3:-XC0_TEST_DISK} product=${4:-}
case "$name" in xc0-e2e-*) : ;; *) usage; exit 2 ;; esac
case "$name$serial" in *[!a-zA-Z0-9_-]*) echo 'invalid name/serial' >&2; exit 2 ;; esac
case "$iso" in *.iso) : ;; *) echo 'not an ISO filename' >&2; exit 2 ;; esac
[ -f "$iso" ] && [ ! -b "$iso" ] || exit 2
v() { sudo -n virsh -c qemu:///system "$@"; }
if v dominfo "$name" >/dev/null 2>&1; then echo 'domain already exists' >&2; exit 2; fi
disk=/var/lib/libvirt/images/$name.qcow2
media=/var/lib/libvirt/images/$name.iso
[ ! -e "$disk" ] && [ ! -e "$media" ] || { echo 'storage already exists' >&2; exit 2; }
sudo -n install -m 0600 "$iso" "$media"
set --
if [ -n "$product" ]; then
    case "$product" in *,*) echo 'invalid DMI product' >&2; exit 2 ;; esac
    set -- --sysinfo "system.product=$product"
fi
if [ "${XC0_VM_3D:-0}" = 1 ]; then
    set -- "$@" --graphics egl-headless,gl.rendernode=/dev/dri/renderD128 \
        --video virtio,model.acceleration.accel3d=yes
else
    set -- "$@" --video vga
fi
sudo -n virt-install --connect qemu:///system --name "$name" --memory 3072 --vcpus 2 \
    --cpu host-passthrough --machine q35 --os-variant debiantesting \
    --boot loader=/usr/share/edk2/x64/OVMF_CODE.4m.fd,loader.readonly=yes,loader.type=pflash,loader.secure=no,nvram.template=/usr/share/edk2/x64/OVMF_VARS.4m.fd \
    --disk "path=$disk,size=32,format=qcow2,bus=virtio,serial=$serial,boot.order=2" \
    --disk "path=$media,device=cdrom,bus=sata,readonly=on,boot.order=1" \
    --network network=default,model=virtio \
    --graphics vnc,listen=127.0.0.1 \
    --noautoconsole "$@"
printf '\nWatch with: virt-manager --connect qemu:///system --show-domain-console %s\n' "$name"
v domblklist "$name"
