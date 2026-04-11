from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.config.config_loader import get_effective_config, reset_config_cache
from src.utils.logger import logger


def is_within_business_hours() -> bool:
    """Verifica se o horário atual está dentro da janela operacional configurada.

    Parâmetros lidos do loader central (config.json → env → defaults):
      - operationWindow.timeZone  (GN_TIME_ZONE)
      - operationWindow.start     (GN_START_TIME)
      - operationWindow.end       (GN_END_TIME)

    Hot-reload: a configuração é relida a cada verificação (sem cache) para que
    alterações em config.json ou nas variáveis de ambiente tenham efeito imediato
    sem precisar reiniciar o processo.
    """
    reset_config_cache()
    win = get_effective_config().operation_window

    tz_name = win.time_zone
    start_str = win.start
    end_str = win.end

    default_tz = "America/Sao_Paulo"
    default_start = "07:00"
    default_end = "23:30"

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.warning(
            f"operationWindow.timeZone inválido ({tz_name!r}); usando padrão {default_tz!r}"
        )
        tz = ZoneInfo(default_tz)

    def _parse_hhmm(raw: str, fallback: str, field_name: str):
        try:
            return datetime.strptime(raw, "%H:%M").time()
        except Exception:
            logger.warning(
                f"operationWindow.{field_name} inválido ({raw!r}); usando padrão {fallback!r}"
            )
            return datetime.strptime(fallback, "%H:%M").time()

    start_t = _parse_hhmm(start_str, default_start, "start")
    end_t = _parse_hhmm(end_str, default_end, "end")
    now_t = datetime.now(tz).time().replace(second=0, microsecond=0)

    logger.debug(
        f"Janela operacional: {start_t.strftime('%H:%M')}–{end_t.strftime('%H:%M')} "
        f"({tz_name}) | agora: {now_t.strftime('%H:%M')}"
    )

    if start_t <= end_t:
        return start_t <= now_t <= end_t
    return now_t >= start_t or now_t <= end_t
