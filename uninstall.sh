#!/usr/bin/env bash
set -euo pipefail

local_prefix="${HOME}/.local"
app_root="${local_prefix}/lib/focus-tomato"
launcher="${local_prefix}/bin/focus-tomato"
applications_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
autostart_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/autostart"
extension_uuid="focus-tomato-state@gxy.local"
extension_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/gnome-shell/extensions/${extension_uuid}"

if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions disable "${extension_uuid}" >/dev/null 2>&1 || true
fi
if command -v python3 >/dev/null 2>&1; then
    python3 -c 'from gi.repository import Gio; import sys; settings = Gio.Settings.new("org.gnome.shell"); uuid = sys.argv[1]; settings.set_strv("enabled-extensions", [value for value in settings.get_strv("enabled-extensions") if value != uuid])' "${extension_uuid}" || true
fi

rm -f -- \
    "${launcher}" \
    "${applications_dir}/focus-tomato.desktop" \
    "${autostart_dir}/focus-tomato.desktop"
rm -rf -- "${app_root}"
rm -rf -- "${extension_dir}"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${applications_dir}" >/dev/null 2>&1 || true
fi

echo "Focus Tomato 已卸载。配置和专注记录已保留。"
echo "如需清除数据，请手动删除 ~/.config/focus-tomato 和 ~/.local/share/focus-tomato。"
