#!/usr/bin/env bash
set -euo pipefail

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
local_prefix="${HOME}/.local"
app_root="${local_prefix}/lib/focus-tomato"
launcher="${local_prefix}/bin/focus-tomato"
applications_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
autostart_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/autostart"
desktop_file="${applications_dir}/focus-tomato.desktop"
autostart_file="${autostart_dir}/focus-tomato.desktop"
icon_file="${app_root}/assets/icons/focus-tomato-idle-symbolic.svg"
extension_uuid="focus-tomato-state@gxy.local"
extension_source="${source_root}/shell-extension/${extension_uuid}"
extension_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/gnome-shell/extensions/${extension_uuid}"

if [[ ! -f "${source_root}/focus_tomato/__init__.py" ]]; then
    echo "错误：请从 Focus Tomato 源码目录运行安装脚本。" >&2
    exit 1
fi

install -d "${app_root}/focus_tomato" "${app_root}/assets/icons" "${app_root}/bin"
install -d "${local_prefix}/bin" "${applications_dir}" "${autostart_dir}"
install -d "${extension_dir}"

find "${source_root}/focus_tomato" -maxdepth 1 -type f -name '*.py' -exec \
    install -m 0644 -t "${app_root}/focus_tomato" {} +
find "${source_root}/assets/icons" -maxdepth 1 -type f -name '*.svg' -exec \
    install -m 0644 -t "${app_root}/assets/icons" {} +
install -m 0755 "${source_root}/bin/focus-tomato" "${app_root}/bin/focus-tomato"
install -m 0644 "${extension_source}/metadata.json" "${extension_dir}/metadata.json"
install -m 0644 "${extension_source}/extension.js" "${extension_dir}/extension.js"

quoted_app_launcher="$(printf '%q' "${app_root}/bin/focus-tomato")"
{
    printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail'
    printf 'exec %s "$@"\n' "${quoted_app_launcher}"
} >"${launcher}"
chmod 0755 "${launcher}"

{
    printf '%s\n' \
        '[Desktop Entry]' \
        'Type=Application' \
        'Name=Focus Tomato' \
        'Name[zh_CN]=专注番茄钟' \
        'Comment=A quiet Pomodoro timer in the system panel' \
        'Comment[zh_CN]=安静地驻留在系统顶栏的番茄钟' \
        "Exec=${launcher}" \
        "Icon=${icon_file}" \
        'Terminal=false' \
        'Categories=Utility;' \
        'StartupNotify=false' \
        'X-GNOME-UsesNotifications=true'
} >"${desktop_file}"

{
    printf '%s\n' \
        '[Desktop Entry]' \
        'Type=Application' \
        'Name=Focus Tomato' \
        'Name[zh_CN]=专注番茄钟' \
        "Exec=${launcher} --autostart" \
        "Icon=${icon_file}" \
        'Terminal=false' \
        'NoDisplay=true' \
        'X-GNOME-Autostart-enabled=true'
} >"${autostart_file}"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${applications_dir}" >/dev/null 2>&1 || true
fi

extension_enabled=false
if command -v gnome-extensions >/dev/null 2>&1; then
    gdbus call --session \
        --dest org.gnome.Shell.Extensions \
        --object-path /org/gnome/Shell/Extensions \
        --method org.gnome.Shell.Extensions.ReloadExtension \
        "${extension_uuid}" >/dev/null 2>&1 || true
    if gnome-extensions enable "${extension_uuid}" >/dev/null 2>&1; then
        extension_enabled=true
    fi
fi
if [[ "${extension_enabled}" == false ]] && command -v python3 >/dev/null 2>&1; then
    python3 -c 'from gi.repository import Gio; import sys; settings = Gio.Settings.new("org.gnome.shell"); values = list(settings.get_strv("enabled-extensions")); uuid = sys.argv[1]; values.append(uuid) if uuid not in values else None; settings.set_strv("enabled-extensions", values)' "${extension_uuid}"
fi

echo "Focus Tomato 已安装。"
echo "应用入口：${desktop_file}"
echo "启动命令：${launcher}"
if [[ "${extension_enabled}" == true ]]; then
    echo "3px 状态色带扩展已启用。"
else
    echo "状态色带扩展已安装并设为启用；注销并重新登录后生效。"
fi
