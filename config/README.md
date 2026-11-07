# Minimal Debian desktop configuration

`desktop/` is the maintained core, **not** a copy of the Arch desktop. Bootstrap
copies the files listed in `manifest.txt` to root-owned
`/usr/local/share/xc0-desktop`, then links each leaf into the selected user's
`.config`. The Git checkout can stay root-owned and need not live in the user's
home. GNU stow is not needed.

Conflicting files, directories, and symlinks are moved alongside the destination
as `NAME.xc0-backup[.N]`, never deleted. Unrelated files and directories are left
alone. The helper refuses symlinked parent directories; all home operations run
as the login user, not root. Repeating bootstrap leaves correct links alone.

The main Hyprland file is native Lua for 0.55.2 and newer. Displays use preferred
modes at scale 1; blur, shadows and animations are disabled to keep the core small
and suitable for a 2 GB VM. Add optional per-user overrides to
`~/.config/hypr/local.lua`; bootstrap does not manage that file. System keyboard
layout is inherited; no Norwegian/US layout is hardcoded here.

UWSM starts the session, propagates its environment and manages native graphical
units and XDG autostart lifetimes. Debian enables its packaged Waybar, Mako and
Hypridle user units; PipeWire supplies its own sockets/services. Those are not
started a second time through custom autostart files. Applets and polkit use XDG
autostart. Lua calls `uwsm finalize HYPRLAND_INSTANCE_SIGNATURE` once. There are
no `hl.env`, portal restarts, session-wide driver overrides or duplicate manual
applet launches. The GTK portal handles file selection, Hyprland handles screen
sharing. The polkit agent path is Debian's `/usr/libexec/...`, not Arch's path.

## First login

At SDDM's graphical login, enter the user's existing credentials. The offered
`Hyprland (xc0, UWSM)` desktop is native Wayland; only the login greeter uses
Xorg. This is password-authenticated, not autologin. `Ctrl+Alt+F2` is a rescue TTY.

| Shortcut | Action |
|---|---|
| Super+Enter | Foot terminal |
| Super+Space | Fuzzel app launcher |
| Super+E / Super+B | Files / Firefox |
| Super+N / Super+Ctrl+B | Network / Bluetooth settings |
| Super+Q / Super+F / Super+T | Close / fullscreen / floating |
| Super+arrows | Focus window |
| Super+1..9 / Super+Shift+1..9 | Focus workspace / move window |
| Super+left/right mouse button | Move / resize window |
| Super+L | Lock using the real user's PAM password |
| Super+Shift+E | Log out (immediate) |
| Super+Print | Copy a selected screenshot to clipboard |

The bar exposes network/audio controls, tray, clock, lock and logout without Nerd
Fonts. Hypridle locks after five minutes, blanks displays after 5.5 minutes, and
inhibits suspend until the lock surface is ready. Suspend itself remains logind's
responsibility: no forced VM suspend timer is configured.

## Deliberately deferred

Quickshell, awww, satty, hyprshot, matugen, Nerd Fonts, dynamic wallpaper/theme
generation, docks, OCR and developer toolchains are not installed or referenced.
No ported Arch scripts, fetched binaries, secrets or default passwords are used.
