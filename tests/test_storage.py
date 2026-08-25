from __future__ import annotations

import tempfile
import unittest
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from focus_tomato.models import BreakKind, RuntimeSnapshot, TimerStatus
from focus_tomato.storage import SQLiteStore


class SQLiteStoreTest(unittest.TestCase):
    def test_runtime_and_daily_stats_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "focus-tomato.db"
            first = SQLiteStore(database)
            first.commit(
                RuntimeSnapshot(
                    status=TimerStatus.BREAK_RUNNING,
                    remaining_ms=12_345,
                    completed_in_cycle=3,
                    break_kind=BreakKind.LONG,
                    phase_total_ms=30_000,
                    break_deadline_wall_ms=1_800_000_000_000,
                    focus_started_at_wall_ms=None,
                    long_break_recommended=True,
                ),
                {"2026-05-01": 61_000, "2026-05-02": 2_000},
                ("2026-05-01", "2026-05-01"),
                recent_cycle_focus_intervals=(
                    (1_799_999_000_000, 1_799_999_100_000),
                    (1_799_999_500_000, 1_799_999_600_000),
                    (1_799_999_750_000, 1_799_999_800_000),
                ),
            )
            first.close()

            second = SQLiteStore(database)
            self.addCleanup(second.close)
            self.assertEqual(
                second.load_runtime(),
                RuntimeSnapshot(
                    status=TimerStatus.BREAK_RUNNING,
                    remaining_ms=12_345,
                    completed_in_cycle=3,
                    break_kind=BreakKind.LONG,
                    phase_total_ms=30_000,
                    break_deadline_wall_ms=1_800_000_000_000,
                    focus_started_at_wall_ms=None,
                    long_break_recommended=True,
                ),
            )
            self.assertEqual(
                second.load_cycle_focus_intervals(),
                (
                    (1_799_999_000_000, 1_799_999_100_000),
                    (1_799_999_500_000, 1_799_999_600_000),
                    (1_799_999_750_000, 1_799_999_800_000),
                ),
            )
            self.assertEqual(second.stats_for_day("2026-05-01").focus_ms, 61_000)
            self.assertEqual(
                second.stats_for_day("2026-05-01").completed_sessions,
                2,
            )
            self.assertEqual(second.stats_for_day("2026-05-02").focus_ms, 2_000)

    def test_commits_accumulate_stats_and_ignore_nonpositive_allocations(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        snapshot = RuntimeSnapshot()

        store.commit(snapshot, {"2026-06-01": 1_500, "ignored": 0})
        store.commit(snapshot, {"2026-06-01": 2_500, "ignored": -10})

        self.assertEqual(store.stats_for_day("2026-06-01").focus_ms, 4_000)
        self.assertEqual(store.stats_for_day("ignored").focus_ms, 0)

    def test_current_focus_pending_stays_provisional_until_finalized(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        focus_snapshot = RuntimeSnapshot(
            status=TimerStatus.FOCUS_PAUSED,
            remaining_ms=90_000,
            phase_total_ms=120_000,
        )
        store.commit(
            focus_snapshot,
            current_focus_pending={"2026-06-01": 30_000},
        )

        self.assertEqual(store.stats_for_day("2026-06-01").focus_ms, 0)
        self.assertEqual(
            store.load_current_focus_pending(),
            {"2026-06-01": 30_000},
        )

        break_snapshot = RuntimeSnapshot(
            status=TimerStatus.BREAK_RUNNING,
            remaining_ms=12_000,
            break_kind=BreakKind.SHORT,
            phase_total_ms=12_000,
            break_deadline_wall_ms=112_000,
        )
        store.commit(
            break_snapshot,
            {"2026-06-01": 60_000},
            current_focus_pending={},
        )

        self.assertEqual(store.stats_for_day("2026-06-01").focus_ms, 60_000)
        self.assertEqual(store.load_current_focus_pending(), {})

    def test_load_runtime_normalizes_invalid_legacy_values(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        store.connection.execute(
            """
            UPDATE runtime_state
            SET status = 'not-a-status', remaining_ms = 5000,
                completed_in_cycle = 2, break_kind = 'not-a-break'
            WHERE singleton_id = 1
            """
        )
        store.connection.commit()

        self.assertEqual(store.load_runtime(), RuntimeSnapshot(completed_in_cycle=2))

    def test_break_without_kind_is_recovered_as_short_break(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        store.connection.execute(
            """
            UPDATE runtime_state
            SET status = ?, remaining_ms = 5000, break_kind = NULL
            WHERE singleton_id = 1
            """,
            ("break_paused",),
        )
        store.connection.commit()

        snapshot = store.load_runtime(now_wall_ms=100_000)
        self.assertEqual(snapshot.status, TimerStatus.BREAK_RUNNING)
        self.assertEqual(snapshot.break_kind, BreakKind.SHORT)
        self.assertEqual(snapshot.break_deadline_wall_ms, 105_000)
        # Read-only normalization leaves conversion to TimerEngine startup.
        raw_status = store.connection.execute(
            "SELECT status FROM runtime_state WHERE singleton_id = 1"
        ).fetchone()[0]
        self.assertEqual(raw_status, "break_paused")

    def test_legacy_running_break_derives_deadline_from_updated_at(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        saved_at_ms = 100_000
        updated_at = datetime.fromtimestamp(
            saved_at_ms / 1000,
            tz=timezone.utc,
        ).astimezone().isoformat()
        store.connection.execute(
            """
            UPDATE runtime_state
            SET status = 'break_running', remaining_ms = 5000,
                break_kind = 'short', phase_total_ms = 10000,
                break_deadline_wall_ms = NULL, updated_at = ?
            WHERE singleton_id = 1
            """,
            (updated_at,),
        )
        store.connection.commit()

        snapshot, needs_migration = store.load_runtime_with_metadata(103_000)

        self.assertTrue(needs_migration)
        self.assertEqual(snapshot.break_deadline_wall_ms, 105_000)
        self.assertEqual(snapshot.remaining_ms, 5_000)

    def test_existing_database_is_migrated_with_phase_total(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE runtime_state (
                    singleton_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    remaining_ms INTEGER NOT NULL,
                    completed_in_cycle INTEGER NOT NULL,
                    break_kind TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO runtime_state VALUES
                    (1, 'focus_paused', 42000, 0, NULL, 'legacy');
                CREATE TABLE daily_stats (
                    day TEXT PRIMARY KEY,
                    focus_ms INTEGER NOT NULL DEFAULT 0,
                    completed_sessions INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO daily_stats VALUES ('2026-06-01', 120000, 2);
                """
            )
            connection.close()

            store = SQLiteStore(database)
            self.addCleanup(store.close)
            snapshot = store.load_runtime()
            self.assertEqual(snapshot.remaining_ms, 42_000)
            self.assertEqual(snapshot.phase_total_ms, 42_000)
            self.assertIsNone(snapshot.focus_started_at_wall_ms)
            self.assertFalse(snapshot.long_break_recommended)
            self.assertEqual(store.load_cycle_focus_starts(), ())
            runtime_columns = {
                row[1]
                for row in store.connection.execute(
                    "PRAGMA table_info(runtime_state)"
                )
            }
            cycle_columns = {
                row[1]
                for row in store.connection.execute(
                    "PRAGMA table_info(recent_cycle_focus_starts)"
                )
            }
            self.assertIn("long_break_recommended", runtime_columns)
            self.assertIn("completed_at_wall_ms", cycle_columns)
            self.assertEqual(
                store.stats_for_day("2026-06-01").focus_ms,
                120_000,
            )
            self.assertEqual(
                store.stats_for_day("2026-06-01").completed_sessions,
                2,
            )

    def test_runtime_persists_active_focus_start_timestamp(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        snapshot = RuntimeSnapshot(
            status=TimerStatus.FOCUS_RUNNING,
            remaining_ms=1_400_000,
            phase_total_ms=1_500_000,
            focus_started_at_wall_ms=1_800_000_000_000,
        )

        store.commit(snapshot)

        self.assertEqual(store.load_runtime(), snapshot)

    def test_runtime_persists_pending_break_and_recommendation(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        snapshot = RuntimeSnapshot(
            status=TimerStatus.BREAK_READY,
            remaining_ms=5 * 60_000,
            completed_in_cycle=3,
            phase_total_ms=5 * 60_000,
            long_break_recommended=True,
        )

        store.commit(snapshot)

        self.assertEqual(store.load_runtime(), snapshot)

    def test_cycle_focus_starts_are_ordered_and_bounded_to_ninety_nine(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)

        store.commit(
            RuntimeSnapshot(),
            recent_cycle_focus_starts=tuple(range(100)),
        )

        self.assertEqual(store.load_cycle_focus_starts(), tuple(range(1, 100)))
        self.assertEqual(store.load_cycle_focus_intervals(), ())

    def test_cycle_focus_history_replacement_is_atomic_with_stats_and_runtime(self) -> None:
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        original = RuntimeSnapshot(
            status=TimerStatus.FOCUS_PAUSED,
            remaining_ms=20_000,
            phase_total_ms=60_000,
            focus_started_at_wall_ms=100,
        )
        store.commit(
            original,
            {"2026-06-01": 1_000},
            recent_cycle_focus_intervals=((100, 200),),
        )
        store.connection.execute(
            """
            CREATE TRIGGER reject_cycle_start
            BEFORE INSERT ON recent_cycle_focus_starts
            WHEN NEW.started_at_wall_ms = 400
            BEGIN
                SELECT RAISE(ABORT, 'test rollback');
            END
            """
        )
        store.connection.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "test rollback"):
            store.commit(
                RuntimeSnapshot(
                    status=TimerStatus.BREAK_RUNNING,
                    remaining_ms=10_000,
                    break_kind=BreakKind.SHORT,
                    phase_total_ms=10_000,
                    break_deadline_wall_ms=900,
                    long_break_recommended=True,
                ),
                {"2026-06-01": 2_000},
                recent_cycle_focus_intervals=((400, 500),),
            )

        self.assertEqual(store.load_runtime(), original)
        self.assertEqual(store.stats_for_day("2026-06-01").focus_ms, 1_000)
        self.assertEqual(store.load_cycle_focus_intervals(), ((100, 200),))


if __name__ == "__main__":
    unittest.main()
