# Debian + Wayland + Hyprland

Two-stage installation for the operator's Debian workstations:

1. A checksum-pinned official Debian netinst installs the base using preseed.
2. **After installation and first boot**, pull an exact Git commit from this
   public repository and run its configuration scripts. No desktop bootstrap
   runs inside the installer and no deploy key is baked into the ISO.

The desktop is a clean Debian configuration, not a claim that the complete Arch
rice, every cosmetic helper or every hardware-specific service has been ported.
The preserved Arch setup is [xc0-sh/arch-dotfiles](https://github.com/xc0-sh/arch-dotfiles).

## Profiles

| Target | Debian suite | Desktop source | Hardware acceptance |
|---|---|---|---|
| loki | testing (currently forky) | Debian testing Hyprland | Intel/Yoga/Wi-Fi checks required on hardware |
| baldr | 13/trixie | targeted trixie-backports Hyprland | laptop checks required |
| thor | 13/trixie | targeted trixie-backports Hyprland | separate NVIDIA/driver review required; preserve its data disk |
| Generic PCs and QEMU | stable or testing | matching Debian repositories | UEFI VM evidence is recorded separately |

Stable installs must not promote the entire backports suite or replace the
stable kernel/audio stack as an accidental consequence of installing Hyprland.

### Minimal installed host layouts and identities

The operator approved these base-only policies on 2026-11-07. `iso/hosts.json`
binds each suite/layout and the explicit normal identities: **xcloud 1000:1000**,
**marius 1001:1001** on both hosts. This deliberately preserves deployed IDs and
deviates from the older thor prose assigning marius UID 1000. Named groups and
numeric collisions are checked; d-i still collects marius's local password.
xcloud has a locked password, no embedded secret, and its own owned home.
Sudo is authenticated on both release and VM-test installs; no NOPASSWD shortcut.

| Host | OS disk (GPT, no disk swap) | Flat Btrfs subvolumes |
|---|---|---|
| thor | 1 GiB FAT32 ESP `/boot/efi`; 1 GiB ext4 `/boot`; LUKS2 → LVM → one Btrfs LV | `@`, `@home`, `@snapshots`, `@var_log`, `@var_tmp`, `@aios`, `@srv_aios`, `@xccode`; `noatime,compress=zstd:1` |
| loki | 1 GiB FAT32 ESP `/boot/efi`; 1 GiB ext4 `/boot`; LUKS2 → LVM → one Btrfs LV | `@`, `@home`, `@pkg` at `/var/cache/pacman/pkg`, `@log`, `@swap`; `noatime,compress=zstd:3` |

Both use the fixed mapper `xcloud_root_crypt`. Loki keeps its former package-cache
mount even though Debian does not use it. `@swap` gets directory attribute `+C`
before any contents are copied; no swapfile is created. Btrfs `nodatacow` mount
options would affect the entire filesystem, so they are **not** used to turn off
CoW on the other subvolumes. Thor's WDS200 data disk is never formatted, mounted
as target data, or added to fstab. Snapper, desktops and service deployment remain
post-install work.

## Release safety

- loki's release requires its exact target serial and DMI. thor's release uses an
  explicit attended `model-confirm` policy: exactly one non-USB, non-removable
  `WDS100T1X0E` OS disk within the approximately 1 TB capacity bound
  (1,750,000,000–2,100,000,000 512-byte sectors).
- Both ask for the local **marius** password, LUKS passphrase and exactly
  `ERASE <live serial>` before approving disk writes. This is the destructive
  gate, not a promise of two later write confirmations.
- The `WDS200T1X0E` data disk is excluded. In either targeting mode, with a
  protected disk present, `partman-lvm/device_remove_lvm` and
  `partman-md/device_remove_md` remain **false**, preventing automatic cross-disk
  metadata removal. An install may stop for separate operator review instead.
- `partman-auto/disk` and `grub-installer/bootdev` are explicitly set and rechecked
  against the selected OS disk, not disk enumeration order.
- Failed validation blocks synchronously; a separate partman guard rechecks identity.
- The original Debian interactive/rescue initrd remains untouched.
- Base install/upgrade failures cannot be turned into a successful install marker.
- The selected expert recipe and UUID/subvolume boot configuration are guarded;
  initramfs cryptroot and GRUB rootflags are verified before completion.
- The marker explicitly says `stage=base`; it does not claim desktop configuration.
- `/etc/xc0-next-steps` records the reviewed Git commit and post-install commands.
- `FIRST-RUN.sh` is the Thor post-install orchestrator in that exact reviewed
  checkout. It stages a user-selected signed xccode release, runs the existing
  desktop bootstrap's dry-run before apply, and refuses to proceed until the
  Snapper APT hook and manually prepared, signed NVIDIA 595 stack are ready.
  It does not install drivers, enroll MOK, toggle NVIDIA APT sources, or handle
  provider secrets. Install xccode before adding the NVIDIA repository; after
  manual driver/MOK preparation use `--no-xccode` for the desktop stage.
- An explicitly separate `vm-test` mode may embed throwaway secrets, but only a
  QEMU VM with its designated virtual disk can use it. Never publish those ISOs.

See [deployment instructions](docs/deployment.md) and [reviewed input pins](VERSIONS.md).
Do not boot an older prototype image as a production installer.

## Dual-host production build and Ventoy deployment

From a clean, reviewed source checkout, with the pinned source netinst available:

```sh
ref=$(git rev-parse HEAD)
ref12=$(printf '%.12s' "$ref")
sh iso/build.sh ~/debian-iso/stable/debian-13.7.0-amd64-netinst.iso testing \
  --hostname loki --target-dmi 20W80011MX --target-serial S3WSNE0K602971
sh iso/build.sh ~/debian-iso/stable/debian-13.7.0-amd64-netinst.iso stable \
  --hostname thor --target-model WDS100T1X0E --protected-model WDS200T1X0E
```

Optionally add `--stage2-ref <full-reviewed-40-character-commit>` to both builds
and use that commit's first 12 characters for `ref12`; a dirty source tree is
still refused. The outputs are `build/xc0-loki-testing-release-<ref12>.iso` and
`build/xc0-thor-stable-release-<ref12>.iso`, each with `.iso.manifest.json` and
`.iso.sha256` sidecars. Recheck loki's recorded hardware identity before use.
**thor's physical DMI and serials are unknown, and its hardware is not validated**;
model-confirm still requires inspecting the live disk and typing its live serial.
These commands are instructions, not evidence that deployment has already run.

Identify and unmount the existing Ventoy data partition and all sibling
partitions, then run (replace the partition placeholder only):

```sh
lsblk -o NAME,SIZE,TRAN,LABEL,SERIAL,MOUNTPOINTS
sh iso/copy-to-ventoy.sh \
  --loki "build/xc0-loki-testing-release-${ref12}.iso" \
  --thor "build/xc0-thor-stable-release-${ref12}.iso" \
  --usb-serial 25030837640037 /dev/USB_PARTITION
```

The helper requires that exact USB parent serial, removable USB transport,
`Ventoy` label, exFAT/FAT and no active VM use. It verifies both host-bound source
artifacts and their testing/stable profiles before writing, requires matching
reviewed `config_ref` values, stages both sets, retains the first Ventoy JSON backup
without overwriting it on later runs, and imports only the existing visual theme.
It never formats or installs Ventoy. After sync/unmount and parent-buffer flush,
it remounts read-only and compares all copies, checks both SHA256 files and
verifies both embedded host identities/profiles again. A failure is not deployable media.

The menu has no auto-boot timeout: select **loki testing/forky** or **thor Debian 13
stable/trixie**. The destination sets are `/xc0/xc0-loki-installer.iso` and
`/xc0/xc0-thor-installer.iso` with their manifest/checksum sidecars. Their preseed
is inside each ISO; no Ventoy host template is selected. Successful installation
powers off: remove the USB, boot the internal disk, then follow
`/etc/xc0-next-steps` for stage2. Only the Debian base is installed at that point.
Schema-3 manifests bind the hostname, suite, layout, all four numeric ID fields
and target policy to the actual initrd. Older schema-1/2 artifacts must not be
substituted. Host/suite mismatches are rejected, including by the media verifier's
`--expect-profile` check.

## Development and tests

```sh
make check
sh iso/build.sh --help
```

`make check` runs shell lint and deterministic installer/bootstrap regression tests.
It does not stand in for a fresh VM install or a real graphical session test.
The new layout/identity checks include file-backed metadata and failure fixtures;
they are not evidence that either new host layout has completed a real VM install.
`tests/vm/` creates file-backed libvirt VMs visible in Virtual Machine Manager and
contains post-login acceptance checks. No test exposes a writable physical host
disk; final USB boot validation uses that identified source read-only.

The source netinst is Debian 13.7.0 because the testing weekly image trial had a
kernel/module mismatch. The testing profile upgrades the clean base before stage 2.

## Repository layout

```text
iso/               netinst repack, preseed, exact-target safeguards, Ventoy copy
setup/             idempotent Debian desktop/bootstrap, package profiles
config/            clean desktop configuration
tests/iso/         installer safety and failure regression tests
tests/bootstrap/   bootstrap/config regression tests
tests/vm/          real libvirt install/session checks
docs/              deployment procedure and limitations
```

This repository is public. Never commit passwords, password hashes, API keys,
private SSH keys, private ISO builds or target credential files. CI scans the full
history with gitleaks as well as running lint/regression tests.

The accepted thor/baldr desktop change is tracked in the private xCloud build
record. A VM pass is not an assertion of NVIDIA, suspend, touch/pen or Secure Boot
compatibility on those machines.
