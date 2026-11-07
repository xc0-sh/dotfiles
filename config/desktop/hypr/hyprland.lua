-- Small native Lua configuration for Hyprland >= 0.55.2, not an Arch rice port.
-- No machine-specific outputs, GPUs, input devices or absolute home paths.
hl.monitor({ output = "", mode = "preferred", position = "auto", scale = 1 })

hl.config({
    general = { gaps_in = 4, gaps_out = 8, border_size = 2, layout = "dwindle" },
    decoration = { rounding = 6, blur = { enabled = false }, shadow = { enabled = false } },
    animations = { enabled = false },
    dwindle = { preserve_split = true },
    input = {
        kb_layout = os.getenv("XKB_DEFAULT_LAYOUT") or "us",
        kb_variant = os.getenv("XKB_DEFAULT_VARIANT") or "",
        kb_model = os.getenv("XKB_DEFAULT_MODEL") or "pc105",
        kb_options = os.getenv("XKB_DEFAULT_OPTIONS") or "",
        follow_mouse = 1,
        touchpad = { natural_scroll = true, tap_to_click = true },
    },
    misc = { disable_hyprland_logo = true, disable_splash_rendering = true },
})

-- The installed XKB system layout is used; set input.kb_layout in local.lua
-- if a per-user override is needed. UWSM alone owns the activation environment,
-- portals and XDG autostart. Do not add portal restarts or hl.env here.
hl.on("hyprland.start", function()
    hl.exec_cmd("uwsm finalize HYPRLAND_INSTANCE_SIGNATURE")
end)

local function app(keys, command, description)
    hl.bind(keys, hl.dsp.exec_cmd("uwsm app -- " .. command), { description = description })
end
app("SUPER + RETURN", "foot", "Terminal")
app("SUPER + SPACE", "fuzzel", "Application launcher")
app("SUPER + E", "thunar", "Files")
app("SUPER + B", "firefox-esr", "Browser")
app("SUPER + N", "nm-connection-editor", "Network settings")
app("SUPER + CTRL + B", "blueman-manager", "Bluetooth settings")
hl.bind("SUPER + L", hl.dsp.exec_cmd("pidof hyprlock || hyprlock"), { description = "Lock screen" })
hl.bind("SUPER + SHIFT + E", hl.dsp.exec_cmd("uwsm stop"), { description = "Log out" })
hl.bind("SUPER + Q", hl.dsp.window.close(), { description = "Close window" })
hl.bind("SUPER + F", hl.dsp.window.fullscreen({ mode = "fullscreen", action = "toggle" }))
hl.bind("SUPER + T", hl.dsp.window.float({ action = "toggle" }))
hl.bind("SUPER + CTRL + R", hl.dsp.exec_cmd("hyprctl reload"), { description = "Reload config" })
for _, direction in ipairs({ "left", "right", "up", "down" }) do
    hl.bind("SUPER + " .. direction, hl.dsp.focus({ direction = direction }))
end
for workspace = 1, 9 do
    hl.bind("SUPER + " .. workspace, hl.dsp.focus({ workspace = workspace }))
    hl.bind("SUPER + SHIFT + " .. workspace, hl.dsp.window.move({ workspace = workspace }))
end
hl.bind("SUPER + mouse:272", hl.dsp.window.drag(), { mouse = true })
hl.bind("SUPER + mouse:273", hl.dsp.window.resize(), { mouse = true })
hl.bind("XF86AudioRaiseVolume", hl.dsp.exec_cmd("wpctl set-volume -l 1 @DEFAULT_AUDIO_SINK@ 5%+"), { repeating = true })
hl.bind("XF86AudioLowerVolume", hl.dsp.exec_cmd("wpctl set-volume @DEFAULT_AUDIO_SINK@ 5%-"), { repeating = true })
hl.bind("XF86AudioMute", hl.dsp.exec_cmd("wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"))
hl.bind("XF86AudioMicMute", hl.dsp.exec_cmd("wpctl set-mute @DEFAULT_AUDIO_SOURCE@ toggle"))
hl.bind("XF86MonBrightnessUp", hl.dsp.exec_cmd("brightnessctl set +5%"), { repeating = true })
hl.bind("XF86MonBrightnessDown", hl.dsp.exec_cmd("brightnessctl set 5%-"), { repeating = true })
hl.bind("SUPER + PRINT", hl.dsp.exec_cmd("sh -c 'area=$(slurp) || exit; grim -g \"$area\" - | wl-copy --type image/png'"), { description = "Copy region screenshot" })

-- An existing user's customization is never replaced by bootstrap.
local local_config = os.getenv("HOME") .. "/.config/hypr/local.lua"
local file = io.open(local_config, "r")
if file then
    file:close()
    dofile(local_config)
end
