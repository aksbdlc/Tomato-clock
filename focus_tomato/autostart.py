from __future__ import annotations

import os
from pathlib import Path


AUTOSTART_NAME = "focus-tomato.desktop"


def autostart_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "autostart" / AUTOSTART_NAME


def _desktop_contents(executable: str, enabled: bool) -> str:
    return f"""[Desktop Entry]
Type=Application
Name=Focus Tomato
Comment=Quiet panel Pomodoro timer
Exec={executable} --autostart
TryExec={executable}
Terminal=false
NoDisplay=true
StartupNotify=false
X-GNOME-Autostart-enabled={'true' if enabled else 'false'}
Hidden={'false' if enabled else 'true'}
"""


def sync_autostart(enabled: bool) -> None:
    """Keep the per-user autostart entry aligned with config.toml."""

    target = autostart_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    executable = os.environ.get("FOCUS_TOMATO_EXECUTABLE")
    if not executable:
        executable = str(Path.home() / ".local" / "bin" / "focus-tomato")
    target.write_text(_desktop_contents(executable, enabled), encoding="utf-8")

