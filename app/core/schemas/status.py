from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ToolStatus:
    name: str
    description: str
    requires_confirmation: bool


@dataclass(frozen=True)
class TimezoneStatus:
    zone: str | None
    now: datetime
