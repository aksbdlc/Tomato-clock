from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timedelta


def local_day(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000).date().isoformat()


def split_duration_by_local_day(start_epoch_ms: int, duration_ms: int) -> dict[str, int]:
    """Split monotonic elapsed time across local calendar days.

    ``start_epoch_ms`` anchors the interval to wall time, while ``duration_ms``
    comes from a monotonic clock. This keeps the elapsed duration stable even if
    the wall clock is corrected.
    """

    if duration_ms <= 0:
        return {}

    allocations: defaultdict[str, int] = defaultdict(int)
    cursor_ms = start_epoch_ms
    remaining_ms = duration_ms

    while remaining_ms > 0:
        current_date = datetime.fromtimestamp(cursor_ms / 1000).date()
        next_date: date = current_date + timedelta(days=1)
        next_midnight_seconds = time.mktime(
            (next_date.year, next_date.month, next_date.day, 0, 0, 0, 0, 0, -1)
        )
        boundary_ms = int(next_midnight_seconds * 1000)

        # Defensive fallback for unusual timezone transitions.
        if boundary_ms <= cursor_ms:
            chunk_ms = remaining_ms
        else:
            chunk_ms = min(remaining_ms, boundary_ms - cursor_ms)

        allocations[current_date.isoformat()] += chunk_ms
        cursor_ms += chunk_ms
        remaining_ms -= chunk_ms

    return dict(allocations)

