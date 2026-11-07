# Deployment gates and operator steps

## What the release does

The official checksum-pinned Debian 13 netinst installs the base with encrypted
LVM. The testing profile then upgrades that clean base to Debian testing. The
stable profile keeps trixie. **The installer stops at the Debian base.** It does
not clone a configuration repo, install Hyprland or run a desktop bootstrap.
A failed base upgrade blocks completion instead of pretending success.

After installation and first boot, clone this repository, check out the exact
reviewed commit recorded in `/etc/xc0-next-steps`, and run the configuration
scripts. Those scripts add targeted backports on the stable profile and install
Hyprland/configuration. They are independently testable and can change without
putting desktop settings or dependencies in the ISO.

The release path is deliberately different from the disposable `vm-test` path:

| | release | vm-test |
|---|---|---|
| Automatic menu timeout | no | yes |
| Target disk | exact serial and DMI, plus local typed erase confirmation | QEMU, `/dev/vda`, serial `XC0_TEST_DISK` only |
| Login/LUKS secrets in ISO | no: entered at the installer | private throwaway credentials only |
| SSH trust | optional explicit public-key file | optional explicit test public-key file |
| Sudo | normal authenticated sudo group | disposable test-only passwordless sudo |
| Completion | power off; remove installer before first disk boot | power off; switch VM boot order |

No test VM is allowed to attach `/dev/nvme*`, a real USB disk or any other host
block device. `tests/vm/create.sh` uses a new qcow2 image and a read-only ISO.

## Build a production artifact for loki

First commit and push the reviewed source, and wait for CI. The builder refuses
a dirty source tree unless an explicit immutable stage-2 ref is supplied.

```sh
sh iso/build.sh ~/debian-iso/stable/debian-13.7.0-amd64-netinst.iso testing \
  --hostname loki \
  --target-dmi 20W80011MX \
  --target-serial S3WSNE0K602971
```

The serial above was measured on loki's Samsung NVMe on 2026-11-07. Recheck with
`lsblk -o NAME,MODEL,SERIAL,SIZE,MOUNTPOINTS` immediately before deployment. A
different machine/disk needs its own targeted build, not a relaxed guard. On
thor, select the **OS** disk serial, never its separate data disk.

The build outputs an ISO, SHA256 file and manifest under ignored `build/`.
It preserves the upstream interactive/rescue entries and their original initrd.
Use an approved Ethernet link or enter Wi-Fi credentials locally if prompted.
Set a unique user password and LUKS passphrase locally; keep the latter in the
operator's password manager before leaving the installer.

## Copy to the existing Ventoy stick

```sh
lsblk -o NAME,SIZE,TRAN,LABEL,SERIAL,MOUNTPOINTS
sh iso/copy-to-ventoy.sh build/<release>.iso /dev/<Ventoy-data-partition>
```

The copy helper checks USB/removable identity and the `Ventoy` label. It never
formats a disk, refuses VM-test artifacts and active USB passthrough, saves the
previous Ventoy JSON, and performs a read-only verification pass. Never replace
the partition argument with the internal NVMe disk.

## Before physical installation

1. Confirm GitHub backups/handover are accessible from another machine.
2. Recheck the exact production artifact hash and target serial.
3. Boot UEFI from USB, select the xc0 entry and read its target identity.
4. The explicit `ERASE <serial>` prompt is the destructive approval boundary.
   Everything on that selected disk is lost; no data-preservation claim is made.
5. When the installer powers off, remove the USB, boot the internal disk and
   unlock LUKS. This is a base text/SSH login, not a desktop yet.
6. Read `/etc/xc0-next-steps`, clone the reviewed Git source and run the scripts.
   Select Hyprland at the display manager after that separate step completes.

If provisioning blocks, inspect `/target/var/log/xc0-install.log` from an
installer shell; in the installed system use `/var/log/xc0-install.log` and
`/var/lib/xc0/install-status`. Do not skip the failure and mark deployment done.

## Evidence, not blanket hardware claims

Base-ISO readiness requires fresh stable AND testing UEFI VM installs, encrypted
first boot, network/SSH, no desktop installed, and the release confirmation/
credential path plus negative disk checks. Record those separately from the
later configuration-script checks: non-root Hyprland desktop, portals/audio and
an idempotent script rerun/reboot. Do not label a base-only pass "desktop complete".

VM success is not proof of physical Intel/NVIDIA graphics, Wi-Fi firmware,
suspend, Yoga pen/touch or Secure Boot support. NVIDIA driver deployment remains
a separate reviewed hardware-specific step; do not install an arbitrary driver
or wipe thor's data disk as a side effect of this desktop bootstrap.
