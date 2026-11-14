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
| Target disk | loki: exact serial and DMI; thor: attended model-confirm; both require typed live-serial erase approval | QEMU, `/dev/vda`, serial `XC0_TEST_DISK` only |
| Login/LUKS secrets in ISO | no: entered at the installer | private throwaway credentials only |
| SSH trust | optional explicit public-key file | optional explicit test public-key file |
| Sudo | normal authenticated sudo group | authenticated sudo with disposable test credentials |
| Completion | power off; remove installer before first disk boot | power off; switch VM boot order |

No test VM may attach the host NVMe or any writable physical block device.
`tests/vm/create.sh` uses only a new qcow2 image and a read-only ISO. A separate
final-media test can boot the identified production USB as a **read-only emulated
USB disk**, with a new disposable qcow2 as its sole writable install target.
Verify this isolation in libvirt XML before approving any VM erase prompt.

### Approved base policy (2026-11-07)

`iso/hosts.json` binds host, suite, storage layout and identities. Both hosts use
**xcloud 1000:1000** and **marius 1001:1001**, preserving deployed IDs; this is an
operator-approved deviation from earlier thor prose assigning marius UID 1000.
d-i creates marius and its private group at UID/GID 1001 and collects the password
locally. The target helper checks names, numeric collisions and ownership before
creating xcloud with its explicit UID/GID; xcloud's password is locked. Neither
identity is silently renumbered, and authenticated sudo is used.

The OS disk is GPT with no disk-backed swap:

* **thor/stable/trixie:** 1 GiB FAT32 ESP at `/boot/efi`, 1 GiB ext4 `/boot`,
  LUKS2 → LVM → one Btrfs root LV. Flat mounts are `@` `/`, `@home` `/home`,
  `@snapshots` `/.snapshots`, `@var_log` `/var/log`, `@var_tmp` `/var/tmp`,
  `@aios` `/var/lib/aios`, `@srv_aios` `/srv/aios`, `@xccode`
  `/var/lib/xcloud/xccode`; options `noatime,compress=zstd:1`.
* **loki/testing/forky:** 1 GiB FAT32 ESP at `/boot/efi`, 1 GiB ext4 `/boot`,
  LUKS2 → LVM → one Btrfs root LV. Flat mounts are `@` `/`, `@home` `/home`, `@pkg`
  `/var/cache/pacman/pkg`, `@log` `/var/log`, and `@swap` `/swap`; options
  `noatime,compress=zstd:3`. The empty `@swap` directory gets `+C` before copying;
  no swapfile or pacman setup is created.

Both use mapper `xcloud_root_crypt`. The partition recipe requests 1075 decimal MB
for aligned 1 GiB boot partitions; a pre-kernel check requires at least 1,073,741,824
bytes and permits up to 4 MiB alignment overhead on the authorized OS disk. A VM rehearsal measured that 1074 MB produced
only 1,072,693,248 bytes (1023 MiB), so that request is rejected. An earlier
Loki rehearsal showed dpkg cannot create kernel backup hard links on FAT; Loki
therefore uses ext4 `/boot`, while the ESP remains at `/boot/efi`.
The Btrfs reshape preserves installed metadata, parks nested mounts, renames the
live crypt mapper along with crypttab, and verifies initramfs and GRUB before
publishing completion. Thor's WDS200 data disk is never formatted, mounted as
target data, or added to fstab. Snapper and desktop/service setup remain deferred.

## Build both production artifacts from source

First commit and push the reviewed source, and wait for CI. The builder refuses
a dirty source tree, including when an explicit stage-2 ref is supplied. Run both
commands from the same clean reviewed checkout. These are operator instructions,
not a claim that builds, USB deployment or physical installs have been executed.

```sh
ref=$(git rev-parse HEAD)
ref12=$(printf '%.12s' "$ref")

sh iso/build.sh ~/debian-iso/stable/debian-13.7.0-amd64-netinst.iso testing \
  --hostname loki \
  --target-dmi 20W80011MX \
  --target-serial S3WSNE0K602971

sh iso/build.sh ~/debian-iso/stable/debian-13.7.0-amd64-netinst.iso stable \
  --hostname thor \
  --target-model WDS100T1X0E \
  --protected-model WDS200T1X0E
```

The post-install source defaults to clean checkout `HEAD`. Optionally append
`--stage2-ref <full-reviewed-40-character-commit>` to **both** build commands; that
commit must exist locally. In that case set `ref`/`ref12` to that chosen commit
when locating the output files. An explicit ref does not permit unreviewed,
dirty installer code.

The loki serial above was measured on its Samsung NVMe on 2026-11-07. Recheck with
`lsblk -o NAME,MODEL,SERIAL,SIZE,MOUNTPOINTS` immediately before deployment. A
different loki machine/disk needs its own targeted build, not a relaxed guard.

**thor's physical DMI identity and disk serials are unknown; its hardware is not
validated.** Its release therefore uses the explicit, attended `model-confirm`
policy, not invented serial/DMI values. It accepts exactly one non-USB,
non-removable OS disk whose trimmed model contains `WDS100T1X0E` and whose capacity
is in the approximately **1 TB** bound: 1,750,000,000 through 2,100,000,000
512-byte sectors (896,000,000,000 through 1,075,200,000,000 bytes). Missing serial,
no match or ambiguous candidates blocks installation. Model-confirm rejects QEMU;
it is not a generic unattended or VM bypass.

