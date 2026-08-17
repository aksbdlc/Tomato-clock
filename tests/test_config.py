from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from focus_tomato.config import (
    DEFAULT_CONFIG_TEXT,
    ConfigError,
    TimerConfig,
    ensure_config,
    load_config,
)


VALID_VALUES: dict[str, object] = {
    "focus_minutes": 25,
    "short_break_minutes": 5,
    "long_break_minutes": 15,
    "sessions_before_long_break": 4,
    "countdown_visible_minutes": 0,
    "show_progress_ring": False,
    "show_state_band": True,
    "autostart": True,
}


def toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def config_text(values: dict[str, object] | None = None) -> str:
    configured = VALID_VALUES if values is None else values
    return "\n".join(
        f"{key} = {toml_scalar(value)}" for key, value in configured.items()
    ) + "\n"


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "nested" / "config.toml"

    def write_values(self, **overrides: object) -> None:
        values = dict(VALID_VALUES)
        values.update(overrides)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(config_text(values), encoding="utf-8")

    def test_missing_config_is_created_with_defaults(self) -> None:
        self.assertFalse(self.path.exists())
        loaded = load_config(self.path)

        self.assertEqual(loaded, TimerConfig())
        self.assertEqual(self.path.read_text(encoding="utf-8"), DEFAULT_CONFIG_TEXT)

    def test_ensure_config_preserves_existing_file(self) -> None:
        self.path.parent.mkdir(parents=True)
        original = config_text({**VALID_VALUES, "focus_minutes": 42})
        self.path.write_text(original, encoding="utf-8")

        self.assertEqual(ensure_config(self.path), self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_valid_values_are_converted_from_minutes_to_seconds(self) -> None:
        self.write_values(
            focus_minutes=40,
            short_break_minutes=7,
            long_break_minutes=22,
            sessions_before_long_break=3,
            countdown_visible_minutes=0,
            show_progress_ring=True,
            show_state_band=False,
            autostart=False,
        )

        self.assertEqual(
            load_config(self.path),
            TimerConfig(
                focus_seconds=2_400,
                short_break_seconds=420,
                long_break_seconds=1_320,
                sessions_before_long_break=3,
                countdown_visible_seconds=0,
                show_progress_ring=True,
                show_state_band=False,
                autostart=False,
            ),
        )

    def test_unknown_key_is_rejected(self) -> None:
        values = dict(VALID_VALUES)
        values["sound"] = True
        self.path.parent.mkdir(parents=True)
        self.path.write_text(config_text(values), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "未知配置项.*sound"):
            load_config(self.path)

    def test_missing_required_key_is_rejected(self) -> None:
        optional_keys = {"show_progress_ring", "show_state_band"}
        for missing_key in set(VALID_VALUES) - optional_keys:
            with self.subTest(key=missing_key):
                values = dict(VALID_VALUES)
                values.pop(missing_key)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(config_text(values), encoding="utf-8")

                with self.assertRaises(ConfigError):
                    load_config(self.path)

    def test_integer_fields_reject_boolean_float_and_string_values(self) -> None:
        for key in (
            "focus_minutes",
            "short_break_minutes",
            "long_break_minutes",
            "sessions_before_long_break",
            "countdown_visible_minutes",
        ):
            for invalid_value in (True, 1.5, "5"):
                with self.subTest(key=key, invalid_value=invalid_value):
                    self.write_values(**{key: invalid_value})
                    with self.assertRaisesRegex(ConfigError, f"{key} 必须是整数"):
                        load_config(self.path)

    def test_integer_ranges_are_enforced(self) -> None:
        invalid_cases = {
            "focus_minutes": (0, 1_441),
            "short_break_minutes": (0, 1_441),
            "long_break_minutes": (0, 1_441),
            "sessions_before_long_break": (0, 101),
            "countdown_visible_minutes": (-1, 1_441),
        }
        for key, values in invalid_cases.items():
            for invalid_value in values:
                with self.subTest(key=key, invalid_value=invalid_value):
                    self.write_values(**{key: invalid_value})
                    with self.assertRaisesRegex(ConfigError, f"{key} 必须在"):
                        load_config(self.path)

    def test_integer_range_boundaries_are_accepted(self) -> None:
        accepted_cases = (
            ("focus_minutes", 1),
            ("focus_minutes", 1_440),
            ("short_break_minutes", 1),
            ("short_break_minutes", 1_440),
            ("long_break_minutes", 1),
            ("long_break_minutes", 1_440),
            ("sessions_before_long_break", 1),
            ("sessions_before_long_break", 100),
            ("countdown_visible_minutes", 0),
            ("countdown_visible_minutes", 1_440),
        )
        for key, value in accepted_cases:
            with self.subTest(key=key, value=value):
                self.write_values(**{key: value})
                load_config(self.path)

    def test_autostart_requires_a_toml_boolean(self) -> None:
        for invalid_value in (0, 1, "true"):
            with self.subTest(invalid_value=invalid_value):
                self.write_values(autostart=invalid_value)
                with self.assertRaisesRegex(
                    ConfigError, "autostart 必须是 true 或 false"
                ):
                    load_config(self.path)

    def test_visual_toggles_require_toml_booleans(self) -> None:
        for key in ("show_progress_ring", "show_state_band"):
            for invalid_value in (0, 1, "true"):
                with self.subTest(key=key, invalid_value=invalid_value):
                    self.write_values(**{key: invalid_value})
                    with self.assertRaisesRegex(
                        ConfigError, f"{key} 必须是 true 或 false"
                    ):
                        load_config(self.path)

    def test_invalid_toml_is_wrapped_as_config_error(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("focus_minutes = [\n", encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "TOML 格式错误"):
            load_config(self.path)


if __name__ == "__main__":
    unittest.main()
