from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Notify", "0.7")

from gi.repository import AyatanaAppIndicator3, Gdk, Gio, GLib, Gtk, Notify

from .autostart import sync_autostart
from .config import ConfigError, config_path, load_config
from .engine import TimerEngine
from .models import BreakKind, RuntimeSnapshot, TaskOutcome, TimerEvent, TimerStatus
from .storage import SQLiteStore


APP_ID = "io.github.focustomato.FocusTomato"
APP_NAME = "Focus Tomato"
STATE_OBJECT_PATH = "/io/github/focustomato/FocusTomato/State"
STATE_INTERFACE = "io.github.focustomato.FocusTomato.State"
STATE_INTERFACE_XML = f"""
<node>
  <interface name="{STATE_INTERFACE}">
    <method name="GetState">
      <arg name="status" type="s" direction="out"/>
      <arg name="break_kind" type="s" direction="out"/>
    </method>
    <signal name="StateChanged">
      <arg name="status" type="s"/>
      <arg name="break_kind" type="s"/>
    </signal>
  </interface>
</node>
"""


_REST_SCREEN_BREAK = "break"
_REST_SCREEN_FOCUS_READY = "focus-ready"
_FOCUS_DURATION_MINUTES_MIN = 1
_FOCUS_DURATION_MINUTES_MAX = 720
_REST_SCREEN_FULLSCREEN_RETRY_DELAY_MS = 100
_REST_SCREEN_FULLSCREEN_MAX_ATTEMPTS = 2
_MODIFIER_KEYVALS = frozenset(
    {
        Gdk.KEY_Shift_L,
        Gdk.KEY_Shift_R,
        Gdk.KEY_Control_L,
        Gdk.KEY_Control_R,
        Gdk.KEY_Alt_L,
        Gdk.KEY_Alt_R,
        Gdk.KEY_Meta_L,
        Gdk.KEY_Meta_R,
        Gdk.KEY_Super_L,
        Gdk.KEY_Super_R,
        Gdk.KEY_Hyper_L,
        Gdk.KEY_Hyper_R,
        Gdk.KEY_Caps_Lock,
        Gdk.KEY_Num_Lock,
        Gdk.KEY_Scroll_Lock,
        Gdk.KEY_ISO_Level3_Shift,
        Gdk.KEY_ISO_Level5_Shift,
        Gdk.KEY_Mode_switch,
    }
)

_REST_SCREEN_CSS = b"""
.focus-tomato-rest-screen {
    background-color: #000000;
}

.focus-tomato-rest-screen.long-break {
    background-color: #5b2a86;
}

.focus-tomato-rest-screen.focus-ready {
    background-color: #9b3d3a;
}

.focus-tomato-rest-screen .rest-countdown {
    color: #ffffff;
    font-size: 120px;
    font-weight: 300;
}

.focus-tomato-rest-screen .rest-stats {
    color: rgba(255, 255, 255, 0.48);
    font-size: 20px;
}

.focus-tomato-rest-screen .rest-long-break-hint {
    color: rgba(255, 255, 255, 0.34);
    font-size: 14px;
}

.focus-tomato-rest-screen .focus-ready-button {
    background-image: none;
    background-color: rgba(0, 0, 0, 0.16);
    border: 1px solid rgba(255, 255, 255, 0.78);
    border-radius: 5px;
    color: #ffffff;
    font-size: 22px;
    padding: 13px 28px;
}

.focus-tomato-rest-screen .focus-ready-button:disabled {
    color: rgba(255, 255, 255, 0.42);
    border-color: rgba(255, 255, 255, 0.28);
}

.focus-tomato-rest-screen .focus-ready-hint {
    color: rgba(255, 255, 255, 0.72);
    font-size: 14px;
}

.focus-tomato-task-row {
    margin-top: 4px;
}
.focus-tomato-task-entry {
    background-color: rgba(255,255,255,0.95);
    color: #222222;
    border-radius: 18px;
    padding: 7px 10px;
    min-width: 260px;
}
.focus-tomato-task-icon {
    background-image: none;
    background-color: rgba(0,0,0,0.18);
    color: #ffffff;
    border-radius: 18px;
    min-width: 36px;
    min-height: 36px;
    padding: 0;
}
.focus-tomato-task-choice {
    background-image: none;
    background-color: rgba(0,0,0,0.18);
    color: #ffffff;
    border-radius: 5px;
    padding: 5px 10px;
}
"""


def _format_remaining(remaining_ms: int) -> str:
    total_seconds = max(0, math.ceil(remaining_ms / 1000))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _format_duration(duration_ms: int) -> str:
    total_minutes = duration_ms // 60_000
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}小时{minutes}分"
    if hours:
        return f"{hours}小时"
    return f"{minutes}分钟"