The guard displays the selected disk's **live** model/serial and requires exactly
`ERASE <live serial>`, not `ERASE WDS100T1X0E` and not a guessed serial. Inspect the
physical inventory before approving it. A disk containing `WDS200T1X0E` in its
model is protected and excluded from selection. In either targeting mode,
when a protected disk is present, `partman-lvm/device_remove_lvm` and
`partman-md/device_remove_md` are set and checked as **false**: global LVM/MD
removal is not auto-authorized across the OS and data disks. Existing metadata
may therefore stop the install for separate operator review rather than silently
clearing it. The guard explicitly sets and rechecks both `partman-auto/disk` and
`grub-installer/bootdev` to the selected OS disk; GRUB does not guess the first disk.

The outputs under ignored `build/` are distinct, host-bound release sets:

```text
xc0-loki-testing-release-<ref12>.iso
xc0-thor-stable-release-<ref12>.iso
```

Each ISO has matching `.iso.manifest.json` and `.iso.sha256` sidecars. Schema-3
manifest, embedded metadata and initrd configuration bind hostname, suite, layout,
all four numeric identity fields, config ref and target policy. Schema-1/2 images
are not substitutes. The builder preserves the upstream interactive/rescue
entries and their original initrd.
The builder preserves the upstream interactive/rescue entries and their original initrd.
Use an approved Ethernet link or enter Wi-Fi credentials locally if prompted.
Set a unique local **marius** password and LUKS passphrase; keep the latter in the
operator's password manager before leaving the installer.

## Copy to the existing Ventoy stick

```sh
lsblk -o NAME,SIZE,TRAN,LABEL,SERIAL,MOUNTPOINTS
sh iso/copy-to-ventoy.sh \
  --loki "build/xc0-loki-testing-release-${ref12}.iso" \
  --thor "build/xc0-thor-stable-release-${ref12}.iso" \
  --usb-serial 25030837640037 /dev/USB_PARTITION
```

Replace only `/dev/USB_PARTITION` with the identified **partition**,
not the whole USB disk or an internal NVMe. The helper accepts exactly this
dual-host interface. The parent must be a removable USB disk with the exact serial
`25030837640037`; all its partitions must be unmounted. The target must have label
`Ventoy` and filesystem exFAT or FAT. The existing libvirt/passthrough safety check
must succeed. No disk is formatted and the Ventoy bootloader is not installed or
updated by this helper (its installed version is not established by `VERSIONS.md`).

Both source artifacts must pass `verify-artifact.py` with `--expect-hostname loki
--expect-profile testing` or `--expect-hostname thor --expect-profile stable`.
Their full `config_ref` values must match, so one stick represents one reviewed
checkout. All these checks precede any writable mount. The complete sets on the
stick are renamed to:

```text
/xc0/xc0-loki-installer.iso[.manifest.json|.sha256]
/xc0/xc0-thor-installer.iso[.manifest.json|.sha256]
```

Each checksum names its local ISO basename, not an absolute build-host path.
The helper stages `.new` files and compares **both** sets before replacing any
active set, then publishes the menu last. Individual renames are atomic; this is
not a power-loss-safe multi-file filesystem transaction. Any failure is fatal;
do not boot partially updated media, and rerun the full verified copy after
resolving it. Unexpected symlinks/non-regular destinations are refused.

The first existing `/ventoy/ventoy.json` is saved as `ventoy.json.before-xc0` only
when that backup does not already exist; repeat deployment never overwrites it.
Only the visual assets from
`/opt/xcloud/claude/xcloud/projects/ventoy/stick-tree/ventoy/theme/xcloud/` are imported (the source
directory, ancestors and assets must not be symlinks). The reviewed `ventoy.json`
and F6 `ventoy_grub.cfg` replace the old menu; no old install scripts, templates
or instructions are copied. The ISOs contain their own preseed, so Ventoy has no
`auto_install` entries. The list menu searches `/xc0` only, has no automatic boot
timeout/default image, and labels loki testing/forky and thor stable/trixie clearly.

After sync and unmount, the helper flushes the parent disk's block buffers,
remounts read-only, compares every deployed file, runs both ISO checksums and the
retained Ventoy-config-backup checksum, and repeats both host/profile artifact
verifications. It must successfully unmount again before reporting completion.
Cleanup checks the actual mount state even if a signal arrives during a mount or
unmount command. Query and unmount errors are fatal. If detachment cannot be
confirmed, it retains the mount directory for operator recovery; it never removes
a directory still reported as mounted.

## Before physical installation

1. Confirm GitHub backups/handover are accessible from another machine.
2. Recheck both production artifacts and the physical OS/data disk inventory.
   thor's actual DMI/serials remain unmeasured until collected on that machine.
3. Boot UEFI from USB, select the correct host and Boot normal mode, then choose
   the xc0 guarded base installer entry and read the live target identity.
   F6 provides a short helper/power/firmware menu, not another installer.
4. Enter the local marius password and LUKS passphrase when prompted. The explicit
   `ERASE <live serial>` prompt is the destructive approval boundary, not a promise
   of two further write confirmations.
   Everything on that selected disk is lost; no data-preservation claim is made.
5. When the installer powers off, remove the USB, boot the internal disk and
   unlock LUKS. This is a base text/SSH login, not a desktop yet.
6. Read `/etc/xc0-next-steps`, clone the exact reviewed Git commit and run
   the host-specific post-install commands as `marius`:
   - **loki:** `sudo bash setup/bootstrap.sh --profile loki --user marius --dry-run`,
     then the same command without `--dry-run` after reviewing its output.
   - **thor:** `./FIRST-RUN.sh --check`. Resolve its Snapper, signed xccode
     release/key, and manual NVIDIA/MOK prerequisites before `--apply`. The script
     runs the existing desktop bootstrap dry-run first and never enrolls MOK or
     installs NVIDIA packages.
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
Mocked model/serial policy tests and isolated VM installs do not validate thor's
physical model-confirm path or prove that its separate data disk was preserved.
