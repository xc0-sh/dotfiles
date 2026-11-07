# Standalone post-install desktop bootstrap

The ISO installs **only a simple Debian base**. It does not invoke this script.
After the base has been installed **and booted**, the operator runs this separate
Git-driven entrypoint from a reviewed, pinned checkout. The selected login user
and its operator-supplied password must already exist. No desktop configuration
is baked into the ISO or applied from its late command.

```sh
sudo sh /opt/xc0/dotfiles/setup/bootstrap.sh --profile testing --user marius --dry-run
sudo sh /opt/xc0/dotfiles/setup/bootstrap.sh --profile testing --user marius
```

Standalone interface:

```text
bootstrap.sh --profile {testing|stable|loki|thor|baldr|generic|qemu} --user USER [--dry-run] [--nvidia-prepared]
bootstrap.sh --help
```

- Both `--profile` and `--user` are required. The user must already exist, have a
  normal login UID and a real, absolute home directory. Root is not a login user.
- `loki` requires Debian testing (`forky`). `thor`, `baldr`, `generic`, and `qemu`
  require Debian 13 (`trixie`); their Hyprland stack uses `trixie-backports`.
  Bootstrap does **not** upgrade between Debian suites or configure NVIDIA drivers.
- Manual invocation through `sh` is supported:
  `sudo sh /opt/xc0/dotfiles/setup/bootstrap.sh --profile "$PROFILE" --user marius`.
  `testing` selects forky (laptop extras only on hostname `loki`); `stable` selects
  trixie and derives `thor`/`baldr` from the hostname, otherwise `generic`.
  Known host/suite mismatches are refused. Both aliases still validate os-release.
- `thor` additionally requires `--nvidia-prepared` and an already-installed Debian
  `nvidia-driver` package. This acknowledges separate driver/kernel preparation;
  it is not a claim that the physical machine or GPU has been validated.
- `--dry-run` validates the host and prints the proposed operations without
  changing packages, files, services, users, or logs. Run it as root, too.
- Success is exit 0. Invalid input, unsupported hosts, package errors, or failed
  configuration return nonzero; the caller must preserve that failure and its log.
- Run directly, with Bash, or with `sh`; a POSIX-compatible initial guard
  re-execs Bash before the implementation's Bash-only syntax is read.

The rest of this document describes the implementation and validation boundaries.

## Package policy and session

Only signed Debian apt repositories are used. Before installation, apt's loaded
origin/codename metadata must match this profile's Debian base, updates, security,
and (for trixie only) backports. Mixed sid/testing/stable and third-party origins
are rejected. Base installation owns the base/security sources; this script does not replace
them or run a distribution/full upgrade. An apt update warning/failure is fatal;
installation uses `--no-install-recommends --no-remove` and never suppresses an
apt/dpkg failure.

On trixie, only `hyprland`, `hypridle`, `hyprlock`,
`xdg-desktop-portal-hyprland`, and `uwsm` are explicitly selected as
`PACKAGE/trixie-backports`. Their persistent preference is 990. The required
`libxkbcommon0` / matching X11 helper and `libxkbregistry0` ABI dependencies are
also preferred at 990:
Hyprland 0.55.2 needs >=1.12.3 while trixie supplies 1.7. All other backports stay
at 100, not 500. This does not backport PipeWire, replace the kernel, add a foreign
architecture, or change GPU drivers. On loki the normal forky repository is used;
only that laptop profile installs `power-profiles-daemon`.

`packages/common.txt` is a deliberately reduced core: Foot, Fuzzel, Waybar, Mako,
Thunar, Firefox ESR, Xwayland, Qt Wayland support, NetworkManager and its applet,
Bluetooth/Blueman, PipeWire, polkit, GTK/Hyprland portals, fonts and clipboard /
screenshot tools. No Rust/compiler stack, Quickshell or downloaded rice binaries.
`packages/upstream.txt` lists **deferred**, not installed, cosmetic extras.

