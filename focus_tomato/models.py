from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TimerStatus(str, Enum):
    IDLE = "idle"
    FOCUS_RUNNING = "focus_running"
    FOCUS_PAUSED = "focus_paused"
    BREAK_READY = "break_ready"
    BREAK_RUNNING = "break_running"

    @property
    def is_running(self) -> bool:
        return self in {self.FOCUS_RUNNING, self.BREAK_RUNNING}

    @property
    def is_focus(self) -> bool:
        return self in {self.FOCUS_RUNNING, self.FOCUS_PAUSED}

    @property
    def is_break(self) -> bool:
        return self is self.BREAK_RUNNING


class BreakKind(str, Enum):
    SHORT = "short"
    LONG = "long"


class TimerEvent(str, Enum):
    FOCUS_COMPLETED = "focus_completed"
    FOCUS_ENDED_EARLY = "focus_ended_early"
    BREAK_COMPLETED = "break_completed"


@dataclass
class RuntimeSnapshot:
    status: TimerStatus = TimerStatus.IDLE
    remaining_ms: int = 0
    completed_in_cycle: int = 0
    break_kind: BreakKind | None = None
    phase_total_ms: int = 0
    # A rest phase uses a wall-clock deadline so it continues while the
    # computer sleeps and after the process restarts.  Focus deliberately
    # does not use this field: it is still driven by a monotonic clock.
    break_deadline_wall_ms: int | None = None
    # The wall-clock instant at which the current focus phase began.  It is
    # persisted solely to evaluate the rolling qualified-focus window across
    # application restarts; remaining focus time remains monotonic-clock
    # driven.
    focus_started_at_wall_ms: int | None = None
    # Persist the recommendation with the pending/running rest so reopening
    # the app never depends on reconstructing a transient completion event.
    long_break_recommended: bool = False


@dataclass(frozen=True)
class DailyStats:
    focus_ms: int = 0
    completed_sessions: int = 0
