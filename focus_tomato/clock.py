from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def monotonic_ms(self) -> int: ...

    def wall_epoch_ms(self) -> int: ...


class SystemClock:
    def monotonic_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000

    def wall_epoch_ms(self) -> int:
        return time.time_ns() // 1_000_000

