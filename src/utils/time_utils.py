from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from src.utils.logger import logger


def is_within_business_hours() -> bool:
    default_tz = "America/Sao_Paulo"
    default_start = "07:00"
    default_end = "23:30"

    tz_name = os.getenv("GN_TIME_ZONE", default_tz)
    start_str = os.getenv("GN_START_TIME", default_start)
    end_str = os.getenv("GN_END_TIME", default_end)

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.warning(
            f"GN_TIME_ZONE inválido ({tz_name!r}); usando padrão {default_tz!r}"
        )
        tz = ZoneInfo(default_tz)

    def _parse_hhmm(raw: str, fallback: str, env_name: str):
        try:
            return datetime.strptime(raw, "%H:%M").time()
        except Exception:
            logger.warning(f"{env_name} inválido ({raw!r}); usando padrão {fallback!r}")
            return datetime.strptime(fallback, "%H:%M").time()

    start_t = _parse_hhmm(start_str, default_start, "GN_START_TIME")
    end_t = _parse_hhmm(end_str, default_end, "GN_END_TIME")
    now_t = datetime.now(tz).time().replace(second=0, microsecond=0)

    if start_t <= end_t:
        return start_t <= now_t <= end_t
    return now_t >= start_t or now_t <= end_t
