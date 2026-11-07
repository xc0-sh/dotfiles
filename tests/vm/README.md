# Real VM acceptance

`create.sh` makes a fresh file-backed, UEFI libvirt VM with standard VGA,
a localhost-only VNC console and the libvirt default network. The base install
does not need accelerated graphics. `XC0_VM_3D=1` selects virtio/EGL for separate
later desktop experiments, not the base-ISO readiness gate.
The helper never reuses/truncates an existing disk/domain or attaches a host block
device. Names must start `xc0-e2e-`. A separate production-media check may attach
the exact identified USB as a read-only source; the sole writable disk must still
be a fresh qcow2. Never expose the physical NVMe or a writable USB to the guest.

```sh
sh tests/vm/create.sh build/<vm-test>.iso xc0-e2e-testing
virt-manager --connect qemu:///system --show-domain-console xc0-e2e-testing
```

The host needs version-matched QEMU virtio VGA/PCI/GL modules, EGL headless
support, libvirt, OVMF and KVM. Keep one install running at a time on loki.
Use a 3 GiB guest and watch the actual console; do not count a boot menu as an
installation or desktop success.

## Gates

1. A negative fixture using the wrong virtual disk serial must block before
   partitioning; compare virtual disk contents with a blank image afterwards.
2. A fresh private test build for **stable** and **testing** must install the
   BASE and power off without failures. Never attach the physical NVMe.
3. Switch boot order to the guest disk, start it and unlock its test LUKS key.
   `send-text.py --file` types private test input without logging the value.
4. Run `acceptance.sh` without `--desktop`: it must prove the base and show the
    next-step Git commands, with no Hyprland running. Base acceptance ends here.
   Build the test ISO with `--hostname loki` or `--hostname thor`, then use
   `acceptance.sh testing marius --host loki` or
   `acceptance.sh stable marius --host thor` to assert the exact normal account
   IDs, locked xcloud password, home owners, UUID/subvolume layout, LUKS2 mapper
   and correctly sized boot partitions. These are required new-layout checks;
   old generic VM evidence does not prove them. VM sudo now requires the
   disposable marius password too; no NOPASSWD rule is installed.
5. Separately test configuration scripts AFTER first boot. Clone/check out the
   recorded Git commit, run the scripts, then log into Hyprland. Any temporary
   autologin used to exercise a disposable VM must not be a production default.
6. Run `acceptance.sh PROFILE USER --desktop` inside the VM as root. Save output and console
   screenshot, installed versions and `/var/lib/xc0/install-status`.
7. Rerun the same pinned bootstrap, confirm preserved config and stable profile
   choices, reboot, unlock and repeat essential session checks.
8. Exercise the exact **release-mode** ISO too: select its menu entry, enter
   disposable credentials locally, and type the exact erase confirmation. Use
   a file-backed virtual disk with the release's expected serial and virtual DMI;
   this validates prompts, not the real laptop's hardware.

Private VM credentials belong outside this repository, under a mode-0700
operator-owned directory. Public key files may be explicitly supplied to the
builder. Never publish the private test ISO or record a test password in a log.

NVIDIA, real Wi-Fi, sleep/resume, Yoga touch/pen, audio hardware and Secure Boot
remain physical acceptance checks even when every VM gate passes.

The new layout implementation also needs fresh multi-disk UEFI rehearsals with
an existing ESP and LVM/MD/data sentinels on the non-target file-backed disk.
Check strict nested-mount restoration, loki's signed GRUB diversion, and a
testing kernel upgrade followed by encrypted reboot. Model-confirm deliberately
rejects QEMU: its exact production image must refuse there, not be treated as
having completed a physical thor installation. No real VM layout pass is claimed
by the file-backed/static unit tests.
