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

## Release safety

- A release ISO requires an exact target disk serial and DMI identity.
- It asks locally for credentials and `ERASE <serial>` before approving disk writes.
- Failed validation blocks synchronously; a separate partman guard rechecks identity.
- The original Debian interactive/rescue initrd remains untouched.
- Base install/upgrade failures cannot be turned into a successful install marker.
- The marker explicitly says `stage=base`; it does not claim desktop configuration.
- `/etc/xc0-next-steps` records the reviewed Git commit and post-install commands.
- An explicitly separate `vm-test` mode may embed throwaway secrets, but only a
  QEMU VM with its designated virtual disk can use it. Never publish those ISOs.

See [deployment instructions](docs/deployment.md) and [reviewed input pins](VERSIONS.md).
Do not boot an older prototype image as a production installer.

## Development and tests

```sh
make check
sh iso/build.sh --help
```

`make check` runs shell lint and deterministic installer/bootstrap regression tests.
It does not stand in for a fresh VM install or a real graphical session test.
`tests/vm/` creates file-backed libvirt VMs visible in Virtual Machine Manager and
contains post-login acceptance checks. No VM test attaches a physical host disk.

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
