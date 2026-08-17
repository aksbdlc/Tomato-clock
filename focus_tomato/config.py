from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 on Ubuntu 22.04
    import tomli as tomllib


APP_DIR_NAME = "focus-tomato"

DEFAULT_CONFIG_TEXT = """# Focus Tomato configuration
# Changes apply to the next phase; the active timer is not reset.
focus_minutes = 25
short_break_minutes = 5
long_break_minutes = 15
sessions_before_long_break = 4
countdown_visible_minutes = 0
show_progress_ring = false
show_state_band = true
autostart = true
"""


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TimerConfig:
    focus_seconds: int = 25 * 60
    short_break_seconds: int = 5 * 60
    long_break_seconds: int = 15 * 60
    sessions_before_long_break: int = 4
    countdown_visible_seconds: int = 0
    show_progress_ring: bool = False
    show_state_band: bool = True
    autostart: bool = True


def config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / APP_DIR_NAME / "config.toml"


def ensure_config(path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return target


def _require_int(data: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return value


def load_config(path: Path | None = None) -> TimerConfig:
    target = ensure_config(path)
    try:
        with target.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 格式错误：{exc}") from exc

    allowed = {
        "focus_minutes",
        "short_break_minutes",
        "long_break_minutes",
        "sessions_before_long_break",
        "countdown_visible_minutes",
        "show_progress_ring",
        "show_state_band",
        "autostart",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"未知配置项：{', '.join(unknown)}")

    focus_minutes = _require_int(raw, "focus_minutes", 1, 24 * 60)
    short_break_minutes = _require_int(raw, "short_break_minutes", 1, 24 * 60)
    long_break_minutes = _require_int(raw, "long_break_minutes", 1, 24 * 60)
    sessions = _require_int(raw, "sessions_before_long_break", 1, 100)
    countdown = _require_int(raw, "countdown_visible_minutes", 0, 24 * 60)
    show_progress_ring = raw.get("show_progress_ring", False)
    if not isinstance(show_progress_ring, bool):
        raise ConfigError("show_progress_ring 必须是 true 或 false")
    show_state_band = raw.get("show_state_band", True)
    if not isinstance(show_state_band, bool):
        raise ConfigError("show_state_band 必须是 true 或 false")
    autostart = raw.get("autostart")
    if not isinstance(autostart, bool):
        raise ConfigError("autostart 必须是 true 或 false")

    return TimerConfig(
        focus_seconds=focus_minutes * 60,
        short_break_seconds=short_break_minutes * 60,
        long_break_seconds=long_break_minutes * 60,
        sessions_before_long_break=sessions,
        countdown_visible_seconds=countdown * 60,
        show_progress_ring=show_progress_ring,
        show_state_band=show_state_band,
        autostart=autostart,
    )
