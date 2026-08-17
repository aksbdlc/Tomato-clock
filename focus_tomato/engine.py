from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from .clock import Clock, SystemClock
from .config import TimerConfig
from .models import (
    BreakKind,
    DailyStats,
    RuntimeSnapshot,
    TimerEvent,
    TimerStatus,
)
from .storage import SQLiteStore
from .time_utils import local_day, split_duration_by_local_day


EARLY_END_MINIMUM_FOCUS_MS = 60_000


class TimerEngine:
    """Pure timer state machine; UI code only invokes actions and renders state."""

    def __init__(
        self,
        store: SQLiteStore,
        config: TimerConfig,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.clock = clock or SystemClock()
        now_monotonic_ms = self.clock.monotonic_ms()
        now_wall_ms = self.clock.wall_epoch_ms()
        self._snapshot, needs_runtime_migration = store.load_runtime_with_metadata(
            now_wall_ms
        )
        self._pending_focus: defaultdict[str, int] = defaultdict(
            int,
            store.load_current_focus_pending(),
        )
        self._anchor_monotonic_ms = now_monotonic_ms
        self._anchor_wall_ms = now_wall_ms
        self.recovered = False
        # Events that occurred while the process was not running.  The UI
        # consumes these once startup has initialized desktop notifications.
        self.startup_events: list[TimerEvent] = []

        if self._snapshot.status is TimerStatus.FOCUS_RUNNING:
            self._snapshot.status = TimerStatus.FOCUS_PAUSED
            self.recovered = True
            self._commit()
        elif self._snapshot.status is TimerStatus.BREAK_RUNNING:
            # A break intentionally survives process restarts.  It can expire
            # during downtime, but that is not a "recovered as paused" state.
            break_completed = self._sync_break_to_wall(now_wall_ms)
            if needs_runtime_migration or break_completed:
                self._commit()
            if break_completed:
                self.startup_events.append(TimerEvent.BREAK_COMPLETED)

        if (
            self._snapshot.status is TimerStatus.IDLE
            and self._pending_focus
        ):
            # The snapshot and provisional rows are written atomically.  This
            # only cleans up data left by an interrupted legacy/manual write.
            self._pending_focus.clear()
            self._commit()

    @property
    def snapshot(self) -> RuntimeSnapshot:
        return replace(self._snapshot)

    def set_config(self, config: TimerConfig) -> None:
        self.config = config

    def _reset_anchors(self) -> None:
        self._anchor_monotonic_ms = self.clock.monotonic_ms()
        self._anchor_wall_ms = self.clock.wall_epoch_ms()

    def _add_focus_duration(self, start_wall_ms: int, duration_ms: int) -> None:
        for day, allocated_ms in split_duration_by_local_day(
            start_wall_ms, duration_ms
        ).items():
            self._pending_focus[day] += allocated_ms

    def _commit(
        self,
        completion_days: list[str] | None = None,
        *,
        finalize_current_focus: bool = False,
        discard_current_focus: bool = False,
    ) -> None:
        if finalize_current_focus and discard_current_focus:
            raise ValueError("focus allocations cannot be finalized and discarded")
        finalized_allocations = (
            dict(self._pending_focus) if finalize_current_focus else {}
        )
        if finalize_current_focus or discard_current_focus:
            persisted_pending: dict[str, int] = {}
        else:
            persisted_pending = dict(self._pending_focus)
        self.store.commit(
            self._snapshot,
            finalized_allocations,
            completion_days or (),
            updated_at_wall_ms=self.clock.wall_epoch_ms(),
            current_focus_pending=persisted_pending,
        )
        if finalize_current_focus or discard_current_focus:
            self._pending_focus.clear()

    def _complete_focus(self, completion_wall_ms: int) -> tuple[TimerEvent, str]:
        completion_day = local_day(completion_wall_ms)
        next_cycle_count = self._snapshot.completed_in_cycle + 1
        if next_cycle_count >= self.config.sessions_before_long_break:
            kind = BreakKind.LONG
            next_cycle_count = 0
            break_seconds = self.config.long_break_seconds
        else:
            kind = BreakKind.SHORT
            break_seconds = self.config.short_break_seconds

        self._snapshot = RuntimeSnapshot(
            status=TimerStatus.BREAK_RUNNING,
            remaining_ms=break_seconds * 1000,
            completed_in_cycle=next_cycle_count,
            break_kind=kind,
            phase_total_ms=break_seconds * 1000,
            break_deadline_wall_ms=completion_wall_ms + break_seconds * 1000,
        )
        return TimerEvent.FOCUS_COMPLETED, completion_day

    def _focus_elapsed_ms(self) -> int:
        return max(0, self._snapshot.phase_total_ms - self._snapshot.remaining_ms)

    def _start_break_for_early_focus_end(
        self,
        actual_focus_ms: int,
        start_wall_ms: int,
    ) -> None:
        break_ms = actual_focus_ms // 5
        self._snapshot = RuntimeSnapshot(
            status=TimerStatus.BREAK_RUNNING,
            remaining_ms=break_ms,
            completed_in_cycle=self._snapshot.completed_in_cycle,
            break_kind=BreakKind.SHORT,
            phase_total_ms=break_ms,
            break_deadline_wall_ms=start_wall_ms + break_ms,
        )

    def _complete_break(self) -> TimerEvent:
        self._snapshot = RuntimeSnapshot(
            status=TimerStatus.IDLE,
            remaining_ms=0,
            completed_in_cycle=self._snapshot.completed_in_cycle,
            break_kind=None,
            phase_total_ms=0,
            break_deadline_wall_ms=None,
        )
        return TimerEvent.BREAK_COMPLETED

    def _sync_break_to_wall(self, now_wall_ms: int) -> bool:
        """Set break remaining time from its persistent wall-clock deadline.

        Returns whether this call completed the break.  A missing deadline is
        defensive-only: storage normally supplies one for every active break.
        """
        if self._snapshot.status is not TimerStatus.BREAK_RUNNING:
            return False
        deadline_ms = self._snapshot.break_deadline_wall_ms
        if deadline_ms is None:
            deadline_ms = now_wall_ms + self._snapshot.remaining_ms
            self._snapshot.break_deadline_wall_ms = deadline_ms
        self._snapshot.remaining_ms = max(0, deadline_ms - now_wall_ms)
        if self._snapshot.remaining_ms > 0:
            return False
        self._complete_break()
        return True

    def _advance(self) -> list[TimerEvent]:
        now_monotonic_ms = self.clock.monotonic_ms()
        now_wall_ms = self.clock.wall_epoch_ms()
        events: list[TimerEvent] = []
        completion_days: list[str] = []

        if self._snapshot.status is TimerStatus.FOCUS_RUNNING:
            elapsed_ms = max(0, now_monotonic_ms - self._anchor_monotonic_ms)
            if elapsed_ms > 0:
                consume_ms = min(elapsed_ms, self._snapshot.remaining_ms)
                cursor_wall_ms = self._anchor_wall_ms + consume_ms
                if consume_ms > 0:
                    self._add_focus_duration(self._anchor_wall_ms, consume_ms)
                self._snapshot.remaining_ms -= consume_ms

                if self._snapshot.remaining_ms <= 0:
                    event, completion_day = self._complete_focus(cursor_wall_ms)
                    events.append(event)
                    completion_days.append(completion_day)

        if self._snapshot.status is TimerStatus.BREAK_RUNNING:
            if self._sync_break_to_wall(now_wall_ms):
                events.append(TimerEvent.BREAK_COMPLETED)

        self._anchor_monotonic_ms = now_monotonic_ms
        self._anchor_wall_ms = now_wall_ms

        if events:
            self._commit(
                completion_days,
                finalize_current_focus=TimerEvent.FOCUS_COMPLETED in events,
            )
        return events

    def tick(self) -> list[TimerEvent]:
        return self._advance()

    def checkpoint(self) -> list[TimerEvent]:
        events = self._advance()
        if self._pending_focus:
            self._commit()
        elif self._snapshot.status.is_running:
            # Persist the latest remaining time even during a break.
            self._commit()
        return events

    def start_focus(self, duration_seconds: int | None = None) -> None:
        if self._snapshot.status is not TimerStatus.IDLE:
            return
        focus_seconds = (
            self.config.focus_seconds
            if duration_seconds is None
            else int(duration_seconds)
        )
        if focus_seconds <= 0:
            raise ValueError("focus duration must be positive")
        self._snapshot = RuntimeSnapshot(
            status=TimerStatus.FOCUS_RUNNING,
            remaining_ms=focus_seconds * 1000,
            completed_in_cycle=self._snapshot.completed_in_cycle,
            break_kind=None,
            phase_total_ms=focus_seconds * 1000,
            break_deadline_wall_ms=None,
        )
        self._pending_focus.clear()
        self._reset_anchors()
        self._commit()

    def pause_focus(self) -> list[TimerEvent]:
        events = self._advance()
        if self._snapshot.status is TimerStatus.FOCUS_RUNNING:
            self._snapshot.status = TimerStatus.FOCUS_PAUSED
            self._commit()
        return events

    def resume(self) -> None:
        if self._snapshot.status is TimerStatus.FOCUS_PAUSED:
            self._snapshot.status = TimerStatus.FOCUS_RUNNING
        else:
            return
        self._reset_anchors()
        self._commit()

    def end_focus(self) -> list[TimerEvent]:
        events = self._advance()
        if self._snapshot.status.is_focus:
            actual_focus_ms = self._focus_elapsed_ms()
            if actual_focus_ms < EARLY_END_MINIMUM_FOCUS_MS:
                self._snapshot = RuntimeSnapshot(
                    status=TimerStatus.IDLE,
                    remaining_ms=0,
                    completed_in_cycle=self._snapshot.completed_in_cycle,
                    break_kind=None,
                    phase_total_ms=0,
                    break_deadline_wall_ms=None,
                )
                self._commit(discard_current_focus=True)
            else:
                self._start_break_for_early_focus_end(
                    actual_focus_ms,
                    self.clock.wall_epoch_ms(),
                )
                events.append(TimerEvent.FOCUS_ENDED_EARLY)
                self._commit(finalize_current_focus=True)
        return events

    def end_break(self) -> list[TimerEvent]:
        events = self._advance()
        if self._snapshot.status.is_break:
            self._snapshot = RuntimeSnapshot(
                status=TimerStatus.IDLE,
                remaining_ms=0,
                completed_in_cycle=self._snapshot.completed_in_cycle,
                break_kind=None,
                phase_total_ms=0,
                break_deadline_wall_ms=None,
            )
            self._commit()
        return events

    def pause_for_sleep(self) -> list[TimerEvent]:
        events = self._advance()
        if self._snapshot.status is TimerStatus.FOCUS_RUNNING:
            self._snapshot.status = TimerStatus.FOCUS_PAUSED
            self._commit()
        elif self._snapshot.status is TimerStatus.BREAK_RUNNING:
            # The deadline keeps advancing during sleep; checkpoint it before
            # suspension so an unclean shutdown cannot lose the latest state.
            self._commit()
        return events

    def shutdown(self) -> list[TimerEvent]:
        # Focus pauses on exit; an active break keeps its wall-clock deadline.
        return self.pause_for_sleep()

    def today_stats(self) -> DailyStats:
        day = local_day(self.clock.wall_epoch_ms())
        persisted = self.store.stats_for_day(day)
        provisional_focus_ms = (
            self._pending_focus.get(day, 0)
            if self._focus_elapsed_ms() >= EARLY_END_MINIMUM_FOCUS_MS
            else 0
        )
        return DailyStats(
            focus_ms=persisted.focus_ms + provisional_focus_ms,
            completed_sessions=persisted.completed_sessions,
        )
