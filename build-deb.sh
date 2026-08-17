#!/usr/bin/env bash
set -euo pipefail

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
version="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "${source_root}/pyproject.toml")"

if [[ -z "${version}" ]]; then
    echo "错误：无法从 pyproject.toml 读取版本号。" >&2
    exit 1
fi
if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "错误：需要 dpkg-deb（Ubuntu/Debian 通常由 dpkg-dev 提供）。" >&2
    exit 1
fi

stage_root="$(mktemp -d "${TMPDIR:-/tmp}/focus-tomato-deb.XXXXXX")"
trap 'rm -rf -- "${stage_root}"' EXIT
package_root="${stage_root}/focus-tomato-${version}"
output_dir="${source_root}/dist"
output_file="${output_dir}/focus-tomato_${version}_all.deb"

install -d \
    "${package_root}/DEBIAN" \
    "${package_root}/usr/bin" \
    "${package_root}/usr/lib/focus-tomato/focus_tomato" \
    "${package_root}/usr/share/focus-tomato/icons" \
    "${package_root}/usr/share/gnome-shell/extensions/focus-tomato-state@gxy.local" \
    "${package_root}/usr/share/applications" \
    "${package_root}/etc/xdg/autostart" \
    "${output_dir}"

sed "s/@VERSION@/${version}/g" \
    "${source_root}/packaging/debian/control" >"${package_root}/DEBIAN/control"
find "${source_root}/focus_tomato" -maxdepth 1 -type f -name '*.py' -exec \
    install -m 0644 -t "${package_root}/usr/lib/focus-tomato/focus_tomato" {} +
find "${source_root}/assets/icons" -maxdepth 1 -type f -name '*.svg' -exec \
    install -m 0644 -t "${package_root}/usr/share/focus-tomato/icons" {} +
install -m 0644 \
    "${source_root}/shell-extension/focus-tomato-state@gxy.local/metadata.json" \
    "${source_root}/shell-extension/focus-tomato-state@gxy.local/extension.js" \
    "${package_root}/usr/share/gnome-shell/extensions/focus-tomato-state@gxy.local"
install -m 0755 "${source_root}/packaging/debian/focus-tomato" \
    "${package_root}/usr/bin/focus-tomato"
install -m 0644 "${source_root}/packaging/debian/focus-tomato.desktop" \
    "${package_root}/usr/share/applications/focus-tomato.desktop"
install -m 0644 "${source_root}/packaging/debian/focus-tomato-autostart.desktop" \
    "${package_root}/etc/xdg/autostart/focus-tomato.desktop"

dpkg-deb --root-owner-group --build "${package_root}" "${output_file}"
echo "已生成：${output_file}"
