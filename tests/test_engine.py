from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from focus_tomato.config import TimerConfig
from focus_tomato.engine import TimerEngine
from focus_tomato.models import BreakKind, RuntimeSnapshot, TimerEvent, TimerStatus
from focus_tomato.storage import SQLiteStore


class FakeClock:
    """A deterministic clock whose monotonic and wall times can move separately."""

    def __init__(self, wall_ms: int = 0, monotonic_ms: int = 0) -> None:
        self._wall_ms = wall_ms
        self._monotonic_ms = monotonic_ms

    def monotonic_ms(self) -> int:
        return self._monotonic_ms

    def wall_epoch_ms(self) -> int:
        return self._wall_ms

    def advance(self, elapsed_ms: int, *, wall_elapsed_ms: int | None = None) -> None:
        if elapsed_ms < 0:
            raise ValueError("elapsed_ms must be non-negative")
        self._monotonic_ms += elapsed_ms
        self._wall_ms += elapsed_ms if wall_elapsed_ms is None else wall_elapsed_ms

    def adjust_wall(self, delta_ms: int) -> None:
        self._wall_ms += delta_ms


def local_epoch_ms(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> int:
    # The production code intentionally uses the host's local timezone. A naive
    # datetime goes through that same timezone, keeping this test portable.
    return int(datetime(year, month, day, hour, minute, second).timestamp() * 1000)


class TimerEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SQLiteStore(":memory:")
        self.addCleanup(self.store.close)
        self.clock = FakeClock(wall_ms=local_epoch_ms(2026, 1, 15, 9, 0))
        self.config = TimerConfig(
            focus_seconds=60,
            short_break_seconds=10,
            long_break_seconds=30,
            sessions_before_long_break=4,
            countdown_visible_seconds=5,
            autostart=True,
        )
        self.engine = TimerEngine(self.store, self.config, self.clock)

    def test_early_end_under_one_minute_discards_active_focus(self) -> None:
        self.engine.start_focus()
        self.assertEqual(self.engine.snapshot.status, TimerStatus.FOCUS_RUNNING)
        self.assertEqual(self.engine.snapshot.remaining_ms, 60_000)

        self.clock.advance(10_000)
        self.assertEqual(self.engine.pause_focus(), [])
        self.assertEqual(self.engine.snapshot.status, TimerStatus.FOCUS_PAUSED)
        self.assertEqual(self.engine.snapshot.remaining_ms, 50_000)

        # Time spent paused must affect neither the deadline nor daily focus.
        self.clock.advance(5 * 60_000)
        self.assertEqual(self.engine.tick(), [])
        self.assertEqual(self.engine.snapshot.remaining_ms, 50_000)

        self.engine.resume()
        self.clock.advance(15_000)
        self.assertEqual(self.engine.end_focus(), [])
        self.assertEqual(self.engine.snapshot.status, TimerStatus.IDLE)

        stats = self.engine.today_stats()
        self.assertEqual(stats.focus_ms, 0)
        self.assertEqual(stats.completed_sessions, 0)
        self.assertEqual(self.store.load_current_focus_pending(), {})

    def test_early_end_at_one_minute_records_focus_and_starts_short_break(self) -> None:
        self.engine.start_focus(duration_seconds=120)
        self.clock.advance(60_000)

        self.assertEqual(
            self.engine.end_focus(),
            [TimerEvent.FOCUS_ENDED_EARLY],
        )

        snapshot = self.engine.snapshot
        self.assertEqual(snapshot.status, TimerStatus.BREAK_RUNNING)
        self.assertEqual(snapshot.remaining_ms, 12_000)
        self.assertEqual(snapshot.phase_total_ms, 12_000)
        self.assertEqual(snapshot.break_kind, BreakKind.SHORT)
        self.assertEqual(snapshot.completed_in_cycle, 0)
        self.assertEqual(
            snapshot.break_deadline_wall_ms,
            self.clock.wall_epoch_ms() + 12_000,
        )
        self.assertEqual(self.engine.today_stats().focus_ms, 60_000)
        self.assertEqual(self.engine.today_stats().completed_sessions, 0)
        self.assertEqual(self.store.load_current_focus_pending(), {})
        self.assertEqual(self.store.load_runtime(), snapshot)

    def test_early_end_break_duration_rounds_down_to_whole_milliseconds(self) -> None:
        self.engine.start_focus(duration_seconds=120)
        self.clock.advance(60_001)

        self.assertEqual(
            self.engine.end_focus(),
            [TimerEvent.FOCUS_ENDED_EARLY],
        )
        self.assertEqual(self.engine.snapshot.remaining_ms, 12_000)
        self.assertEqual(self.engine.today_stats().focus_ms, 60_001)

    def test_early_end_excludes_paused_time_after_crossing_one_minute(self) -> None:
        self.engine.start_focus(duration_seconds=120)
        self.clock.advance(40_000)
        self.assertEqual(self.engine.pause_focus(), [])

        self.clock.advance(60 * 60_000)
        self.assertEqual(self.engine.tick(), [])
        self.engine.resume()
        self.clock.advance(25_000)

        self.assertEqual(
            self.engine.end_focus(),
            [TimerEvent.FOCUS_ENDED_EARLY],
        )
        self.assertEqual(self.engine.snapshot.remaining_ms, 13_000)
        self.assertEqual(self.engine.today_stats().focus_ms, 65_000)
        self.assertEqual(self.engine.today_stats().completed_sessions, 0)

    def test_early_end_preserves_the_long_break_cycle(self) -> None:
        for _ in range(3):
            self.engine.start_focus()
            self.clock.advance(60_000)
            self.assertEqual(self.engine.tick(), [TimerEvent.FOCUS_COMPLETED])
            self.assertEqual(self.engine.end_break(), [])

        self.assertEqual(self.engine.snapshot.completed_in_cycle, 3)
        self.engine.start_focus(duration_seconds=120)
        self.clock.advance(60_000)

        self.assertEqual(
            self.engine.end_focus(),
            [TimerEvent.FOCUS_ENDED_EARLY],
        )
        self.assertEqual(self.engine.snapshot.break_kind, BreakKind.SHORT)
        self.assertEqual(self.engine.snapshot.completed_in_cycle, 3)
        self.assertEqual(self.engine.today_stats().completed_sessions, 3)

    def test_early_end_across_midnight_finalizes_each_day_without_completion(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        clock = FakeClock(wall_ms=local_epoch_ms(2026, 3, 7, 23, 59, 30))
        engine = TimerEngine(store, self.config, clock)

        engine.start_focus(duration_seconds=120)
        clock.advance(30_000)
        self.assertEqual(engine.checkpoint(), [])
        clock.advance(30_000)

        self.assertEqual(engine.end_focus(), [TimerEvent.FOCUS_ENDED_EARLY])
        first_day = store.stats_for_day("2026-03-07")
        second_day = store.stats_for_day("2026-03-08")
        self.assertEqual(first_day.focus_ms, 30_000)
        self.assertEqual(first_day.completed_sessions, 0)
        self.assertEqual(second_day.focus_ms, 30_000)
        self.assertEqual(second_day.completed_sessions, 0)
        self.assertEqual(store.load_current_focus_pending(), {})

    def test_checkpointed_subminute_focus_is_discarded_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "timer.db"
            first_store = SQLiteStore(database)
            first_clock = FakeClock(wall_ms=local_epoch_ms(2026, 2, 1, 8, 0))
            first_engine = TimerEngine(first_store, self.config, first_clock)
            first_engine.start_focus(duration_seconds=120)
            first_clock.advance(30_000)
            self.assertEqual(first_engine.checkpoint(), [])
            self.assertEqual(first_store.stats_for_day("2026-02-01").focus_ms, 0)
            self.assertEqual(
                first_store.load_current_focus_pending(),
                {"2026-02-01": 30_000},
            )
            first_store.close()

            second_store = SQLiteStore(database)
            self.addCleanup(second_store.close)
            recovered = TimerEngine(
                second_store,
                self.config,
                FakeClock(wall_ms=local_epoch_ms(2026, 2, 1, 12, 0)),
            )

            self.assertTrue(recovered.recovered)
            self.assertEqual(recovered.end_focus(), [])
            self.assertEqual(recovered.snapshot.status, TimerStatus.IDLE)
            self.assertEqual(second_store.stats_for_day("2026-02-01").focus_ms, 0)
            self.assertEqual(second_store.load_current_focus_pending(), {})

    def test_checkpointed_fifty_nine_seconds_remains_unrecorded_when_ended(self) -> None:
        self.engine.start_focus(duration_seconds=120)
        self.clock.advance(55_000)
        self.assertEqual(self.engine.checkpoint(), [])
        self.assertEqual(self.store.stats_for_day("2026-01-15").focus_ms, 0)
        self.assertEqual(self.engine.today_stats().focus_ms, 0)

        self.clock.advance(4_000)
        self.assertEqual(self.engine.end_focus(), [])
        self.assertEqual(self.engine.snapshot.status, TimerStatus.IDLE)
        self.assertEqual(self.store.stats_for_day("2026-01-15").focus_ms, 0)
        self.assertEqual(self.engine.today_stats().focus_ms, 0)
        self.assertEqual(self.store.load_current_focus_pending(), {})

    def test_checkpointed_focus_across_restart_finalizes_once_after_one_minute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "timer.db"
            first_store = SQLiteStore(database)
            first_clock = FakeClock(wall_ms=local_epoch_ms(2026, 2, 1, 8, 0))
            first_engine = TimerEngine(first_store, self.config, first_clock)
            first_engine.start_focus(duration_seconds=120)
            first_clock.advance(55_000)
            first_engine.checkpoint()
            first_store.close()

            second_store = SQLiteStore(database)
            self.addCleanup(second_store.close)
            second_clock = FakeClock(wall_ms=local_epoch_ms(2026, 2, 1, 12, 0))
            recovered = TimerEngine(second_store, self.config, second_clock)
            self.assertTrue(recovered.recovered)
            recovered.resume()
            second_clock.advance(10_000)

            self.assertEqual(
                recovered.end_focus(),
                [TimerEvent.FOCUS_ENDED_EARLY],
            )
            snapshot = recovered.snapshot
            self.assertEqual(snapshot.status, TimerStatus.BREAK_RUNNING)
            self.assertEqual(snapshot.remaining_ms, 13_000)
            self.assertEqual(snapshot.completed_in_cycle, 0)
            self.assertEqual(second_store.stats_for_day("2026-02-01").focus_ms, 65_000)
            self.assertEqual(
                second_store.stats_for_day("2026-02-01").completed_sessions,
                0,
            )
            self.assertEqual(second_store.load_current_focus_pending(), {})

    def test_completed_focus_counts_once_and_tracks_full_duration(self) -> None:
        self.engine.start_focus()
        self.clock.advance(60_000)

        self.assertEqual(self.engine.tick(), [TimerEvent.FOCUS_COMPLETED])
        self.assertEqual(self.engine.snapshot.status, TimerStatus.BREAK_RUNNING)
        self.assertEqual(self.engine.snapshot.break_kind, BreakKind.SHORT)

        stats = self.engine.today_stats()
        self.assertEqual(stats.focus_ms, 60_000)
        self.assertEqual(stats.completed_sessions, 1)

        # Re-rendering/ticking at the same instant must not double count.
        self.assertEqual(self.engine.tick(), [])
        stats = self.engine.today_stats()
        self.assertEqual(stats.focus_ms, 60_000)
        self.assertEqual(stats.completed_sessions, 1)

    def test_custom_focus_duration_is_used_and_persisted(self) -> None:
        self.engine.start_focus(duration_seconds=37 * 60)

        snapshot = self.engine.snapshot
        self.assertEqual(snapshot.remaining_ms, 37 * 60_000)
        self.assertEqual(snapshot.phase_total_ms, 37 * 60_000)
        self.assertEqual(self.store.load_runtime().phase_total_ms, 37 * 60_000)

        self.clock.advance(12_000)
        self.engine.pause_focus()
        paused = self.engine.snapshot
        self.assertEqual(paused.remaining_ms, 37 * 60_000 - 12_000)
        self.assertEqual(paused.phase_total_ms, 37 * 60_000)

    def test_custom_focus_duration_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            self.engine.start_focus(duration_seconds=0)
        self.assertEqual(self.engine.snapshot.status, TimerStatus.IDLE)

    def test_irregular_ticks_do_not_accumulate_deadline_drift(self) -> None:
        self.engine.start_focus()

        for elapsed_ms in (333, 1_777, 4_321, 9_999, 2_570, 11_000):
            self.clock.advance(elapsed_ms)
            self.assertEqual(self.engine.tick(), [])

        elapsed_total = 333 + 1_777 + 4_321 + 9_999 + 2_570 + 11_000
        self.assertEqual(self.engine.snapshot.remaining_ms, 60_000 - elapsed_total)

        self.clock.advance(60_000 - elapsed_total - 1)
        self.assertEqual(self.engine.tick(), [])
        self.assertEqual(self.engine.snapshot.remaining_ms, 1)

        self.clock.advance(1)
        self.assertEqual(self.engine.tick(), [TimerEvent.FOCUS_COMPLETED])
        self.assertEqual(self.engine.today_stats().focus_ms, 60_000)

    def test_wall_clock_corrections_do_not_change_timer_deadline(self) -> None:
        self.engine.start_focus()

        # NTP/user clock corrections alter wall time but not monotonic elapsed.
        self.clock.advance(12_000, wall_elapsed_ms=3 * 60 * 60_000)
        self.assertEqual(self.engine.tick(), [])
        self.assertEqual(self.engine.snapshot.remaining_ms, 48_000)

        self.clock.adjust_wall(-6 * 60 * 60_000)
        self.clock.advance(20_000, wall_elapsed_ms=0)
        self.assertEqual(self.engine.tick(), [])
        self.assertEqual(self.engine.snapshot.remaining_ms, 28_000)

        self.clock.advance(28_000, wall_elapsed_ms=1_000)
        self.assertEqual(self.engine.tick(), [TimerEvent.FOCUS_COMPLETED])

    def test_first_three_sessions_use_short_break_and_fourth_uses_long_break(self) -> None:
        for session_number in range(1, 5):
            with self.subTest(session=session_number):
                self.engine.start_focus()
                self.clock.advance(60_000)
                self.assertEqual(self.engine.tick(), [TimerEvent.FOCUS_COMPLETED])

                snapshot = self.engine.snapshot
                expected_kind = BreakKind.LONG if session_number == 4 else BreakKind.SHORT
                expected_duration = 30_000 if session_number == 4 else 10_000
                expected_cycle = 0 if session_number == 4 else session_number
                self.assertEqual(snapshot.status, TimerStatus.BREAK_RUNNING)
                self.assertEqual(snapshot.break_kind, expected_kind)
                self.assertEqual(snapshot.remaining_ms, expected_duration)
                self.assertEqual(snapshot.completed_in_cycle, expected_cycle)

                self.clock.advance(expected_duration)
                self.assertEqual(self.engine.tick(), [TimerEvent.BREAK_COMPLETED])
                self.assertEqual(self.engine.snapshot.status, TimerStatus.IDLE)
                self.assertIsNone(self.engine.snapshot.break_kind)

        stats = self.engine.today_stats()
        self.assertEqual(stats.completed_sessions, 4)
        self.assertEqual(stats.focus_ms, 4 * 60_000)

    def test_elapsed_time_can_complete_focus_and_break_in_one_tick(self) -> None:
        self.engine.start_focus()
        self.clock.advance(70_000)

        self.assertEqual(
            self.engine.tick(),
            [TimerEvent.FOCUS_COMPLETED, TimerEvent.BREAK_COMPLETED],
        )
        self.assertEqual(self.engine.snapshot.status, TimerStatus.IDLE)
        self.assertEqual(self.engine.today_stats().completed_sessions, 1)

    def test_ending_break_returns_to_idle_without_changing_focus_stats(self) -> None:
        self.engine.start_focus()
        self.clock.advance(60_000)
        self.engine.tick()
        before = self.engine.today_stats()

        self.clock.advance(4_000)
        self.assertEqual(self.engine.end_break(), [])
        self.assertEqual(self.engine.snapshot.status, TimerStatus.IDLE)
        self.assertEqual(self.engine.today_stats(), before)

    def test_sleep_pauses_focus_until_explicit_resume(self) -> None:
        self.engine.start_focus()
        self.clock.advance(17_000)
        self.assertEqual(self.engine.pause_for_sleep(), [])

        self.assertEqual(self.engine.snapshot.status, TimerStatus.FOCUS_PAUSED)
        self.assertEqual(self.engine.snapshot.remaining_ms, 43_000)
        self.assertEqual(self.engine.today_stats().focus_ms, 0)

        self.clock.advance(8 * 60 * 60_000)
        self.assertEqual(self.engine.tick(), [])
        self.assertEqual(self.engine.snapshot.remaining_ms, 43_000)
        self.assertEqual(self.engine.today_stats().focus_ms, 0)

        self.engine.resume()
        self.clock.advance(43_000)
        self.assertEqual(self.engine.tick(), [TimerEvent.FOCUS_COMPLETED])
        self.assertEqual(self.engine.today_stats().focus_ms, 60_000)

    def test_sleep_does_not_pause_an_active_break(self) -> None:
        self.engine.start_focus()
        self.clock.advance(60_000)
        self.engine.tick()
        self.clock.advance(4_000)

        self.assertEqual(self.engine.pause_for_sleep(), [])
        self.assertEqual(self.engine.snapshot.status, TimerStatus.BREAK_RUNNING)
        self.assertEqual(self.engine.snapshot.remaining_ms, 6_000)

        # Simulate suspend: Linux's monotonic clock need not include it, but
        # the persisted break deadline must still elapse by wall time.
        self.clock.advance(0, wall_elapsed_ms=60 * 60_000)
        self.assertEqual(self.engine.tick(), [TimerEvent.BREAK_COMPLETED])
        self.assertEqual(self.engine.snapshot.status, TimerStatus.IDLE)

    def test_running_focus_is_recovered_as_paused_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "timer.db"
            first_store = SQLiteStore(database)
            first_clock = FakeClock(wall_ms=local_epoch_ms(2026, 2, 1, 8, 0))
            first_engine = TimerEngine(first_store, self.config, first_clock)
            first_engine.start_focus()
            first_clock.advance(19_000)
            first_engine.checkpoint()
            first_store.close()

            second_store = SQLiteStore(database)
            self.addCleanup(second_store.close)
            second_clock = FakeClock(
                wall_ms=local_epoch_ms(2026, 2, 1, 12, 0),
                monotonic_ms=900_000,
            )
            recovered = TimerEngine(second_store, self.config, second_clock)

            self.assertTrue(recovered.recovered)
            self.assertEqual(recovered.snapshot.status, TimerStatus.FOCUS_PAUSED)
            self.assertEqual(recovered.snapshot.remaining_ms, 41_000)
            self.assertEqual(second_store.stats_for_day("2026-02-01").focus_ms, 0)
            self.assertEqual(
                second_store.load_current_focus_pending(),
                {"2026-02-01": 19_000},
            )
            self.assertEqual(recovered.today_stats().focus_ms, 0)

            second_clock.advance(2 * 60 * 60_000)
            recovered.tick()
            self.assertEqual(recovered.snapshot.remaining_ms, 41_000)

    def test_running_break_continues_after_restart_by_wall_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "timer.db"
            first_store = SQLiteStore(database)
            first_wall_ms = local_epoch_ms(2026, 2, 1, 8, 0)
            first_store.commit(
                RuntimeSnapshot(
                    status=TimerStatus.BREAK_RUNNING,
                    remaining_ms=7_000,
                    completed_in_cycle=2,
                    break_kind=BreakKind.SHORT,
                    phase_total_ms=10_000,
                    break_deadline_wall_ms=first_wall_ms + 10_000,
                ),
                updated_at_wall_ms=first_wall_ms,
            )
            first_store.close()

            second_store = SQLiteStore(database)
            self.addCleanup(second_store.close)
            second_clock = FakeClock(wall_ms=first_wall_ms + 3_000)
            recovered = TimerEngine(second_store, self.config, second_clock)

            self.assertFalse(recovered.recovered)
            self.assertEqual(recovered.snapshot.status, TimerStatus.BREAK_RUNNING)
            self.assertEqual(recovered.snapshot.remaining_ms, 7_000)
            self.assertEqual(recovered.snapshot.completed_in_cycle, 2)
            self.assertEqual(recovered.snapshot.break_kind, BreakKind.SHORT)
            self.assertEqual(recovered.snapshot.phase_total_ms, 10_000)
            self.assertEqual(
                recovered.snapshot.break_deadline_wall_ms,
                first_wall_ms + 10_000,
            )

            second_clock.advance(0, wall_elapsed_ms=7_000)
            self.assertEqual(recovered.tick(), [TimerEvent.BREAK_COMPLETED])
            self.assertEqual(recovered.snapshot.status, TimerStatus.IDLE)

    def test_expired_break_becomes_idle_and_reports_startup_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "timer.db"
            first_store = SQLiteStore(database)
            first_wall_ms = local_epoch_ms(2026, 2, 1, 8, 0)
            first_store.commit(
                RuntimeSnapshot(
                    status=TimerStatus.BREAK_RUNNING,
                    remaining_ms=7_000,
                    completed_in_cycle=2,
                    break_kind=BreakKind.SHORT,
                    phase_total_ms=10_000,
                    break_deadline_wall_ms=first_wall_ms + 7_000,
                ),
                updated_at_wall_ms=first_wall_ms,
            )
            first_store.close()

            second_store = SQLiteStore(database)
            self.addCleanup(second_store.close)
            recovered = TimerEngine(
                second_store,
                self.config,
                FakeClock(wall_ms=first_wall_ms + 8_000),
            )

            self.assertFalse(recovered.recovered)
            self.assertEqual(recovered.snapshot.status, TimerStatus.IDLE)
            self.assertEqual(recovered.snapshot.completed_in_cycle, 2)
            self.assertEqual(recovered.startup_events, [TimerEvent.BREAK_COMPLETED])

    def test_legacy_paused_break_resumes_from_its_saved_remaining_time(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        store.connection.execute(
            """
            UPDATE runtime_state
            SET status = 'break_paused', remaining_ms = 7000,
                completed_in_cycle = 2, break_kind = 'short',
                phase_total_ms = 10000
            WHERE singleton_id = 1
            """
        )
        store.connection.commit()
        now_wall_ms = local_epoch_ms(2026, 2, 1, 8, 0)

        migrated = TimerEngine(store, self.config, FakeClock(wall_ms=now_wall_ms))

        self.assertFalse(migrated.recovered)
        self.assertEqual(migrated.snapshot.status, TimerStatus.BREAK_RUNNING)
        self.assertEqual(migrated.snapshot.remaining_ms, 7_000)
        self.assertEqual(
            migrated.snapshot.break_deadline_wall_ms,
            now_wall_ms + 7_000,
        )
        row = store.connection.execute(
            "SELECT status, break_deadline_wall_ms FROM runtime_state "
            "WHERE singleton_id = 1"
        ).fetchone()
        self.assertEqual(row, (TimerStatus.BREAK_RUNNING.value, now_wall_ms + 7_000))

    def test_focus_across_midnight_splits_duration_and_counts_completion_on_end_day(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        clock = FakeClock(wall_ms=local_epoch_ms(2026, 3, 7, 23, 59, 50))
        config = TimerConfig(
            focus_seconds=20,
            short_break_seconds=5,
            long_break_seconds=10,
            sessions_before_long_break=4,
            countdown_visible_seconds=5,
        )
        engine = TimerEngine(store, config, clock)

        engine.start_focus()
        clock.advance(20_000)
        self.assertEqual(engine.tick(), [TimerEvent.FOCUS_COMPLETED])

        first_day = store.stats_for_day("2026-03-07")
        second_day = store.stats_for_day("2026-03-08")
        self.assertEqual(first_day.focus_ms, 10_000)
        self.assertEqual(first_day.completed_sessions, 0)
        self.assertEqual(second_day.focus_ms, 10_000)
        self.assertEqual(second_day.completed_sessions, 1)


if __name__ == "__main__":
    unittest.main()
