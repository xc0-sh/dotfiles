# xc0-sh/dotfiles

Debian + Wayland + Hyprland, installed in two stages:

1. **ISO** — the official Debian netinst image, repacked with a preseed answer file. It
   installs a minimal base system and nothing else.
2. **Git** — the installer's last step clones this repository and runs `setup/bootstrap.sh`,
   which installs the desktop and links the configuration. Everything after the base comes
   from here, so changing the desktop never means rebuilding the ISO.

This repository is **public** so a fresh machine can clone it with no credentials. It must
never contain secrets: no keys, tokens, passwords or password hashes. CI runs gitleaks on
every push.

## Profiles

| Machine | Debian | Hyprland | Notes |
|---|---|---|---|
| loki (ThinkPad X13 Yoga Gen 2) | testing (forky) | 0.57.0 from testing | Intel graphics, touch and pen |
| thor | 13 (trixie) + backports | 0.55.2 from trixie-backports | NVIDIA; driver stack per thor Host Spec §7 |
| baldr | 13 (trixie) + backports | 0.55.2 from trixie-backports | |
| other PCs, QEMU | 13 (trixie) + backports | 0.55.2 from trixie-backports | |

thor and baldr are specified as X11 + i3 in the xCloud documents today. Moving them to
Wayland + Hyprland is an accepted change that lands in those specifications once this
installer works (`xcloud-state`, open-loki-agent decisions, 2026-11-07).

## Layout

```
iso/          netinst repack: build script, preseed files, late command (stage 1)
setup/        bootstrap.sh and package lists per profile (stage 2)
config/       configuration packages, linked into $HOME with GNU stow
tests/        QEMU boot tests (UEFI via OVMF)
```

## Known facts (checked 2026-11-07)

- Debian 13 **main** has no Hyprland; trixie-backports has 0.55.2, testing and sid have 0.57.0.
  Upstream is 0.57.1.
- The operator's current Lua config (`hyprland.lua`, from `xc0-sh/arch-dotfiles`) passes
  `Hyprland --verify-config` on both 0.55.2 and 0.57.0. That is a parse check, not a live
  session.
- No Debian package exists for: `awww` (wallpaper), `satty`, `hyprshot`, `matugen`, Nerd
  Fonts, `prettier`. These come from upstream releases in stage 2, pinned with checksums.

## Related repositories

- [`xc0-sh/arch-dotfiles`](https://github.com/xc0-sh/arch-dotfiles) — the Arch Linux
  Hyprland dotfiles this replaces on Debian (copy of `xc0sh/dotfiles`).
- `xc0-sh/fenrir` — the xCloud live ISO (live-build, X11/i3). Not used here.
