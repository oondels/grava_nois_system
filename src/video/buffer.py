from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import csv
import math
import io
import time
from pathlib import Path
from typing import Deque, List, Optional

from src.config.settings import CaptureConfig
from src.utils.logger import logger


@dataclass(frozen=True)
class SegmentBufferDiagnostics:
    segment_count: int
    last_segment: str | None
    last_segment_at: str | None
    segment_age_sec: float | None
    buffer_status: str

    @property
    def buffer_fresh(self) -> bool:
        return self.buffer_status == "FRESH"


class SegmentBuffer:
    def __init__(self, cfg: CaptureConfig):
        self.cfg = cfg
        self._segments: Deque[str] = deque(maxlen=cfg.max_segments)
        self._lock = threading.RLock()
        self._closed: dict[str, dict] = {}
        self._reservations: dict[str, tuple[float, float, set[str]]] = {}
        self._media_origin: float | None = None
        self._session_id = cfg.capture_session_id
        self._listing = cfg.segment_list_path
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None

    def start(self) -> None:
        self._t = threading.Thread(target=self._index_loop, daemon=True)
        self._t.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout=join_timeout)
        self._cleanup_stopped_session()

    def snapshot_last(self, n: int) -> List[str]:
        with self._lock:
            return list(self._segments)[-n:]

    def diagnostics(self, *, stale_after_sec: float) -> SegmentBufferDiagnostics:
        with self._lock:
            segments = list(self._segments)

        if not segments:
            return SegmentBufferDiagnostics(
                segment_count=0,
                last_segment=None,
                last_segment_at=None,
                segment_age_sec=None,
                buffer_status="EMPTY",
            )

        last_segment = segments[-1]
        try:
            last_path = Path(last_segment)
            mtime = last_path.stat().st_mtime
        except FileNotFoundError:
            return SegmentBufferDiagnostics(
                segment_count=len(segments),
                last_segment=last_segment,
                last_segment_at=None,
                segment_age_sec=None,
                buffer_status="MISSING",
            )
        except Exception:
            return SegmentBufferDiagnostics(
                segment_count=len(segments),
                last_segment=last_segment,
                last_segment_at=None,
                segment_age_sec=None,
                buffer_status="UNKNOWN",
            )

        age = max(0.0, time.time() - mtime)
        status = "FRESH" if age <= stale_after_sec else "STALE"
        return SegmentBufferDiagnostics(
            segment_count=len(segments),
            last_segment=last_segment,
            last_segment_at=datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            segment_age_sec=round(age, 3),
            buffer_status=status,
        )

    def _index_loop(self) -> None:
        while not self._stop.is_set():
            if self.cfg.track_segments:
                self._index_closed()
                self._stop.wait(0.1)
                continue
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

    def protect(self, job_id: str, start: float, end: float) -> list[dict]:
        """Register pre AND future post segments under the eviction lock."""
        with self._lock:
            self._reservations[job_id] = (start, end, set())
            return self.pending_segments(job_id)

    def pending_segments(self, job_id: str) -> list[dict]:
        with self._lock:
            start, end, copied = self._reservations[job_id]
            return [dict(seg) for key, seg in self._closed.items()
                    if key not in copied and seg["start_mono"] < end and seg["end_mono"] > start]

    def copied(self, job_id: str, path: str) -> None:
        with self._lock:
            self._reservations[job_id][2].add(path)

    def release(self, job_id: str) -> None:
        with self._lock:
            self._reservations.pop(job_id, None)
        self._cleanup_stopped_session()

    def _cleanup_stopped_session(self) -> None:
        with self._lock:
            if not self.cfg.track_segments or not self._stop.is_set() or self._reservations or not self._session_id:
                return
        for path in self.cfg.buffer_dir.glob(f"buffer-{self._session_id}-*.ts"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Stopped capture segment cleanup pending")
        if self._listing:
            self._listing.unlink(missing_ok=True)

    def _index_closed(self) -> None:
        listing = self._listing
        if listing is None:
            return
        try:
            raw = listing.read_text()
            # Ignore a trailing partial record while FFmpeg rewrites its list.
            raw = raw[:raw.rfind("\n") + 1]
            rows = list(csv.reader(io.StringIO(raw)))
            entries = []
            for row in rows:
                if len(row) != 3 or Path(row[0]).name != row[0]:
                    continue
                path = self.cfg.buffer_dir / row[0]
                start, end = float(row[1]), float(row[2])
                if not math.isfinite(start) or not math.isfinite(end) or end <= start or path.is_symlink() or not path.is_file():
                    continue
                entries.append((path, start, end))
        except (OSError, ValueError, csv.Error):
            return
        with self._lock:
            if entries and self._media_origin is None:
                # Local observation, NOT a claim about the camera's absolute clock.
                self._media_origin = time.monotonic() - entries[-1][2]
            for path, start, end in entries:
                key = str(path)
                if key not in self._closed:
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    if size <= 0:
                        continue
                    self._closed[key] = {"source": key, "name": path.name,
                        "session_id": self._session_id,
                        "media_start": start, "media_end": end,
                        "start_mono": self._media_origin + start,
                        "end_mono": self._media_origin + end,
                        "size_bytes": size}
            ordered = sorted(self._closed, key=lambda key: self._closed[key]["media_start"])
            evict = []
            for key in ordered[:-self.cfg.max_segments]:
                seg = self._closed[key]
                protected = any(key not in copied and seg["start_mono"] < end and seg["end_mono"] > start
                                for start, end, copied in self._reservations.values())
                if not protected:
                    evict.append(key)
                    del self._closed[key]
            # Exclude evicting files before releasing the lock; no new pin can select them.
            self._segments.clear()
            self._segments.extend(key for key in ordered[-self.cfg.max_segments:] if key in self._closed)
        for key in evict:
            try:
                Path(key).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not evict closed buffer segment")



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
    for pattern in ("buffer*.ts", "buffer*.mp4", "closed-*.csv"):
        for p in cfg.buffer_dir.glob(pattern):
            try:
                p.unlink()
                removed += 1
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Erro ao apagar {p}: {e}")

    logger.info(f"Buffer limpo: {removed} segmentos removidos")
