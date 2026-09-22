"""Processing authorization; civil windows and elapsed activity are independent."""

import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


def validate_windows(windows: object) -> list[str]:
    if not isinstance(windows, list) or len(windows) > 128:
        return ["additionalWindows must be a list of at most 128 intervals"]
    errors = []
    for window in windows:
        if not isinstance(window, dict) or set(window) != {"weekdays", "start", "end"}:
            errors.append("each interval requires only weekdays, start and end")
            continue
        days = window["weekdays"]
        if (
            not isinstance(days, list)
            or not days
            or any(type(d) is not int or not 1 <= d <= 7 for d in days)
        ):
            errors.append("weekdays must contain ISO days 1..7")
        for key in ("start", "end"):
            if not isinstance(window[key], str) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", window[key]
            ):
                errors.append(f"{key} must be HH:MM")
        if window["start"] == window["end"]:
            errors.append("interval start and end must differ")
    return errors


def window_authorization(
    now: datetime, time_zone: str, windows: list[dict[str, Any]]
) -> str | None:
    local = now.astimezone(ZoneInfo(time_zone))
    minute = local.hour * 60 + local.minute
    if minute < 300:
        return "mandatory_window"
    day = local.isoweekday()
    previous = 7 if day == 1 else day - 1
    for window in windows:
        start, end = (int(window[k][:2]) * 60 + int(window[k][3:]) for k in ("start", "end"))
        if start < end:
            allowed = day in window["weekdays"] and start <= minute < end
        else:
            allowed = (day in window["weekdays"] and minute >= start) or (
                previous in window["weekdays"] and minute < end
            )
        if allowed:
            return "additional_window"
    return None


class DeviceActivity:
    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.clock = monotonic
        self._last = monotonic()  # conservative after every process restart
        self._lock = threading.Lock()

    def record(self, instant: float | None = None) -> None:
        with self._lock:
            self._last = max(self._last, self.clock() if instant is None else instant)

    def idle_seconds(self) -> float:
        with self._lock:
            return max(0.0, self.clock() - self._last)

    def authorization(
        self, now: datetime, time_zone: str, windows: list[dict[str, Any]]
    ) -> str | None:
        return window_authorization(now, time_zone, windows) or (
            "idle" if self.idle_seconds() >= 1800 else None
        )
