from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    def __init__(self, now_datetime: datetime) -> None:
        if now_datetime.tzinfo is None or now_datetime.utcoffset() is None:
            raise ValueError("now_datetime must be timezone-aware")
        self._now_datetime = now_datetime

    def now(self) -> datetime:
        return self._now_datetime
