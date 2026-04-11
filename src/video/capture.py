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

from src.config.config_loader import get_effective_config
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


def _sanitize_cmd_for_log(cmd: list[str]) -> str:
    """Remove credenciais RTSP de comandos para log seguro."""
    sanitized = []
    for arg in cmd:
        if "rtsp://" in arg.lower():
            parsed = urlparse(arg)
            if parsed.username or parsed.password:
                host_port = f"{parsed.hostname}:{parsed.port or 554}"
                clean = parsed._replace(netloc=f"***:***@{host_port}")
                from urllib.parse import urlunparse

                sanitized.append(urlunparse(clean))
            else:
                sanitized.append(arg)
        else:
            sanitized.append(arg)
    return " ".join(sanitized)


def _tail_file(path: Path, max_lines: int = 20) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:]).strip()
    except Exception:
        return ""


def start_ffmpeg(cfg: CaptureConfig) -> subprocess.Popen:
    start_num = _calc_start_number(cfg.buffer_dir)
    out_pattern = str(cfg.buffer_dir / "buffer%06d.ts")
    # URL RTSP por câmera (fallback legado via GN_RTSP_URL)
    rtsp_url = (cfg.rtsp_url or os.getenv("GN_RTSP_URL") or "").strip()

    use_rtsp = bool(rtsp_url)

    # Health check: verifica conectividade com câmera RTSP antes de iniciar FFmpeg
    if use_rtsp:
        _rtsp_check_cfg = get_effective_config().capture.rtsp
        max_retries = _rtsp_check_cfg.max_retries
        timeout = _rtsp_check_cfg.timeout_seconds

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
        _cfg_op = get_effective_config()
        _rtsp_cfg = _cfg_op.capture.rtsp
        _light_mode = _cfg_op.processing.light_mode

        # --- Resolve perfil efetivo ----------------------------------------
        # Precedência: profile explícito > env GN_RTSP_PROFILE (já absorvido
        # no loader) > inferência por lightMode > fallback "compatible".
        effective_profile: str = _rtsp_cfg.profile or (
            "hq" if not _light_mode else "compatible"
        )

        # --- Resolve reencode efetivo --------------------------------------
        # None = usa default do profile; True/False = override explícito.
        if _rtsp_cfg.reencode is None:
            rtsp_reencode = effective_profile == "compatible"
        else:
            rtsp_reencode = _rtsp_cfg.reencode

        rtsp_gop = _rtsp_cfg.gop
        rtsp_preset = _rtsp_cfg.preset or "veryfast"
        rtsp_crf = _rtsp_cfg.crf
        rtsp_fps = _rtsp_cfg.fps
        # wallclock: só aplica se explicitamente habilitado (nunca pelo profile)
        rtsp_use_wallclock = _rtsp_cfg.use_wallclock_timestamps

        logger.info(
            f"[{cfg.camera_id}] Perfil RTSP: {effective_profile} | "
            f"reencode={rtsp_reencode} | "
            f"low_latency_input={_rtsp_cfg.low_latency_input} | "
            f"low_delay_codec_flags={_rtsp_cfg.low_delay_codec_flags}"
        )

        cmd = [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-rtsp_flags",
            "prefer_tcp",
        ]

        # Wallclock timestamps: ativa apenas se explicitamente habilitado.
        # Útil para câmeras que geram timestamps não-monotônicos.
        # Use com cuidado: pode causar jitter em redes instáveis.
        if rtsp_use_wallclock:
            cmd += ["-use_wallclock_as_timestamps", "1"]

        # lowLatencyInput: reduz buffering do probe de entrada.
        # Ganho limitado ao buffer de análise inicial; não elimina latência
        # de rede ou pipeline. Mantido fora do caminho padrão.
        if _rtsp_cfg.low_latency_input:
            cmd += ["-fflags", "nobuffer"]

        cmd += [
            # +genpts: regenera PTS para frames sem timestamp.
            "-fflags",
            "+genpts",
            # ignore_err: decoder tenta reconstruir macroblocks corrompidos
            # via error concealment em vez de descartar o frame inteiro.
            "-err_detect",
            "ignore_err",
            "-i",
            rtsp_url,
            "-map",
            "0:v:0",
            "-an",
        ]

        if rtsp_reencode:
            # Profile compatible / reencode explícito:
            # libx264 com force_key_frames e fps_mode=vfr para tolerância
            # a DTS/PTS ruins e perda de pacotes.
            if rtsp_fps:
                cmd += ["-vf", f"fps={rtsp_fps}"]

            # lowDelayCodecFlags: só faz sentido com reencode ativo.
            if _rtsp_cfg.low_delay_codec_flags:
                cmd += ["-flags", "low_delay"]

            cmd += [
                "-c:v",
                "libx264",
                "-preset",
                rtsp_preset,
                "-crf",
                str(rtsp_crf),
                "-pix_fmt",
                "yuv420p",
                "-g",
                str(rtsp_gop),
                "-keyint_min",
                str(rtsp_gop),
                "-sc_threshold",
                "0",
                # Força keyframe no início de cada segmento para que
                # a concatenação posterior nunca inicie sem um IDR.
                "-force_key_frames",
                f"expr:gte(t,n_forced*{cfg.seg_time})",
                # vfr: não duplica frames quando o stream entrega menos
                # do que o esperado; evita "congelamento" massivo.
                "-fps_mode",
                "vfr",
            ]
        else:
            # Profile hq / passthrough:
            # Sem re-encode — preserva qualidade original da câmera.
            # Adequado para câmeras com timestamps estáveis.
            if _rtsp_cfg.low_delay_codec_flags:
                logger.warning(
                    f"[{cfg.camera_id}] low_delay_codec_flags=true ignorado: "
                    "sem efeito com -c:v copy (reencode desativado)"
                )

            cmd += ["-c:v", "copy"]

        cmd += [
            "-f",
            "segment",
            "-segment_format",
            "mpegts",
            "-segment_time",
            str(cfg.seg_time),
            "-segment_start_number",
            str(start_num),
            "-reset_timestamps",
            "1",
            out_pattern,
        ]
    else:
        _v4l2_cfg = get_effective_config().capture.v4l2
        framerate_raw = str(_v4l2_cfg.framerate)
        video_size = _v4l2_cfg.video_size or "1280x720"
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

    log_file = None
    try:
        # Abre arquivo de log em modo append
        log_file = open(log_file_path, "a", buffering=1)  # line buffering
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log_file.write(f"\n{'='*80}\n")
        log_file.write(f"[{timestamp}] Iniciando FFmpeg\n")
        log_file.write(f"Comando: {_sanitize_cmd_for_log(cmd)}\n")
        log_file.write(f"{'='*80}\n\n")
        log_file.flush()

        logger.info(f"FFmpeg logs sendo salvos em: {log_file_path}")

        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,  # Combina stderr com stdout
            stdin=subprocess.DEVNULL,
        )

        # O processo filho mantém o fd aberto; no pai podemos fechar para evitar vazamento.
        try:
            log_file.close()
        except Exception:
            pass

        startup_check_sec = get_effective_config().capture.rtsp.startup_check_seconds
        time.sleep(startup_check_sec)
        return_code = proc.poll()
        if return_code is not None:
            tail = _tail_file(log_file_path, max_lines=20)
            detail = f"\nÚltimas linhas do log:\n{tail}" if tail else ""
            raise RuntimeError(
                f"FFmpeg encerrou durante inicialização para {cfg.camera_id} "
                f"(exit code {return_code}). Verifique URL/credenciais RTSP. "
                f"Log: {log_file_path}{detail}"
            )
        return proc
    except FileNotFoundError as exc:
        if log_file is not None and not log_file.closed:
            log_file.close()
        raise RuntimeError("ffmpeg não encontrado no PATH") from exc
    except Exception as exc:
        if log_file is not None and not log_file.closed:
            log_file.close()
        raise RuntimeError(f"Erro ao iniciar FFmpeg: {exc}") from exc
