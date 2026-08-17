from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from .models import BreakKind, DailyStats, RuntimeSnapshot, TimerStatus


APP_DIR_NAME = "focus-tomato"


def database_path() -> Path:
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / APP_DIR_NAME / "focus-tomato.db"


class SQLiteStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = path or database_path()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=5)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_state (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                status TEXT NOT NULL,
                remaining_ms INTEGER NOT NULL CHECK (remaining_ms >= 0),
                completed_in_cycle INTEGER NOT NULL CHECK (completed_in_cycle >= 0),
                break_kind TEXT,
                phase_total_ms INTEGER NOT NULL DEFAULT 0
                    CHECK (phase_total_ms >= 0),
                break_deadline_wall_ms INTEGER,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                day TEXT PRIMARY KEY,
                focus_ms INTEGER NOT NULL DEFAULT 0 CHECK (focus_ms >= 0),
                completed_sessions INTEGER NOT NULL DEFAULT 0
                    CHECK (completed_sessions >= 0)
            );

            CREATE TABLE IF NOT EXISTS current_focus_pending (
                singleton_id INTEGER NOT NULL DEFAULT 1
                    CHECK (singleton_id = 1),
                day TEXT NOT NULL,
                focus_ms INTEGER NOT NULL CHECK (focus_ms >= 0),
                PRIMARY KEY (singleton_id, day)
            );
            """
        )
        runtime_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(runtime_state)")
        }
        if "phase_total_ms" not in runtime_columns:
            self.connection.execute(
                "ALTER TABLE runtime_state "
                "ADD COLUMN phase_total_ms INTEGER NOT NULL DEFAULT 0"
            )
        if "break_deadline_wall_ms" not in runtime_columns:
            self.connection.execute(
                "ALTER TABLE runtime_state "
                "ADD COLUMN break_deadline_wall_ms INTEGER"
            )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO runtime_state (
                singleton_id, status, remaining_ms, completed_in_cycle,
                break_kind, phase_total_ms, break_deadline_wall_ms, updated_at
            ) VALUES (1, ?, 0, 0, NULL, 0, NULL, ?)
            """,
            (TimerStatus.IDLE.value, datetime.now().astimezone().isoformat()),
        )
        self.connection.commit()

    @staticmethod
    def _parse_updated_at_ms(value: object) -> int | None:
        if not isinstance(value, str):
            return None
        try:
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except (OverflowError, TypeError, ValueError):
            return None

    @staticmethod
    def _updated_at_value(wall_ms: int | None) -> str:
        if wall_ms is None:
            return datetime.now().astimezone().isoformat()
        return datetime.fromtimestamp(
            int(wall_ms) / 1000,
            tz=timezone.utc,
        ).astimezone().isoformat()

    def _read_runtime(
        self,
        now_wall_ms: int | None,
    ) -> tuple[RuntimeSnapshot, bool]:
        row = self.connection.execute(
            """
            SELECT status, remaining_ms, completed_in_cycle, break_kind,
                   phase_total_ms, break_deadline_wall_ms, updated_at
            FROM runtime_state WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None:
            return RuntimeSnapshot(), False

        raw_status = row[0]
        legacy_break_paused = raw_status == "break_paused"
        try:
            status = TimerStatus(raw_status)
        except (TypeError, ValueError):
            status = TimerStatus.BREAK_RUNNING if legacy_break_paused else TimerStatus.IDLE
        remaining_ms = max(0, int(row[1]))
        completed_in_cycle = max(0, int(row[2]))
        try:
            break_kind = BreakKind(row[3]) if row[3] else None
        except (TypeError, ValueError):
            break_kind = None
        phase_total_ms = max(0, int(row[4]))
        try:
            deadline_value = row[5]
            break_deadline_wall_ms = (
                max(0, int(deadline_value)) if deadline_value is not None else None
            )
        except (TypeError, ValueError):
            break_deadline_wall_ms = None

        if status is TimerStatus.IDLE:
            remaining_ms = 0
            break_kind = None
            phase_total_ms = 0
            break_deadline_wall_ms = None
        elif status.is_break and break_kind is None:
            break_kind = BreakKind.SHORT
        if status is not TimerStatus.IDLE and phase_total_ms < remaining_ms:
            # Existing databases did not store the original phase duration.
            phase_total_ms = remaining_ms

        needs_runtime_migration = False
        if status.is_break:
            now_ms = (
                int(now_wall_ms)
                if now_wall_ms is not None
                else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            )
            if legacy_break_paused:
                # A previously-paused rest resumes from its preserved remaining
                # duration at the first launch of the new version.
                break_deadline_wall_ms = now_ms + remaining_ms
                needs_runtime_migration = True
            elif break_deadline_wall_ms is None:
                saved_at_ms = self._parse_updated_at_ms(row[6])
                break_deadline_wall_ms = (saved_at_ms or now_ms) + remaining_ms
                needs_runtime_migration = True

        return (
            RuntimeSnapshot(
                status,
                remaining_ms,
                completed_in_cycle,
                break_kind,
                phase_total_ms,
                break_deadline_wall_ms,
            ),
            needs_runtime_migration,
        )

    def load_runtime(self, now_wall_ms: int | None = None) -> RuntimeSnapshot:
        """Return a normalized runtime snapshot without writing legacy rows."""
        snapshot, _ = self._read_runtime(now_wall_ms)
        return snapshot

    def load_runtime_with_metadata(
        self,
        now_wall_ms: int,
    ) -> tuple[RuntimeSnapshot, bool]:
        """Return the runtime and whether Engine should persist its migration.

        This keeps read-only callers non-destructive while allowing the engine
        to atomically promote known legacy break states on startup.
        """
        return self._read_runtime(now_wall_ms)

    def load_current_focus_pending(self) -> dict[str, int]:
        """Return unfinalized focus allocations for the active focus phase."""
        rows = self.connection.execute(
            "SELECT day, focus_ms FROM current_focus_pending WHERE singleton_id = 1"
        ).fetchall()
        return {
            str(day): max(0, int(focus_ms))
            for day, focus_ms in rows
            if int(focus_ms) > 0
        }

    def commit(
        self,
        snapshot: RuntimeSnapshot,
        focus_allocations: Mapping[str, int] | None = None,
        completion_days: Sequence[str] = (),
        *,
        updated_at_wall_ms: int | None = None,
        current_focus_pending: Mapping[str, int] | None = None,
    ) -> None:
        allocations = focus_allocations or {}
        cursor = self.connection.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            for day, duration_ms in allocations.items():
                if duration_ms <= 0:
                    continue
                cursor.execute(
                    """
                    INSERT INTO daily_stats (day, focus_ms, completed_sessions)
                    VALUES (?, ?, 0)
                    ON CONFLICT(day) DO UPDATE SET
                        focus_ms = focus_ms + excluded.focus_ms
                    """,
                    (day, int(duration_ms)),
                )
            for day in completion_days:
                cursor.execute(
                    """
                    INSERT INTO daily_stats (day, focus_ms, completed_sessions)
                    VALUES (?, 0, 1)
                    ON CONFLICT(day) DO UPDATE SET
                        completed_sessions = completed_sessions + 1
                    """,
                    (day,),
                )
            if current_focus_pending is not None:
                cursor.execute(
                    "DELETE FROM current_focus_pending WHERE singleton_id = 1"
                )
                for day, duration_ms in current_focus_pending.items():
                    if duration_ms <= 0:
                        continue
                    cursor.execute(
                        """
                        INSERT INTO current_focus_pending (
                            singleton_id, day, focus_ms
                        ) VALUES (1, ?, ?)
                        """,
                        (day, int(duration_ms)),
                    )
            cursor.execute(
                """
                UPDATE runtime_state SET
                    status = ?, remaining_ms = ?, completed_in_cycle = ?,
                    break_kind = ?, phase_total_ms = ?,
                    break_deadline_wall_ms = ?, updated_at = ?
                WHERE singleton_id = 1
                """,
                (
                    snapshot.status.value,
                    int(snapshot.remaining_ms),
                    int(snapshot.completed_in_cycle),
                    snapshot.break_kind.value if snapshot.break_kind else None,
                    int(snapshot.phase_total_ms),
                    (
                        int(snapshot.break_deadline_wall_ms)
                        if snapshot.break_deadline_wall_ms is not None
                        else None
                    ),
                    self._updated_at_value(updated_at_wall_ms),
                ),
            )
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def stats_for_day(self, day: str) -> DailyStats:
        row = self.connection.execute(
            "SELECT focus_ms, completed_sessions FROM daily_stats WHERE day = ?",
            (day,),
        ).fetchone()
        if row is None:
            return DailyStats()
        return DailyStats(focus_ms=int(row[0]), completed_sessions=int(row[1]))

    def close(self) -> None:
        self.connection.close()
