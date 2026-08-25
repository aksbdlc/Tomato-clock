from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from uuid import uuid4

from .clock import Clock, SystemClock
from .config import TimerConfig
from .models import (
    BreakKind,
    DailyStats,
    RuntimeSnapshot,
    TaskOutcome,
    TimerEvent,
    TimerStatus,
)
from .storage import SQLiteStore
from .time_utils import local_day, split_duration_by_local_day


# A focus must reach five active minutes before it becomes part of the user's
# record.  This deliberately applies to both naturally completed and manually
# ended focuses.  Paused time does not count.
MINIMUM_RECORDED_FOCUS_MS = 5 * 60_000
CONTINUOUS_BREAK_WINDOW_MS = 20 * 60_000
MAXIMUM_BREAK_MS = 24 * 60 * 60_000


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
        self._recent_cycle_focus_intervals = list(
            store.load_cycle_focus_intervals()
        )
        self._trim_cycle_focus_history()
        # Older releases persisted only starts. They cannot prove the complete
        # between-focus gaps required by the new recommendation rule.
        needs_cycle_migration = (
            self._snapshot.completed_in_cycle
            != len(self._recent_cycle_focus_intervals)
        )
        self._snapshot.completed_in_cycle = len(
            self._recent_cycle_focus_intervals
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
            if needs_runtime_migration or needs_cycle_migration or break_completed:
                self._commit()
            if break_completed:
                self.startup_events.append(TimerEvent.BREAK_COMPLETED)
        elif needs_runtime_migration or needs_cycle_migration:
            self._commit()

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
        self._trim_cycle_focus_history()
        self._snapshot.completed_in_cycle = len(
            self._recent_cycle_focus_intervals
        )
        self._commit()

    def _cycle_history_limit(self) -> int:
        return max(0, self.config.sessions_before_long_break - 1)

    def _trim_cycle_focus_history(self) -> None:
        """Keep exactly the completed focuses that can precede the next one."""
        limit = self._cycle_history_limit()
        if limit == 0:
            self._recent_cycle_focus_intervals.clear()
        else:
            self._recent_cycle_focus_intervals = (
                self._recent_cycle_focus_intervals[-limit:]
            )

    def _set_idle(self) -> None:
        next_goal = self._snapshot.next_goal
        self._snapshot = RuntimeSnapshot(
            status=TimerStatus.IDLE,
            remaining_ms=0,
            completed_in_cycle=len(self._recent_cycle_focus_intervals),
            break_kind=None,
            phase_total_ms=0,
            break_deadline_wall_ms=None,
            focus_started_at_wall_ms=None,
            long_break_recommended=False,
            next_goal=next_goal,
        )

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
            recent_cycle_focus_intervals=self._recent_cycle_focus_intervals,
        )
        if finalize_current_focus or discard_current_focus:
            self._pending_focus.clear()

    def _recommend_long_break_after_qualified_focus(
        self,
        completion_wall_ms: int,
    ) -> bool:
        """Update completed-focus history and evaluate the rolling gap rule."""
        focus_started_at_wall_ms = self._snapshot.focus_started_at_wall_ms
        if (
            focus_started_at_wall_ms is None
            or focus_started_at_wall_ms > completion_wall_ms
        ):
            self._recent_cycle_focus_intervals.clear()
            return False

        if (
            self._recent_cycle_focus_intervals
            and focus_started_at_wall_ms
            < self._recent_cycle_focus_intervals[-1][1]
        ):
            # A backwards wall-clock correction must not manufacture a
            # negative break interval that looks like uninterrupted work.
            self._recent_cycle_focus_intervals.clear()

        candidates = [
            *self._recent_cycle_focus_intervals,
            (focus_started_at_wall_ms, completion_wall_ms),
        ]
        required_sessions = self.config.sessions_before_long_break
        recommendation = False
        if len(candidates) >= required_sessions:
            recent = candidates[-required_sessions:]
            gaps = [
                current_start_ms - previous_completion_ms
                for (_, previous_completion_ms), (current_start_ms, _)
                in zip(recent, recent[1:])
            ]
            recommendation = (
                all(gap_ms >= 0 for gap_ms in gaps)
                and sum(gaps) <= CONTINUOUS_BREAK_WINDOW_MS
            )

        limit = self._cycle_history_limit()
        self._recent_cycle_focus_intervals = (
            candidates[-limit:] if limit else []
        )
        return recommendation

    def _complete_focus(
        self,
        completion_wall_ms: int,
    ) -> tuple[TimerEvent | None, str | None]:
        started_at_wall_ms = self._snapshot.focus_started_at_wall_ms
        session_id = self._snapshot.task_session_id
        focus_goal = self._snapshot.focus_goal
        focus_ms = self._focus_elapsed_ms()
        if focus_ms < MINIMUM_RECORDED_FOCUS_MS:
            self._recent_cycle_focus_intervals.clear()
            self._snapshot.next_goal = self._snapshot.focus_goal
            self._set_idle()
            return None, None

        completion_day = local_day(completion_wall_ms)
        recommendation = self._recommend_long_break_after_qualified_focus(
            completion_wall_ms
        )
        break_seconds = self.config.short_break_seconds

        self._snapshot = RuntimeSnapshot(
            status=TimerStatus.BREAK_READY,
            remaining_ms=break_seconds * 1000,
            completed_in_cycle=len(self._recent_cycle_focus_intervals),
            break_kind=None,
            phase_total_ms=break_seconds * 1000,
            break_deadline_wall_ms=None,
            focus_started_at_wall_ms=None,
            long_break_recommended=recommendation,
            focus_goal=focus_goal,
            task_session_id=session_id,
            break_task=None,
            break_task_outcome=TaskOutcome.UNRECORDED,
            break_task_confirmed=False,
        )
        if session_id:
            self.store.create_task_record(
                session_id, focus_goal, started_at_wall_ms, focus_ms
            )
        return TimerEvent.FOCUS_COMPLETED, completion_day

    def _focus_elapsed_ms(self) -> int:
        return max(0, self._snapshot.phase_total_ms - self._snapshot.remaining_ms)

    def _prepare_break_for_early_focus_end(
        self,
        actual_focus_ms: int,
    ) -> None:
        started_at_wall_ms = self._snapshot.focus_started_at_wall_ms
        session_id = self._snapshot.task_session_id
        focus_goal = self._snapshot.focus_goal
        break_ms = actual_focus_ms // 5
        self._recent_cycle_focus_intervals.clear()
        self._snapshot = RuntimeSnapshot(
            status=TimerStatus.BREAK_READY,
            remaining_ms=break_ms,
            completed_in_cycle=0,
            break_kind=None,
            phase_total_ms=break_ms,
            break_deadline_wall_ms=None,
            focus_started_at_wall_ms=None,
            long_break_recommended=False,
            focus_goal=focus_goal,
            task_session_id=session_id,
        )
        if session_id:
            self.store.create_task_record(
                session_id, focus_goal, started_at_wall_ms, actual_focus_ms
            )

    def _complete_break(self) -> TimerEvent:
        if (
            self._snapshot.break_task
            and self._snapshot.task_session_id
            and self._snapshot.break_task_outcome is TaskOutcome.UNRECORDED
        ):
            self._snapshot.break_task_outcome = TaskOutcome.COMPLETED
            self.store.update_task_record(
                self._snapshot.task_session_id,
                task_text=self._snapshot.break_task,
                outcome=TaskOutcome.COMPLETED,
                completed_at_wall_ms=self.clock.wall_epoch_ms(),
            )
        self._set_idle()
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
        finalize_current_focus = False
        discard_current_focus = False

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
                    if event is None:
                        discard_current_focus = True
                    else:
                        assert completion_day is not None
                        events.append(event)
                        completion_days.append(completion_day)
                        finalize_current_focus = True

        if self._snapshot.status is TimerStatus.BREAK_RUNNING:
            if self._sync_break_to_wall(now_wall_ms):
                events.append(TimerEvent.BREAK_COMPLETED)

        self._anchor_monotonic_ms = now_monotonic_ms
        self._anchor_wall_ms = now_wall_ms

        if events or finalize_current_focus or discard_current_focus:
            self._commit(
                completion_days,
                finalize_current_focus=finalize_current_focus,
                discard_current_focus=discard_current_focus,
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

    def start_focus(
        self, duration_seconds: int | None = None, goal: str | None = None
    ) -> None:
        if self._snapshot.status is not TimerStatus.IDLE:
            return
        focus_seconds = (
            self.config.focus_seconds
            if duration_seconds is None
            else int(duration_seconds)
        )
        if focus_seconds <= 0:
            raise ValueError("focus duration must be positive")
        focus_started_at_wall_ms = self.clock.wall_epoch_ms()
        normalized_goal = (goal if goal is not None else self._snapshot.next_goal) or None
        if normalized_goal is not None:
            normalized_goal = normalized_goal[:20]
        self._snapshot = RuntimeSnapshot(
            status=TimerStatus.FOCUS_RUNNING,
            remaining_ms=focus_seconds * 1000,
            completed_in_cycle=len(self._recent_cycle_focus_intervals),
            break_kind=None,
            phase_total_ms=focus_seconds * 1000,
            break_deadline_wall_ms=None,
            focus_started_at_wall_ms=focus_started_at_wall_ms,
            focus_goal=normalized_goal,
            task_session_id=str(uuid4()),
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
            if actual_focus_ms < MINIMUM_RECORDED_FOCUS_MS:
                self._recent_cycle_focus_intervals.clear()
                self._set_idle()
                self._commit(discard_current_focus=True)
            else:
                self._prepare_break_for_early_focus_end(actual_focus_ms)
                events.append(TimerEvent.FOCUS_ENDED_EARLY)
                self._commit(finalize_current_focus=True)
        return events

    def start_break(self) -> bool:
        if self._snapshot.status is not TimerStatus.BREAK_READY:
            return False
        now_wall_ms = self.clock.wall_epoch_ms()
        self._snapshot.status = TimerStatus.BREAK_RUNNING
        self._snapshot.break_kind = BreakKind.SHORT
        self._snapshot.break_deadline_wall_ms = (
            now_wall_ms + self._snapshot.remaining_ms
        )
        self._commit()
        return True

    @property
    def task_goal(self) -> str | None:
        return self._snapshot.focus_goal

    @property
    def next_goal(self) -> str | None:
        return self._snapshot.next_goal

    def set_next_goal(self, goal: str | None) -> None:
        self._snapshot.next_goal = (goal or None)
        self._commit()

    def confirm_break_task(
        self,
        task_text: str | None,
        outcome: TaskOutcome = TaskOutcome.COMPLETED,
    ) -> bool:
        if not self._snapshot.status.is_break or not self._snapshot.task_session_id:
            return False
        text = (task_text or "").strip()[:20] or None
        self._snapshot.break_task = text
        has_goal = bool(self._snapshot.focus_goal)
        self._snapshot.break_task_outcome = (
            outcome if (text or has_goal) else TaskOutcome.UNRECORDED
        )
        self._snapshot.break_task_confirmed = bool(text or has_goal)
        if text or has_goal:
            if outcome is TaskOutcome.INCOMPLETE and self._snapshot.focus_goal:
                self._snapshot.next_goal = self._snapshot.focus_goal
            elif outcome is TaskOutcome.COMPLETED:
                self._snapshot.next_goal = None
            self.store.update_task_record(
                self._snapshot.task_session_id,
                task_text=text,
                outcome=self._snapshot.break_task_outcome,
                completed_at_wall_ms=self.clock.wall_epoch_ms(),
            )
        self._commit()
        return True

    def set_break_task_draft(self, task_text: str | None) -> bool:
        if not self._snapshot.status.is_break:
            return False
        self._snapshot.break_task = (task_text or "")[:20] or None
        self._snapshot.break_task_outcome = TaskOutcome.UNRECORDED
        self._snapshot.break_task_confirmed = False
        self._commit()
        return True

    def clear_break_task(self) -> bool:
        if not self._snapshot.status.is_break:
            return False
        self._snapshot.break_task = None
        self._snapshot.break_task_outcome = TaskOutcome.UNRECORDED
        self._snapshot.break_task_confirmed = False
        self._commit()
        return True

    def skip_break(self) -> bool:
        if self._snapshot.status is not TimerStatus.BREAK_READY:
            return False
        self._set_idle()
        self._commit()
        return True

    def adjust_break_minutes(self, adjustment: int) -> list[TimerEvent]:
        events = self._advance()
        if self._snapshot.status is not TimerStatus.BREAK_RUNNING:
            return events
        delta_ms = int(adjustment) * 60_000
        if delta_ms == 0:
            return events
        remaining_ms = self._snapshot.remaining_ms + delta_ms
        phase_total_ms = self._snapshot.phase_total_ms + delta_ms
        if (
            remaining_ms <= 0
            or phase_total_ms <= 0
            or phase_total_ms > MAXIMUM_BREAK_MS
        ):
            return events
        deadline_ms = self._snapshot.break_deadline_wall_ms
        if deadline_ms is None:
            deadline_ms = self.clock.wall_epoch_ms() + self._snapshot.remaining_ms
        self._snapshot.remaining_ms = remaining_ms
        self._snapshot.phase_total_ms = phase_total_ms
        self._snapshot.break_deadline_wall_ms = deadline_ms + delta_ms
        self._commit()
        return events

    def end_break(self) -> list[TimerEvent]:
        events = self._advance()
        if self._snapshot.status.is_break:
            self._set_idle()
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
            if self._focus_elapsed_ms() >= MINIMUM_RECORDED_FOCUS_MS
            else 0
        )
        return DailyStats(
            focus_ms=persisted.focus_ms + provisional_focus_ms,
            completed_sessions=persisted.completed_sessions,
        )