class FocusTomatoApplication(Gtk.Application):
    def __init__(self, autostart_launch: bool = False) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.autostart_launch = autostart_launch
        self.store: SQLiteStore | None = None
        self.engine: TimerEngine | None = None
        self.indicator: AyatanaAppIndicator3.Indicator | None = None
        self.menu: Gtk.Menu | None = None
        self.status_item: Gtk.MenuItem | None = None
        self.primary_item: Gtk.MenuItem | None = None
        self.secondary_item: Gtk.MenuItem | None = None
        self.today_item: Gtk.MenuItem | None = None
        self._tick_id = 0
        self._checkpoint_id = 0
        self._config_monitor: Gio.FileMonitor | None = None
        self._config_reload_id = 0
        self._system_bus: Gio.DBusConnection | None = None
        self._sleep_subscription_id = 0
        self._state_node_info: Gio.DBusNodeInfo | None = None
        self._state_registration_id = 0
        self._last_broadcast_state: tuple[str, str] | None = None
        self._recent_focus_minutes: int | None = None
        self._rest_screen: Gtk.Window | None = None
        self._rest_screen_background: Gtk.EventBox | None = None
        self._rest_screen_break_content: Gtk.Box | None = None
        self._rest_screen_focus_content: Gtk.Box | None = None
        self._rest_screen_countdown: Gtk.Label | None = None
        self._rest_screen_stats: Gtk.Label | None = None
        self._rest_screen_long_break_hint: Gtk.Label | None = None
        self._rest_screen_focus_countdown: Gtk.Label | None = None
        self._rest_screen_focus_hint: Gtk.Label | None = None
        self._rest_screen_focus_button: Gtk.Button | None = None
        self._focus_goal_plus: Gtk.Button | None = None
        self._focus_goal_entry: Gtk.Entry | None = None
        self._focus_goal_confirm: Gtk.Button | None = None
        self._focus_goal_clear: Gtk.Button | None = None
        self._break_task_label: Gtk.Label | None = None
        self._break_task_plus: Gtk.Button | None = None
        self._break_task_entry: Gtk.Entry | None = None
        self._break_task_confirm: Gtk.Button | None = None
        self._break_task_clear: Gtk.Button | None = None
        self._break_task_entry_row: Gtk.Box | None = None
        self._break_task_completed: Gtk.Button | None = None
        self._break_task_incomplete: Gtk.Button | None = None
        self._focus_goal_expanded = False
        self._break_task_expanded = False
        self._rest_screen_lower_slot_size_group: Gtk.SizeGroup | None = None
        self._rest_screen_focus_duration_text = ""
        self._rest_screen_focus_replace_on_input = False
        self._rest_screen_page: str | None = None
        self._rest_screen_css: Gtk.CssProvider | None = None
        self._rest_screen_fullscreen_pending = False
        self._rest_screen_fullscreen_attempts = 0
        self._rest_screen_fullscreen_source = 0
        self._rest_screen_is_fullscreen = False
        self._active_notifications: list[Notify.Notification] = []
        self._shutting_down = False

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        try:
            config = load_config()
        except ConfigError as exc:
            print(f"Focus Tomato 配置错误：{exc}", file=sys.stderr)
            config = None

        if config is None:
            self.quit()
            return
        if self.autostart_launch and not config.autostart:
            self.quit()
            return

        # The application normally lives in the indicator. Its fullscreen rest
        # window is created only when a user chooses to open it.
        self.hold()
        sync_autostart(config.autostart)
        self.store = SQLiteStore()
        self.engine = TimerEngine(self.store, config)
        self._export_state_interface()
        Notify.init(APP_NAME)
        self._build_indicator()
        self._watch_config()
        self._watch_sleep()
        self._tick_id = GLib.timeout_add(250, self._on_tick)
        self._checkpoint_id = GLib.timeout_add_seconds(5, self._on_checkpoint)

        if self.engine.recovered:
            self._notify(
                "计时已恢复为暂停",
                "上次运行的阶段已保留，请从顶栏菜单继续。",
            )
        self._handle_events(self.engine.startup_events)
        self._render()

    def do_activate(self) -> None:
        # Application activation only exposes the panel indicator. Opening the
        # focus launch surface remains an explicit action from its menu.
        if self.indicator is not None:
            self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

    def _icon_dir(self) -> str:
        configured = os.environ.get("FOCUS_TOMATO_ICON_DIR")
        if configured:
            return configured
        return str(Path(__file__).resolve().parent.parent / "assets" / "icons")

    def _build_indicator(self) -> None:
        icon_dir = self._icon_dir()
        self.indicator = AyatanaAppIndicator3.Indicator.new_with_path(
            "focus-tomato",
            "focus-tomato-idle-symbolic",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            icon_dir,
        )
        self.indicator.set_title(APP_NAME)
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label="等待开始")
        self.status_item.set_sensitive(False)
        self.status_item.connect("activate", self._on_status_item)
        self.primary_item = Gtk.MenuItem(label="开始专注…")
        self.primary_item.connect("activate", self._on_primary)
        self.secondary_item = Gtk.MenuItem(label="结束本轮")
        self.secondary_item.connect("activate", self._on_secondary)
        self.today_item = Gtk.MenuItem(label="今日 0 次 · 0分钟")
        self.today_item.set_sensitive(False)
        quit_item = Gtk.MenuItem(label="退出")
        quit_item.connect("activate", self._on_quit)

        for item in (
            self.status_item,
            Gtk.SeparatorMenuItem(),
            self.primary_item,
            self.secondary_item,
            Gtk.SeparatorMenuItem(),
            self.today_item,
            Gtk.SeparatorMenuItem(),
            quit_item,
        ):
            self.menu.append(item)
        self.menu.show_all()
        self.indicator.set_menu(self.menu)

    def _on_status_item(self, _item: Gtk.MenuItem) -> None:
        """Use the live rest row as the repeatable fullscreen entry point."""
        self._show_rest_screen()

    def _ensure_rest_screen(self) -> Gtk.Window:
        if self._rest_screen is not None:
            return self._rest_screen

        css = Gtk.CssProvider()
        css.load_from_data(_REST_SCREEN_CSS)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen,
                css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        self._rest_screen_css = css

        window = Gtk.ApplicationWindow(application=self)
        window.set_title(APP_NAME)
        window.set_decorated(False)
        window.set_skip_taskbar_hint(True)
        window.set_skip_pager_hint(True)
        window.connect("delete-event", self._on_rest_screen_delete)
        window.connect("map", self._on_rest_screen_map)
        window.connect("key-press-event", self._on_rest_screen_key_press)
        window.connect("window-state-event", self._on_rest_screen_window_state)

        background = Gtk.EventBox()
        background.set_visible_window(True)
        background.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        background.connect("button-press-event", self._on_rest_screen_background_press)
        background.get_style_context().add_class("focus-tomato-rest-screen")
        window.add(background)

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        background.add(overlay)

        alignment = Gtk.Alignment.new(0.5, 0.5, 0.0, 0.0)
        overlay.add(alignment)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)
        content.set_margin_start(32)
        content.set_margin_end(32)
        content.set_margin_top(32)
        content.set_margin_bottom(32)
        alignment.add(content)

        break_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        break_content.set_halign(Gtk.Align.CENTER)
        countdown = Gtk.Label()
        countdown.set_xalign(0.5)
        countdown.get_style_context().add_class("rest-countdown")
        stats = Gtk.Label()
        stats.set_xalign(0.5)
        stats.set_valign(Gtk.Align.CENTER)
        stats.get_style_context().add_class("rest-stats")
        long_break_hint = Gtk.Label()
        long_break_hint.set_xalign(0.5)
        long_break_hint.get_style_context().add_class("rest-long-break-hint")
        break_content.pack_start(countdown, False, False, 0)
        break_content.pack_start(stats, False, False, 0)
        break_content.pack_start(long_break_hint, False, False, 0)
        break_task_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        break_task_box.set_halign(Gtk.Align.CENTER)
        break_task_label = Gtk.Label()
        break_task_label.set_xalign(0.5)
        break_task_box.pack_start(break_task_label, False, False, 0)
        break_task_controls = Gtk.Box(spacing=8)
        break_task_plus = Gtk.Button(label="＋")
        break_task_plus.get_style_context().add_class("focus-tomato-task-icon")
        break_task_plus.connect("clicked", lambda _b: self._toggle_break_task_editor(True))
        break_task_controls.pack_start(break_task_plus, False, False, 0)
        break_task_completed = Gtk.Button(label="✓ 已完成")
        break_task_completed.get_style_context().add_class("focus-tomato-task-choice")
        break_task_completed.connect("clicked", lambda _b: self._confirm_break_task(TaskOutcome.COMPLETED))
        break_task_incomplete = Gtk.Button(label="未完成")
        break_task_incomplete.get_style_context().add_class("focus-tomato-task-choice")
        break_task_incomplete.connect("clicked", lambda _b: self._confirm_break_task(TaskOutcome.INCOMPLETE))
        break_task_controls.pack_start(break_task_completed, False, False, 0)
        break_task_controls.pack_start(break_task_incomplete, False, False, 0)
        break_task_box.pack_start(break_task_controls, False, False, 0)
        break_task_entry_row = Gtk.Box(spacing=5)
        break_task_entry = Gtk.Entry()
        break_task_entry.set_max_length(20)
        break_task_entry.set_placeholder_text("记录完成的任务")
        break_task_entry.get_style_context().add_class("focus-tomato-task-entry")
        break_task_confirm = Gtk.Button(label="✓")
        break_task_clear = Gtk.Button(label="×")
        for button in (break_task_confirm, break_task_clear):
            button.get_style_context().add_class("focus-tomato-task-icon")
        break_task_confirm.connect("clicked", lambda _b: self._confirm_break_task(TaskOutcome.COMPLETED))
        break_task_clear.connect("clicked", lambda _b: self._clear_break_task())
        break_task_entry.connect("activate", lambda _e: self._confirm_break_task(TaskOutcome.COMPLETED))
        break_task_entry.connect("changed", lambda entry: self._on_break_task_draft_changed(entry))
        break_task_entry_row.pack_start(break_task_entry, True, True, 0)
        break_task_entry_row.pack_start(break_task_confirm, False, False, 0)
        break_task_entry_row.pack_start(break_task_clear, False, False, 0)
        break_task_box.pack_start(break_task_entry_row, False, False, 0)
        break_content.pack_start(break_task_box, False, False, 0)
        content.pack_start(break_content, False, False, 0)

        focus_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        focus_content.set_halign(Gtk.Align.CENTER)
        focus_countdown = Gtk.Label()
        focus_countdown.set_xalign(0.5)
        focus_countdown.get_style_context().add_class("rest-countdown")
        start_button = Gtk.Button(label="开始专注")
        start_button.get_style_context().add_class("focus-ready-button")
        start_button.connect("clicked", self._on_rest_screen_start_focus)
        focus_content.pack_start(focus_countdown, False, False, 0)
        focus_content.pack_start(start_button, False, False, 0)
        focus_task_row = Gtk.Box(spacing=5)
        focus_task_row.set_halign(Gtk.Align.CENTER)
        focus_goal_plus = Gtk.Button(label="＋")
        focus_goal_plus.get_style_context().add_class("focus-tomato-task-icon")
        focus_goal_plus.connect("clicked", lambda _b: self._toggle_focus_goal_editor(True))
        focus_task_row.pack_start(focus_goal_plus, False, False, 0)
        focus_goal_entry = Gtk.Entry()
        focus_goal_entry.set_max_length(20)
        focus_goal_entry.set_placeholder_text("这次专注的目标")
        focus_goal_entry.get_style_context().add_class("focus-tomato-task-entry")
        focus_goal_confirm = Gtk.Button(label="✓")
        focus_goal_clear = Gtk.Button(label="×")
        for button in (focus_goal_confirm, focus_goal_clear):
            button.get_style_context().add_class("focus-tomato-task-icon")
        focus_goal_confirm.connect("clicked", lambda _b: self._confirm_focus_goal())
        focus_goal_clear.connect("clicked", lambda _b: self._clear_focus_goal())
        focus_goal_entry.connect("activate", lambda _e: self._confirm_focus_goal())
        focus_task_row.pack_start(focus_goal_entry, True, True, 0)
        focus_task_row.pack_start(focus_goal_confirm, False, False, 0)
        focus_task_row.pack_start(focus_goal_clear, False, False, 0)
        focus_content.pack_start(focus_task_row, False, False, 0)
        content.pack_start(focus_content, False, False, 0)

        lower_slot_size_group = Gtk.SizeGroup.new(Gtk.SizeGroupMode.VERTICAL)
        lower_slot_size_group.add_widget(stats)
        lower_slot_size_group.add_widget(start_button)

        focus_hint = Gtk.Label(
            label="输入分钟数 · ←→ ±1分钟 · ↑↓ ±5分钟 · Enter 开始 · Esc 退出"
        )
        focus_hint.set_halign(Gtk.Align.CENTER)
        focus_hint.set_valign(Gtk.Align.END)
        focus_hint.set_margin_bottom(28)
        focus_hint.set_margin_start(24)
        focus_hint.set_margin_end(24)
        focus_hint.get_style_context().add_class("focus-ready-hint")
        overlay.add_overlay(focus_hint)

        # Prepare children without mapping the top-level window at its natural
        # content size.  On Wayland that initial map can race the later
        # fullscreen request and leave a small window in the upper-left corner.
        background.show_all()
        focus_content.hide()
        focus_hint.hide()
        focus_goal_entry.hide(); focus_goal_confirm.hide(); focus_goal_clear.hide()
        break_task_entry_row.hide()

        self._rest_screen = window
        self._rest_screen_background = background
        self._rest_screen_break_content = break_content
        self._rest_screen_focus_content = focus_content
        self._rest_screen_countdown = countdown
        self._rest_screen_stats = stats
        self._rest_screen_long_break_hint = long_break_hint
        self._rest_screen_focus_countdown = focus_countdown
        self._rest_screen_focus_hint = focus_hint
        self._rest_screen_focus_button = start_button
        self._focus_goal_plus = focus_goal_plus
        self._focus_goal_entry = focus_goal_entry
        self._focus_goal_confirm = focus_goal_confirm
        self._focus_goal_clear = focus_goal_clear
        self._break_task_label = break_task_label
        self._break_task_plus = break_task_plus
        self._break_task_entry = break_task_entry
        self._break_task_confirm = break_task_confirm
        self._break_task_clear = break_task_clear
        self._break_task_entry_row = break_task_entry_row
        self._break_task_completed = break_task_completed
        self._break_task_incomplete = break_task_incomplete
        self._rest_screen_lower_slot_size_group = lower_slot_size_group
        return window

    def _show_rest_screen(self) -> None:
        """Open the current rest countdown, never a stale post-rest page."""
        if self.engine is None:
            return
        snapshot = self.engine.snapshot
        if not snapshot.status.is_break:
            return

        window = self._ensure_rest_screen()
        self._show_rest_countdown_page(snapshot)
        self._present_rest_screen_fullscreen(window)

    def _show_rest_countdown_page(self, snapshot: RuntimeSnapshot) -> None:
        self._ensure_rest_screen()
        assert self._rest_screen_background is not None
        assert self._rest_screen_break_content is not None
        assert self._rest_screen_focus_content is not None
        assert self._rest_screen_focus_hint is not None

        self._rest_screen_page = _REST_SCREEN_BREAK
        style_context = self._rest_screen_background.get_style_context()
        style_context.remove_class("long-break")
        style_context.remove_class("focus-ready")
        self._rest_screen_focus_content.hide()
        self._rest_screen_focus_hint.hide()
        self._rest_screen_break_content.show()
        self._render_rest_screen(snapshot)

    def _show_focus_ready_page(self) -> None:
        """Render the red next-focus launch surface in the shared window."""
        self._ensure_rest_screen()
        assert self._rest_screen_background is not None
        assert self._rest_screen_break_content is not None
        assert self._rest_screen_focus_content is not None
        assert self._rest_screen_focus_hint is not None

        self._rest_screen_page = _REST_SCREEN_FOCUS_READY
        style_context = self._rest_screen_background.get_style_context()
        style_context.remove_class("long-break")
        style_context.add_class("focus-ready")
        self._rest_screen_break_content.hide()
        self._rest_screen_focus_content.show()
        self._rest_screen_focus_hint.show()
        default_minutes = self._recent_focus_minutes or 25
        self._set_rest_screen_focus_duration(
            str(default_minutes),
            replace_on_input=True,
        )
        goal = self.engine.next_goal if self.engine is not None else None
        if goal:
            assert self._focus_goal_entry is not None
            self._focus_goal_entry.set_text(goal)
            self._toggle_focus_goal_editor(True)
        else:
            self._toggle_focus_goal_editor(False)
        if self._rest_screen is not None:
            self._rest_screen.set_focus(None)

    def _show_focus_ready_screen(self) -> None:
        """Open the full-screen focus launch surface from an idle state."""
        if self.engine is None or self.engine.snapshot.status is not TimerStatus.IDLE:
            return
        window = self._ensure_rest_screen()
        self._show_focus_ready_page()
        self._present_rest_screen_fullscreen(window)

    def _present_rest_screen_fullscreen(self, window: Gtk.Window) -> None:
        """Map the shared surface, then request and verify fullscreen state."""
        self._cancel_rest_screen_fullscreen_request()
        self._rest_screen_fullscreen_pending = True
        self._rest_screen_fullscreen_attempts = 0
        window.show()
        window.present()
        self._schedule_rest_screen_fullscreen_request()

    def _on_rest_screen_map(self, _window: Gtk.Widget) -> None:
        """Wait for a mapped Wayland surface before requesting fullscreen."""
        self._schedule_rest_screen_fullscreen_request()

    def _schedule_rest_screen_fullscreen_request(self) -> None:
        window = self._rest_screen
        if (
            not self._rest_screen_fullscreen_pending
            or self._rest_screen_fullscreen_source
            or window is None
            or not window.get_mapped()
        ):
            return
        self._rest_screen_fullscreen_source = GLib.idle_add(
            self._request_rest_screen_fullscreen,
        )

    def _request_rest_screen_fullscreen(self) -> bool:
        self._rest_screen_fullscreen_source = 0
        window = self._rest_screen
        if (
            not self._rest_screen_fullscreen_pending
            or window is None
            or not window.get_visible()
            or not window.get_mapped()
        ):
            return GLib.SOURCE_REMOVE

        self._rest_screen_fullscreen_attempts += 1
        window.fullscreen()
        if self._rest_screen_fullscreen_pending:
            self._rest_screen_fullscreen_source = GLib.timeout_add(
                _REST_SCREEN_FULLSCREEN_RETRY_DELAY_MS,
                self._verify_rest_screen_fullscreen,
            )
        return GLib.SOURCE_REMOVE

    def _verify_rest_screen_fullscreen(self) -> bool:
        self._rest_screen_fullscreen_source = 0
        if (
            not self._rest_screen_fullscreen_pending
            or self._rest_screen is None
            or not self._rest_screen.get_visible()
        ):
            return GLib.SOURCE_REMOVE
        if self._rest_screen_is_fullscreen:
            self._rest_screen_fullscreen_pending = False
            return GLib.SOURCE_REMOVE
        if self._rest_screen_fullscreen_attempts < _REST_SCREEN_FULLSCREEN_MAX_ATTEMPTS:
            self._schedule_rest_screen_fullscreen_request()
            return GLib.SOURCE_REMOVE

        self._rest_screen_fullscreen_pending = False
        print("休息屏幕未进入全屏。", file=sys.stderr)
        return GLib.SOURCE_REMOVE

    def _on_rest_screen_window_state(
        self,
        _window: Gtk.Widget,
        event: Gdk.EventWindowState,
    ) -> bool:
        self._rest_screen_is_fullscreen = bool(
            event.new_window_state & Gdk.WindowState.FULLSCREEN
        )
        if self._rest_screen_is_fullscreen:
            self._rest_screen_fullscreen_pending = False
            self._rest_screen_fullscreen_attempts = 0
            self._cancel_rest_screen_fullscreen_request()
        return False

    def _cancel_rest_screen_fullscreen_request(self) -> None:
        if self._rest_screen_fullscreen_source:
            GLib.source_remove(self._rest_screen_fullscreen_source)
            self._rest_screen_fullscreen_source = 0

    def _render_rest_screen(self, snapshot: RuntimeSnapshot) -> None:
        if self._rest_screen_page != _REST_SCREEN_BREAK:
            return
        assert self._rest_screen_countdown is not None
        assert self._rest_screen_stats is not None
        assert self._rest_screen_long_break_hint is not None
        assert self.engine is not None
        self._rest_screen_countdown.set_text(_format_remaining(snapshot.remaining_ms))
        stats = self.engine.today_stats()
        self._rest_screen_stats.set_text(
            f"今日 {stats.completed_sessions} 次 · {_format_duration(stats.focus_ms)}"
        )
        if snapshot.long_break_recommended:
            suggested_minutes = self.engine.config.long_break_seconds // 60
            required_sessions = self.engine.config.sessions_before_long_break
            self._rest_screen_long_break_hint.set_text(
                f"已连续完成 {required_sessions} 次专注，可以考虑休息 {suggested_minutes} 分钟"
            )
            self._rest_screen_long_break_hint.show()
        else:
            self._rest_screen_long_break_hint.hide()
        self._render_break_task(snapshot)

    def _toggle_focus_goal_editor(self, expanded: bool) -> None:
        if expanded == self._focus_goal_expanded:
            return
        self._focus_goal_expanded = expanded
        for widget in (self._focus_goal_entry, self._focus_goal_confirm, self._focus_goal_clear):
            if widget is not None:
                self._animate_task_widget(widget, expanded)
        if self._focus_goal_plus is not None:
            self._focus_goal_plus.set_visible(not expanded)
        if expanded and self._focus_goal_entry is not None:
            self._focus_goal_entry.grab_focus()

    def _confirm_focus_goal(self) -> None:
        if self.engine is None:
            return
        goal = self._focus_goal_entry.get_text().strip()[:20] if self._focus_goal_entry else ""
        self.engine.set_next_goal(goal or None)
        if self._focus_goal_entry is not None:
            self._focus_goal_entry.set_text(goal)
        self._start_focus_from_rest_screen()

    def _clear_focus_goal(self) -> None:
        if self._focus_goal_entry is not None:
            self._focus_goal_entry.set_text("")
        if self.engine is not None:
            self.engine.set_next_goal(None)
        self._toggle_focus_goal_editor(False)

    def _toggle_break_task_editor(self, expanded: bool) -> None:
        was_expanded = self._break_task_expanded
        if expanded == was_expanded:
            return
        self._break_task_expanded = expanded
        if self._break_task_entry_row is not None:
            self._animate_task_widget(self._break_task_entry_row, expanded)
        if self._break_task_plus is not None:
            self._break_task_plus.set_visible(not expanded)
        if expanded and not was_expanded and self._break_task_entry is not None:
            self._break_task_entry.grab_focus()

    def _animate_task_widget(self, widget: Gtk.Widget, visible: bool) -> None:
        """Use a short opacity/size transition for the plus-to-entry reveal."""
        if visible:
            widget.show()
            widget.set_opacity(0.0)
            step = {"value": 0}
            def reveal() -> bool:
                step["value"] += 1
                widget.set_opacity(min(1.0, step["value"] / 6.0))
                widget.set_size_request(42 + step["value"] * 42, -1)
                return step["value"] < 6
            GLib.timeout_add(25, reveal)
        else:
            widget.set_opacity(0.0)
            widget.set_size_request(-1, -1)
            widget.hide()

    def _confirm_break_task(self, outcome: TaskOutcome) -> None:
        if self.engine is None or not self.engine.snapshot.status.is_break:
            return
        text = self._break_task_entry.get_text() if self._break_task_entry else ""
        if text.strip() or self.engine.snapshot.focus_goal:
            self.engine.confirm_break_task(text, outcome)
            self._toggle_break_task_editor(True)
            if self._break_task_entry is not None:
                self._break_task_entry.set_text(text.strip()[:20])
            self._render()

    def _on_break_task_draft_changed(self, entry: Gtk.Entry) -> None:
        if self.engine is not None and self._break_task_expanded:
            self.engine.set_break_task_draft(entry.get_text())

    def _clear_break_task(self) -> None:
        if self.engine is not None:
            self.engine.clear_break_task()
        if self._break_task_entry is not None:
            self._break_task_entry.set_text("")
        self._toggle_break_task_editor(False)
        self._render()

    def _render_break_task(self, snapshot: RuntimeSnapshot) -> None:
        if self._break_task_label is None:
            return
        goal = snapshot.focus_goal
        if snapshot.break_task and self._break_task_entry is not None:
            if self._break_task_entry.get_text() != snapshot.break_task:
                self._break_task_entry.set_text(snapshot.break_task)
        if goal:
            self._break_task_label.set_text(f"目标：{goal}")
            self._break_task_label.show()
            for button in (self._break_task_completed, self._break_task_incomplete):
                if button is not None:
                    button.show()
            if self._break_task_plus is not None:
                self._break_task_plus.hide()
            self._toggle_break_task_editor(True)
        else:
            self._break_task_label.set_text("记录这次休息前完成的任务")
            self._break_task_label.show()
            for button in (self._break_task_completed, self._break_task_incomplete):
                if button is not None:
                    button.hide()
            if not self._break_task_expanded:
                self._toggle_break_task_editor(False)

    def _rest_screen_is_visible(self) -> bool:
        return self._rest_screen is not None and self._rest_screen.get_visible()

    def _close_rest_screen(self) -> None:
        self._rest_screen_fullscreen_pending = False
        self._cancel_rest_screen_fullscreen_request()
        if self._rest_screen is not None:
            self._rest_screen.hide()
        self._rest_screen_page = None

    def _on_rest_screen_delete(
        self,
        _window: Gtk.Window,
        _event: Gdk.Event,
    ) -> bool:
        self._close_rest_screen()
        return True

    def _on_rest_screen_background_press(
        self,
        _background: Gtk.EventBox,
        event: Gdk.EventButton,
    ) -> bool:
        if (
            event.button != Gdk.BUTTON_PRIMARY
            or self._rest_screen_page != _REST_SCREEN_BREAK
        ):
            return False
        self._close_rest_screen()
        return True

    def _on_rest_screen_key_press(
        self,
        _window: Gtk.Window,
        event: Gdk.EventKey,
    ) -> bool:
        if self._rest_screen_page == _REST_SCREEN_BREAK:
            if event.keyval == Gdk.KEY_Escape:
                self._close_rest_screen()
                return True
            if event.keyval in {Gdk.KEY_Return, Gdk.KEY_KP_Enter}:
                if self._break_task_expanded:
                    self._confirm_break_task(TaskOutcome.COMPLETED)
                return True
            control_pressed = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
            if event.keyval == Gdk.KEY_space and control_pressed:
                return True
            adjustments = {
                Gdk.KEY_Right: 1,
                Gdk.KEY_Left: -1,
                Gdk.KEY_Up: 5,
                Gdk.KEY_Down: -5,
                Gdk.KEY_space: 1,
            }
            adjustment = adjustments.get(event.keyval)
            if adjustment is not None and not control_pressed:
                assert self.engine is not None
                self._handle_events(
                    self.engine.adjust_break_minutes(adjustment)
                )
                self._render()
                return True
            return False

        if self._rest_screen_page != _REST_SCREEN_FOCUS_READY:
            return False
        return self._handle_focus_ready_key(event)

    def _handle_focus_ready_key(self, event: Gdk.EventKey) -> bool:
        keyval = event.keyval
        if keyval == Gdk.KEY_Escape:
            self._close_rest_screen()
            return True
        adjustments = {
            Gdk.KEY_Right: 1,
            Gdk.KEY_Left: -1,
            Gdk.KEY_Up: 5,
            Gdk.KEY_Down: -5,
        }
        adjustment = adjustments.get(keyval)
        if adjustment is not None:
            self._adjust_rest_screen_focus_duration(adjustment)
            return True
        if keyval in {Gdk.KEY_Return, Gdk.KEY_KP_Enter}:
            if self._focus_goal_expanded:
                self._confirm_focus_goal()
            else:
                self._start_focus_from_rest_screen()
            return True
        if keyval in {Gdk.KEY_BackSpace, Gdk.KEY_Delete}:
            self._delete_rest_screen_focus_duration()
            return True
        unicode_value = Gdk.keyval_to_unicode(keyval)
        if ord("0") <= unicode_value <= ord("9"):
            self._append_rest_screen_focus_duration(chr(unicode_value))
            return True
        return False

    def _set_rest_screen_focus_duration(
        self,
        text: str,
        *,
        replace_on_input: bool,
    ) -> None:
        self._rest_screen_focus_duration_text = text
        self._rest_screen_focus_replace_on_input = replace_on_input
        minutes = self._rest_screen_focus_minutes()
        if self._rest_screen_focus_countdown is not None:
            if text.isdecimal():
                self._rest_screen_focus_countdown.set_text(
                    _format_remaining(int(text) * 60_000)
                )
            else:
                self._rest_screen_focus_countdown.set_text("--:--")
        if self._rest_screen_focus_button is not None:
            self._rest_screen_focus_button.set_sensitive(minutes is not None)

    def _rest_screen_focus_minutes(self) -> int | None:
        text = self._rest_screen_focus_duration_text
        if not text.isdecimal():
            return None
        minutes = int(text)
        if not _FOCUS_DURATION_MINUTES_MIN <= minutes <= _FOCUS_DURATION_MINUTES_MAX:
            return None
        return minutes

    def _append_rest_screen_focus_duration(self, digit: str) -> None:
        if self._rest_screen_focus_replace_on_input:
            text = digit
        else:
            text = self._rest_screen_focus_duration_text + digit
        if len(text) > 3:
            return
        self._set_rest_screen_focus_duration(text, replace_on_input=False)

    def _delete_rest_screen_focus_duration(self) -> None:
        if self._rest_screen_focus_replace_on_input:
            text = ""
        else:
            text = self._rest_screen_focus_duration_text[:-1]
        self._set_rest_screen_focus_duration(text, replace_on_input=False)

    def _adjust_rest_screen_focus_duration(self, adjustment: int) -> None:
        current = self._rest_screen_focus_minutes()
        if current is None:
            current = self._recent_focus_minutes or 25
        minutes = max(
            _FOCUS_DURATION_MINUTES_MIN,
            min(_FOCUS_DURATION_MINUTES_MAX, current + adjustment),
        )
        self._set_rest_screen_focus_duration(
            str(minutes),
            replace_on_input=True,
        )

    def _on_rest_screen_start_focus(self, _button: Gtk.Button) -> None:
        self._start_focus_from_rest_screen()

    def _start_focus_from_rest_screen(self) -> bool:
        if self.engine is None or self.engine.snapshot.status is not TimerStatus.IDLE:
            return False
        minutes = self._rest_screen_focus_minutes()
        if minutes is None:
            return False
        self._recent_focus_minutes = minutes
        goal = self._focus_goal_entry.get_text().strip()[:20] if self._focus_goal_expanded and self._focus_goal_entry else None
        self.engine.start_focus(minutes * 60, goal=goal)
        self._close_rest_screen()
        self._render()
        return True

    def _watch_config(self) -> None:
        file = Gio.File.new_for_path(str(config_path()))
        self._config_monitor = file.monitor_file(Gio.FileMonitorFlags.NONE, None)
        self._config_monitor.connect("changed", self._on_config_changed)

    def _on_config_changed(self, *_args: object) -> None:
        if self._config_reload_id:
            GLib.source_remove(self._config_reload_id)
        self._config_reload_id = GLib.timeout_add(250, self._reload_config)

    def _reload_config(self) -> bool:
        self._config_reload_id = 0
        try:
            config = load_config()
        except ConfigError as exc:
            self._notify("配置未生效", str(exc))
            return GLib.SOURCE_REMOVE
        assert self.engine is not None
        self.engine.set_config(config)
        sync_autostart(config.autostart)
        self._broadcast_state()
        return GLib.SOURCE_REMOVE

    def _export_state_interface(self) -> None:
        connection = self.get_dbus_connection()
        if connection is None:
            print("无法导出状态接口：应用 D-Bus 尚未连接", file=sys.stderr)
            return
        self._state_node_info = Gio.DBusNodeInfo.new_for_xml(STATE_INTERFACE_XML)
        self._state_registration_id = connection.register_object(
            STATE_OBJECT_PATH,
            self._state_node_info.interfaces[0],
            self._on_state_method_call,
            None,
            None,
        )

    def _on_state_method_call(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _object_path: str,
        _interface_name: str,
        method_name: str,
        _parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        if method_name == "GetState":
            invocation.return_value(GLib.Variant("(ss)", self._state_tuple()))
            return
        invocation.return_dbus_error(
            f"{STATE_INTERFACE}.UnknownMethod",
            f"Unknown method: {method_name}",
        )

    def _state_tuple(self) -> tuple[str, str]:
        if self.engine is None or not self.engine.config.show_state_band:
            return (TimerStatus.IDLE.value, "")
        snapshot = self.engine.snapshot
        break_kind = snapshot.break_kind.value if snapshot.break_kind else ""
        return (snapshot.status.value, break_kind)

    def _broadcast_state(self) -> None:
        state = self._state_tuple()
        if state == self._last_broadcast_state:
            return
        self._last_broadcast_state = state
        print(
            f"[Focus Tomato] state={state[0]} break={state[1] or '-'}",
            file=sys.stderr,
            flush=True,
        )
        self._emit_state(state)
        # GNOME Shell 42 can deliver an older asynchronous GetState reply
        # after this signal. Repeat the authoritative current state so even a
        # cached pre-fix extension self-corrects during this login session.
        GLib.timeout_add(750, self._confirm_current_state)
        GLib.timeout_add(2_500, self._confirm_current_state)

    def _emit_state(self, state: tuple[str, str]) -> None:
        connection = self.get_dbus_connection()
        if connection is None or not self._state_registration_id:
            return
        connection.emit_signal(
            None,
            STATE_OBJECT_PATH,
            STATE_INTERFACE,
            "StateChanged",
            GLib.Variant("(ss)", state),
        )

    def _confirm_current_state(self) -> bool:
        if not self._shutting_down:
            self._emit_state(self._state_tuple())
        return GLib.SOURCE_REMOVE

    def _watch_sleep(self) -> None:
        try:
            self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._sleep_subscription_id = self._system_bus.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                "/org/freedesktop/login1",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep,
            )
        except GLib.Error as exc:
            print(f"无法监听系统休眠：{exc}", file=sys.stderr)

    def _on_prepare_for_sleep(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        _signal: str,
        parameters: GLib.Variant,
    ) -> None:
        sleeping = parameters.unpack()[0]
        if sleeping and self.engine is not None:
            self._handle_events(self.engine.pause_for_sleep())
            self._render()

    def _on_tick(self) -> bool:
        if self._shutting_down or self.engine is None:
            return GLib.SOURCE_REMOVE
        self._handle_events(self.engine.tick())
        self._render()
        return GLib.SOURCE_CONTINUE

    def _on_checkpoint(self) -> bool:
        if self._shutting_down or self.engine is None:
            return GLib.SOURCE_REMOVE
        self._handle_events(self.engine.checkpoint())
        self._render()
        return GLib.SOURCE_CONTINUE

    def _handle_events(self, events: list[TimerEvent]) -> None:
        for event in events:
            if event is TimerEvent.FOCUS_COMPLETED:
                assert self.engine is not None
                snapshot = self.engine.snapshot
                self._notify(
                    "专注完成",
                    f"做得好，点击开始 {_format_duration(snapshot.remaining_ms)}休息。",
                    default_action=self._start_pending_break,
                )
            elif event is TimerEvent.FOCUS_ENDED_EARLY:
                assert self.engine is not None
                self._notify(
                    "专注提前结束",
                    "已记录实际专注时长，点击开始缩短休息。",
                    default_action=self._start_pending_break,
                )
            elif event is TimerEvent.BREAK_COMPLETED:
                if self._rest_screen_is_visible():
                    self._show_focus_ready_page()
                else:
                    self._notify("休息结束", "准备好后，从顶栏开始下一轮专注。")

    def _notify(
        self,
        summary: str,
        body: str,
        default_action: Callable[[], None] | None = None,
    ) -> None:
        icon_uri = (Path(self._icon_dir()) / "focus-tomato-focus-symbolic.svg").as_uri()
        notification = Notify.Notification.new(summary, body, icon_uri)
        notification.set_app_name(APP_NAME)
        notification.set_urgency(Notify.Urgency.NORMAL)
        notification.set_hint("suppress-sound", GLib.Variant.new_boolean(True))
        notification.set_hint("desktop-entry", GLib.Variant.new_string("focus-tomato"))
        if default_action is not None:
            # The libnotify "default" action is dispatched when the notification
            # body is clicked, rather than requiring a separate action button.
            def activate_default_action(*_args: object) -> None:
                default_action()

            notification.add_action(
                "default",
                "开始休息",
                activate_default_action,
                None,
            )
            self._active_notifications.append(notification)
            notification.connect("closed", self._on_notification_closed)
        try:
            notification.show()
        except GLib.Error as exc:
            print(f"无法显示通知：{exc}", file=sys.stderr)

    def _on_notification_closed(self, notification: Notify.Notification) -> None:
        try:
            self._active_notifications.remove(notification)
        except ValueError:
            pass

    def _close_active_notifications(self) -> None:
        notifications = tuple(self._active_notifications)
        self._active_notifications.clear()
        for notification in notifications:
            try:
                notification.close()
            except GLib.Error:
                pass

    def _start_pending_break(self) -> None:
        if self.engine is None or not self.engine.start_break():
            return
        self._close_active_notifications()
        self._show_rest_screen()
        self._render()

    def _render(self) -> None:
        if self.engine is None or self.indicator is None:
            return
        snapshot = self.engine.snapshot
        labels = {
            TimerStatus.IDLE: "等待开始",
            TimerStatus.FOCUS_RUNNING: f"专注中 · {_format_remaining(snapshot.remaining_ms)}",
            TimerStatus.FOCUS_PAUSED: f"专注已暂停 · {_format_remaining(snapshot.remaining_ms)}",
            TimerStatus.BREAK_READY: f"等待休息 · {_format_remaining(snapshot.remaining_ms)}",
            TimerStatus.BREAK_RUNNING: f"休息中 · {_format_remaining(snapshot.remaining_ms)}",
        }
        icons = {
            TimerStatus.IDLE: "focus-tomato-idle-symbolic",
            TimerStatus.FOCUS_RUNNING: "focus-tomato-focus-symbolic",
            TimerStatus.FOCUS_PAUSED: "focus-tomato-paused-symbolic",
            TimerStatus.BREAK_READY: "focus-tomato-break-symbolic",
            TimerStatus.BREAK_RUNNING: "focus-tomato-break-symbolic",
        }
        assert self.status_item is not None
        assert self.primary_item is not None
        assert self.secondary_item is not None
        assert self.today_item is not None

        self.status_item.set_label(labels[snapshot.status])
        self.status_item.set_sensitive(snapshot.status is TimerStatus.BREAK_RUNNING)
        icon_name = icons[snapshot.status]
        if self.engine.config.show_progress_ring and snapshot.status.is_running:
            icon_name = self._progress_ring_icon(snapshot)
        self.indicator.set_icon_full(icon_name, labels[snapshot.status])

        if snapshot.status is TimerStatus.IDLE:
            self.primary_item.set_label("开始专注…")
            self.primary_item.show()
            self.secondary_item.hide()
        elif snapshot.status is TimerStatus.FOCUS_RUNNING:
            self.primary_item.set_label("暂停")
            self.primary_item.show()
            self.secondary_item.set_label("提前结束专注")
            self.secondary_item.show()
        elif snapshot.status is TimerStatus.FOCUS_PAUSED:
            self.primary_item.set_label("继续专注")
            self.primary_item.show()
            self.secondary_item.set_label("提前结束专注")
            self.secondary_item.show()
        elif snapshot.status is TimerStatus.BREAK_READY:
            self.primary_item.set_label("开始休息")
            self.primary_item.show()
            self.secondary_item.set_label("跳过休息")
            self.secondary_item.show()
        elif snapshot.status is TimerStatus.BREAK_RUNNING:
            self.primary_item.hide()
            self.secondary_item.set_label("结束休息")
            self.secondary_item.show()

        if (
            snapshot.status.is_running
            and snapshot.remaining_ms <= self.engine.config.countdown_visible_seconds * 1000
        ):
            self.indicator.set_label(_format_remaining(snapshot.remaining_ms), "00:00")
        else:
            self.indicator.set_label("", "00:00")

        stats = self.engine.today_stats()
        self.today_item.set_label(
            f"今日 {stats.completed_sessions} 次 · {_format_duration(stats.focus_ms)}"
        )
        if self._rest_screen_is_visible():
            if self._rest_screen_page == _REST_SCREEN_BREAK and snapshot.status.is_break:
                self._render_rest_screen(snapshot)
            elif self._rest_screen_page == _REST_SCREEN_BREAK:
                # This is the manual-end fallback. Natural completion reaches
                # _show_focus_ready_page() above before the next render.
                self._close_rest_screen()
        self._broadcast_state()

    def _progress_ring_icon(self, snapshot: RuntimeSnapshot) -> str:
        # Eight coarse steps show approximate progress without exposing a
        # numerical countdown or animating every second.
        if snapshot.phase_total_ms <= 0:
            bucket = 8
        else:
            ratio = max(
                0.0,
                min(1.0, snapshot.remaining_ms / snapshot.phase_total_ms),
            )
            bucket = max(0, min(8, math.ceil(ratio * 8)))
        if snapshot.status is TimerStatus.FOCUS_RUNNING:
            phase = "focus"
        elif snapshot.break_kind is BreakKind.LONG:
            phase = "long-break"
        else:
            phase = "short-break"
        return f"focus-tomato-ring-{phase}-{bucket}"

    def _on_primary(self, _item: Gtk.MenuItem) -> None:
        assert self.engine is not None
        status = self.engine.snapshot.status
        if status is TimerStatus.IDLE:
            self._show_focus_ready_screen()
            return
        elif status is TimerStatus.FOCUS_RUNNING:
            self._handle_events(self.engine.pause_focus())
        elif status is TimerStatus.FOCUS_PAUSED:
            self.engine.resume()
        elif status is TimerStatus.BREAK_READY:
            self._start_pending_break()
            return
        self._render()

    def _on_secondary(self, _item: Gtk.MenuItem) -> None:
        assert self.engine is not None
        if self.engine.snapshot.status.is_focus:
            self._handle_events(self.engine.end_focus())
        elif self.engine.snapshot.status is TimerStatus.BREAK_READY:
            if self.engine.skip_break():
                self._close_active_notifications()
        elif self.engine.snapshot.status.is_break:
            self._handle_events(self.engine.end_break())
            # Manually ending a rest returns to idle; only a naturally elapsed
            # rest may turn an already-open screen into the red launch page.
            self._close_rest_screen()
        self._render()

    def _on_quit(self, _item: Gtk.MenuItem) -> None:
        self._clean_shutdown()
        self.quit()

    def _clean_shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        if self._checkpoint_id:
            GLib.source_remove(self._checkpoint_id)
            self._checkpoint_id = 0
        if self.engine is not None:
            self._handle_events(self.engine.shutdown())
        self._rest_screen_fullscreen_pending = False
        self._cancel_rest_screen_fullscreen_request()
        if self._rest_screen is not None:
            self._rest_screen.destroy()
            self._rest_screen = None
        self._active_notifications.clear()
        if self._system_bus is not None and self._sleep_subscription_id:
            self._system_bus.signal_unsubscribe(self._sleep_subscription_id)
        connection = self.get_dbus_connection()
        if connection is not None and self._state_registration_id:
            connection.unregister_object(self._state_registration_id)
            self._state_registration_id = 0
        if self.store is not None:
            self.store.close()
        Notify.uninit()

    def do_shutdown(self) -> None:
        self._clean_shutdown()
        Gtk.Application.do_shutdown(self)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quiet GNOME panel Pomodoro timer")
    parser.add_argument("--autostart", action="store_true")
    args, gtk_args = parser.parse_known_args(argv)
    app = FocusTomatoApplication(autostart_launch=args.autostart)
    try:
        return app.run([sys.argv[0], *gtk_args])
    except KeyboardInterrupt:
        return 130
