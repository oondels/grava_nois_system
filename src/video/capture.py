from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from dotenv import load_dotenv

from src.config.settings import CaptureConfig
from src.utils.logger import logger

load_dotenv()


def check_rtsp_connectivity(
    rtsp_url: str, timeout: int = 5, max_retries: int = 10, camera_id: str = ""
) -> bool:
    """
    Verifica se a câmera RTSP está acessível antes de iniciar o FFmpeg.

    Args:
        rtsp_url: URL RTSP completa (ex: rtsp://user:pass@192.168.1.21:554/cam/realmonitor)
        timeout: Tempo limite por tentativa em segundos
        max_retries: Número máximo de tentativas
        camera_id: Identificador da câmera para logs (opcional)

    Returns:
        True se a câmera estiver acessível, False caso contrário
    """
    prefix = f"[{camera_id}] " if camera_id else ""
    try:
        parsed = urlparse(rtsp_url)
        host = parsed.hostname
        port = parsed.port or 554

        if not host:
            logger.error(f"{prefix}URL RTSP inválida (hostname não encontrado)")
            return False

        logger.info(f"{prefix}Verificando conectividade com câmera {host}:{port}...")

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"{prefix}Tentativa {attempt}/{max_retries}...")
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect((host, port))
                sock.close()
                logger.info(f"{prefix}Câmera acessível em {host}:{port}")
                return True
            except (socket.timeout, socket.error, OSError) as e:
                logger.warning(f"{prefix}Falha na tentativa {attempt}: {e}")
                if attempt < max_retries:
                    wait_time = 5
                    logger.info(f"{prefix}Aguardando {wait_time}s antes de tentar novamente...")
                    time.sleep(wait_time)

        logger.error(f"{prefix}Câmera não acessível após {max_retries} tentativas")
        return False

    except Exception as e:
        logger.exception(f"{prefix}Erro inesperado ao verificar conectividade RTSP: {e}")
        return False


def _calc_start_number(buffer_dir: Path) -> int:
    pattern = re.compile(r"buffer(\d{3,})\.ts$")
    nums: List[int] = []
    for f in os.listdir(buffer_dir):
        m = pattern.match(f)
        if m:
            try:
                nums.append(int(m.group(1)))
            except ValueError:
                pass
    return (max(nums) + 1) if nums else 0


def start_ffmpeg(cfg: CaptureConfig) -> subprocess.Popen:
    start_num = _calc_start_number(cfg.buffer_dir)
    out_pattern = str(cfg.buffer_dir / "buffer%06d.ts")
    # URL RTSP por câmera (fallback legado via GN_RTSP_URL)
    rtsp_url = (cfg.rtsp_url or os.getenv("GN_RTSP_URL") or "").strip()

    use_rtsp = bool(rtsp_url)

    # Health check: verifica conectividade com câmera RTSP antes de iniciar FFmpeg
    if use_rtsp:
        max_retries = int(os.getenv("GN_RTSP_MAX_RETRIES", "10"))
        timeout = int(os.getenv("GN_RTSP_TIMEOUT", "5"))

        if not check_rtsp_connectivity(
            rtsp_url, timeout=timeout, max_retries=max_retries, camera_id=cfg.camera_id
        ):
            raise RuntimeError(
                f"Câmera RTSP não acessível após {max_retries} tentativas. "
                "Verifique:\n"
                "  1. Se a câmera está ligada e conectada à rede\n"
                "  2. Se o endereço IP e porta estão corretos em GN_RTSP_URL\n"
                "  3. Se há conectividade de rede entre Raspberry e câmera\n"
                "  4. Se o firewall não está bloqueando a porta RTSP (padrão: 554)"
            )

    if use_rtsp:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-rtsp_flags",
            "prefer_tcp",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-i",
            rtsp_url,
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-an",
            "-f",
            "segment",
            "-segment_format",
            "mpegts",
            "-segment_time",
            str(cfg.seg_time),
            "-segment_start_number",
            str(start_num),
            "-reset_timestamps",
            "0",
            out_pattern,
        ]
    else:
        framerate_raw = os.getenv("GN_INPUT_FRAMERATE", "30")
        video_size = os.getenv("GN_VIDEO_SIZE", "1280x720")
        gop = max(1, int(float(framerate_raw)))
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-f",
            "v4l2",
            "-thread_queue_size",
            "512",
            "-input_format",
            "mjpeg",
            "-framerate",
            str(framerate_raw),
            "-video_size",
            str(video_size),
            "-i",
            cfg.device,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(framerate_raw),
            "-g",
            str(gop),
            "-keyint_min",
            str(gop),
            "-sc_threshold",
            "0",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{cfg.seg_time})",
            "-f",
            "segment",
            "-segment_format",
            "mpegts",
            "-segment_time",
            str(cfg.seg_time),
            "-segment_start_number",
            str(start_num),
            "-reset_timestamps",
            "0",
            out_pattern,
        ]

    # Configurar logging do FFmpeg (fallback relativo à raiz do projeto)
    base_dir = Path(__file__).resolve().parent.parent.parent
    default_log_dir = base_dir / "logs"
    log_dir = Path(os.getenv("GN_LOG_DIR", default_log_dir))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(
            f"GN_LOG_DIR inválido ou sem permissão ({log_dir}): {e}. "
            f"Usando fallback local: {default_log_dir}"
        )
        log_dir = default_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = log_dir / f"ffmpeg_{cfg.camera_id}.log"

    try:
        # Abre arquivo de log em modo append
        log_file = open(log_file_path, "a", buffering=1)  # line buffering
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log_file.write(f"\n{'='*80}\n")
        log_file.write(f"[{timestamp}] Iniciando FFmpeg\n")
        log_file.write(f"Comando: {' '.join(cmd)}\n")
        log_file.write(f"{'='*80}\n\n")
        log_file.flush()

        logger.info(f"FFmpeg logs sendo salvos em: {log_file_path}")

        return subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,  # Combina stderr com stdout
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg não encontrado no PATH") from exc
    except Exception as exc:
        raise RuntimeError(f"Erro ao iniciar FFmpeg: {exc}") from exc