SDDM's packaged Maui graphical greeter authenticates the real user through PAM.
There is no default password or autologin. Only its greeter uses Xorg; the offered
desktop is the native Wayland `Hyprland (xc0, UWSM)` session. The lightweight Maui
theme avoids pulling the Debian Breeze theme's Plasma dependency tree. Explicit
`xserver-xorg-core` and `xserver-xorg-input-libinput` packages provide the greeter
backend even with recommends disabled. Its root-owned launcher
starts UWSM and the user's explicit Lua configuration. UWSM owns the session
environment, portal activation, native graphical-session units and XDG autostart
lifecycle; bootstrap never runs
`systemctl --user` as root or connects to a user's existing bus. A rescue login
remains on Ctrl+Alt+F2. See [the configuration guide](../config/README.md).

The exact session filename is `xc0-hyprland.desktop`, installed in
`/usr/local/share/xc0-desktop/wayland-sessions/`. SDDM's configured Wayland session
directory contains that UWSM launcher, not an unmanaged upstream launcher. A
**temporary VM-only** acceptance override may use `Session=xc0-hyprland.desktop`;
bootstrap itself never writes an `[Autologin]` section or a default password.

## Network handoff (next reboot, not during script execution)

Stock d-i DHCP Ethernet and IPv6-auto interfaces, and ordinary `wpa-ssid` /
`wpa-psk` Wi-Fi stanzas, are retained verbatim in `/etc/network/interfaces`.
NetworkManager's Debian ifupdown plugin is enabled with `managed=true` so it
imports those connections on the next boot. Existing Wi-Fi credentials are
neither printed nor copied into this repository. Imported connections are
read-only in NM; new/edited profiles belong in its normal keyfile store.

Bootstrap masks **both** `networking.service` and udev's `ifup@.service` using
offline `systemctl --root=/`, then enables NetworkManager for the next boot.
It does not call ifdown, restart networking, rewrite resolv.conf, or stop the
current base-system connection. During apt installation, a temporary `policy-rc.d`
returns 101 to prevent maintainer scripts from starting services; any previous
site policy (even a symlink) is restored on success or failure.

Static IPs, bridges, external includes, hook commands, enterprise Wi-Fi and
nonempty `/etc/network/interfaces.d` are deliberately refused before any package
or file change. Prepare tested NM profiles and leave only loopback in interfaces
before retrying such hosts. The migration is not an unattended replacement for
an existing server's custom networking. Validate Wi-Fi/DNS after the first reboot;
the code/unit/container checks do not establish real network reconnection.

## Preservation, reruns and logs

- Run before the first graphical login. Bootstrap enables next-boot services but
  does not restart an existing session or reboot the machine. Existing alternative
  display managers are refused, not silently replaced.
- System files are atomically replaced only when different, with previous data
  retained under root-only `/var/backups/xc0-bootstrap-XXXXXXXX/`.
- Managed configuration is staged in root-owned `/usr/local/share/xc0-desktop`,
  not linked to an editable Git working tree. The home-link helper runs as the
  selected user. Conflicting leaves are moved to `NAME.xc0-backup[.N]`; unrelated
  data is preserved. Correct links are left unchanged on subsequent runs.
- A real run takes an exclusive lock and writes a root-readable log under
  `/var/log/xc0-bootstrap/`. Errors include the stage and exit status. A previous
  `last-success` is historical, not proof that a later failed run succeeded.
- Exact installed versions (including dependencies) are recorded in
  `/var/lib/xc0-bootstrap/packages.tsv`; selected apt policies and successful
  profile/user metadata are adjacent. Apt repositories intentionally track signed
  security/maintenance updates: the Git pin does **not** pin all Debian packages
  forever. No custom release binary is downloaded.

Bootstrap rejects non-Debian systems, including the Arch build workstation.
Do not try to run it there. `--help`, lint and deterministic fixture tests are safe
without invoking the host bootstrap. See [tests/bootstrap](../tests/bootstrap/README.md).

## Validation limits

The optional container test installs actual Debian packages twice, verifies Lua
parsing, checks policy/config preservation and inspects offline enablement in a
2 GB memory limit. It is not a graphical boot test, a measured 2 GB desktop RAM
budget, or proof of lock/suspend, audio, Wi-Fi, Bluetooth, portals, touch/pen,
multi-monitor, or NVIDIA correctness. Those require a booted VM / actual hardware.
Thor's proprietary driver and kernel compatibility, Steam/i386 compatibility,
and a physical Wayland session remain separate prerequisites and validation work.
