#!/bin/sh
# SDDM's PAM session supplies logind, the user bus and XDG_RUNTIME_DIR.
# UWSM owns environment propagation, XDG autostart and session teardown.
set -eu
if [ "$(id -u)" -eq 0 ] || [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    echo "xc0-hyprland-session: use a non-root PAM/logind login session" >&2
    exit 1
fi
exec /usr/bin/uwsm start -D Hyprland -- /usr/bin/Hyprland --config "$HOME/.config/hypr/hyprland.lua"
