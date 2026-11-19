"""Daily, local-wall-clock notification silence. Stored times are minute-of-day."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class QuietHours:
    enabled: bool = False
    start_minute: int = 22 * 60
    end_minute: int = 8 * 60
    summary: bool = False

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.summary) is not bool:
            raise ValueError("免打扰开关无效")
        if any(type(value) is not int or not 0 <= value < 1440 for value in (self.start_minute, self.end_minute)):
            raise ValueError("时段必须为本地时间 00:00–23:59")
        if self.start_minute == self.end_minute:
            raise ValueError("开始时间和结束时间须不同")

    def ends_at(self, now: float) -> float | None:
        if not self.enabled:
            return None
        local = datetime.fromtimestamp(now)
        minute = local.hour * 60 + local.minute
        wraps = self.end_minute < self.start_minute
        if wraps:
            if self.end_minute <= minute < self.start_minute:
                return None
        elif not self.start_minute <= minute < self.end_minute:
            return None
        end = local.replace(hour=self.end_minute // 60, minute=self.end_minute % 60, second=0, microsecond=0)
        if wraps and minute >= self.start_minute:
            end += timedelta(days=1)
        return end.timestamp()
