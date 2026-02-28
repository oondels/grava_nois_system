from __future__ import annotations

from collections import deque
import threading
from typing import Deque, List, Optional

from src.config.settings import CaptureConfig
from src.utils.logger import logger


class SegmentBuffer:
    def __init__(self, cfg: CaptureConfig):
        self.cfg = cfg
        self._segments: Deque[str] = deque(maxlen=cfg.max_segments)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None

    def start(self) -> None:
        self._t = threading.Thread(target=self._index_loop, daemon=True)
        self._t.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=join_timeout)

    def snapshot_last(self, n: int) -> List[str]:
        with self._lock:
            return list(self._segments)[-n:]

    def _index_loop(self) -> None:
        while not self._stop.is_set():
            # ordena pelo número do arquivo
            def _segnum(p):
                try:
                    return int(p.stem.replace("buffer", ""))
                except Exception:
                    return -1

            files = sorted(self.cfg.buffer_dir.glob("buffer*.ts"), key=_segnum)

            # limpa excedentes no disco
            extra = files[: -self.cfg.max_segments]
            for p in extra:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            files = files[-self.cfg.max_segments :]
            with self._lock:
                self._segments.clear()
                self._segments.extend(str(p) for p in files)
            self._stop.wait(self.cfg.scan_interval)



def clear_buffer(cfg) -> None:
    """
    Remove segmentos remanescentes de execuções anteriores no diretório
    de buffer (ex.: buffer%06d.ts/.mp4) e limpa também a pasta de staging usada na
    concatenação de segmentos. Isso garante que um highlight novo não concatene
    pedaços antigos.

    A função é idempotente e tolerante a erros.
    """
    try:
        cfg.buffer_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Não foi possível garantir a pasta de buffer: {e}")

    removed = 0
    # Apaga apenas arquivos que seguem o padrão de segmentos
    for pattern in ("buffer*.ts", "buffer*.mp4"):
        for p in cfg.buffer_dir.glob(pattern):
            try:
                p.unlink()
                removed += 1
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Erro ao apagar {p}: {e}")

    logger.info(f"Buffer limpo: {removed} segmentos removidos")
