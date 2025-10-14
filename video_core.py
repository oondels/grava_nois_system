import os, re, json, time, hashlib, subprocess, threading, platform, shutil, socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from typing import Deque, List, Optional, Tuple, Dict, Any
from dataclasses import dataclass
from dotenv import load_dotenv
from urllib.parse import urlparse
import requests

load_dotenv()


# ---- CONFIG & TYPES ---------------------------------------------------------
@dataclass
class CaptureConfig:
    buffer_dir: Path
    clips_dir: Path  # onde o highlight nasce
    queue_dir: Path  # fila para tratamento posterior (raw)
    failed_dir_highlight: Path
    device: str = "/dev/video0"
    seg_time: int = 1
    pre_seconds: int = 25
    post_seconds: int = 10
    scan_interval: float = 1
    max_buffer_seconds: int = 40
    pre_segments: Optional[int] = None
    post_segments: Optional[int] = None

    @property
    def max_segments(self) -> int:
        return max(1, int(self.max_buffer_seconds / self.seg_time))

    def ensure_dirs(self) -> None:
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir_highlight.mkdir(parents=True, exist_ok=True)


# ---- Health check RTSP ------------------------------------------------------
def check_rtsp_connectivity(rtsp_url: str, timeout: int = 5, max_retries: int = 10) -> bool:
    """
    Verifica se a câmera RTSP está acessível antes de iniciar o FFmpeg.

    Args:
        rtsp_url: URL RTSP completa (ex: rtsp://user:pass@192.168.1.21:554/cam/realmonitor)
        timeout: Tempo limite por tentativa em segundos
        max_retries: Número máximo de tentativas

    Returns:
        True se a câmera estiver acessível, False caso contrário
    """
    try:
        parsed = urlparse(rtsp_url)
        host = parsed.hostname
        port = parsed.port or 554

        if not host:
            print("[rtsp-check] ERRO: URL RTSP inválida (hostname não encontrado)")
            return False

        print(f"[rtsp-check] Verificando conectividade com câmera {host}:{port}...")

        for attempt in range(1, max_retries + 1):
            try:
                print(f"[rtsp-check] Tentativa {attempt}/{max_retries}...")
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect((host, port))
                sock.close()
                print(f"[rtsp-check] ✓ Câmera acessível em {host}:{port}!")
                return True
            except (socket.timeout, socket.error, OSError) as e:
                print(f"[rtsp-check] ✗ Falha: {e}")
                if attempt < max_retries:
                    wait_time = 5
                    print(f"[rtsp-check] Aguardando {wait_time}s antes de tentar novamente...")
                    time.sleep(wait_time)

        print(f"[rtsp-check] ERRO: Câmera não acessível após {max_retries} tentativas.")
        return False

    except Exception as e:
        print(f"[rtsp-check] ERRO inesperado ao verificar conectividade: {e}")
        return False


# ---- FFmpeg recorder --------------------------------------------------------
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
    # Permite configurar a URL RTSP via env GN_RTSP_URL
    # Ex.: rtsp://user:pass@192.168.1.21:2399/cam/realmonitor?channel=1&subtype=0
    rtsp_url = (os.getenv("GN_RTSP_URL") or "").strip()

    use_rtsp = bool(rtsp_url)

    # Health check: verifica conectividade com câmera RTSP antes de iniciar FFmpeg
    if use_rtsp:
        max_retries = int(os.getenv("GN_RTSP_MAX_RETRIES", "10"))
        timeout = int(os.getenv("GN_RTSP_TIMEOUT", "5"))

        if not check_rtsp_connectivity(rtsp_url, timeout=timeout, max_retries=max_retries):
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

    # Configurar logging do FFmpeg
    log_dir = Path(os.getenv("GN_LOG_DIR", "/usr/src/app/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = log_dir / "ffmpeg.log"

    try:
        # Abre arquivo de log em modo append
        log_file = open(log_file_path, "a", buffering=1)  # line buffering
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log_file.write(f"\n{'='*80}\n")
        log_file.write(f"[{timestamp}] Iniciando FFmpeg\n")
        log_file.write(f"Comando: {' '.join(cmd)}\n")
        log_file.write(f"{'='*80}\n\n")
        log_file.flush()

        print(f"[ffmpeg] Logs sendo salvos em: {log_file_path}")

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


# ---- Segment buffer (indexer thread) ---------------------------------------
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


# ---- Constroi clip após usuarios clicar no botao ------------------------------------------------------
def build_highlight(cfg: CaptureConfig, segbuf: SegmentBuffer) -> Optional[Path]:
    print("Botão apertado! Aguardando pós-buffer…")

    # Pasta para arquivos com erro de build
    fail_build_dir = cfg.failed_dir_highlight / "build_failed"
    fail_build_dir.mkdir(parents=True, exist_ok=True)

    click_ts = time.time()
    if cfg.post_segments is not None:
        wait_after = cfg.post_segments * cfg.seg_time
    else:
        wait_after = cfg.post_seconds
    time.sleep(max(0, wait_after) + 0.50)

    # Calcula quantos segmentos de vídeo são necessários para cobrir o tempo total do highlight
    # (pré-buffer + pós-buffer). Como cada segmento tem duração cfg.seg_time, dividimos o tempo
    # total pela duração do segmento e arredondamos para garantir cobertura completa.
    pre_seg = (
        cfg.pre_segments
        if cfg.pre_segments is not None
        else max(1, int(round(cfg.pre_seconds / cfg.seg_time)))
    )
    post_seg = (
        cfg.post_segments
        if cfg.post_segments is not None
        else max(1, int(round(cfg.post_seconds / cfg.seg_time)))
    )
    need = max(1, pre_seg + post_seg)
    print(f"\n\n {need} segmentos são necessários para o highlight.")

    def _segnum_from_path(s):
        try:
            return int(Path(s).stem.replace("buffer", ""))
        except:
            return -1

    # Ultimos videos em buffer
    selected_videos = sorted(segbuf.snapshot_last(need), key=_segnum_from_path)
    print(f"Total de segmentos selecionados: {len(selected_videos)}")

    if not selected_videos:
        print("Nenhum segmento capturado — encerrando.")
        return None

    # Cria uma pasta de staging apenas para o arquivo de manifesto (concat list)
    timestamp = datetime.fromtimestamp(click_ts, tz=timezone.utc).strftime(
        "%Y%m%d-%H%M%SZ"
    )

    # Usamos a pasta de clipes gravados para o arquivo de lista temporário
    concat_list_path = cfg.clips_dir / f"concat_{timestamp}.txt"
    valid_segments = [
        p
        for p in (Path(s) for s in selected_videos)
        if p.exists() and p.stat().st_size > 0
    ]
    if not valid_segments or len(valid_segments) < 2:
        print("Nenhum segmento válido encontrado — encerrando.")
        return None

    with open(concat_list_path, "w") as f:
        for seg_path in valid_segments:
            f.write(f"file '{seg_path.resolve()}'\n")

    timestamp = datetime.fromtimestamp(click_ts, tz=timezone.utc).strftime(
        "%Y%m%d-%H%M%SZ"
    )
    tmp_ts = cfg.clips_dir / f"highlight_{timestamp}.ts"
    out_mp4 = cfg.clips_dir / f"highlight_{timestamp}.mp4"

    try:
        # concat TS -> TS (regen PTS)
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-fflags",
                "+genpts+igndts",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list_path),
                "-c",
                "copy",
                str(tmp_ts),
            ],
            check=True,
        )

        # remux TS -> MP4 (copy) com PTS normalizado e base zerada
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-fflags",
                "+genpts",
                "-i",
                str(tmp_ts),
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-movflags",
                "+faststart",
                "-avoid_negative_ts",
                "make_zero",
                str(out_mp4),
            ],
            check=True,
        )

        print(f"Saved {out_mp4}")
        return out_mp4

    except Exception as e:
        # Move quaisquer saídas parciais para a pasta de falha
        try:
            if tmp_ts.exists():
                tmp_ts.replace(fail_build_dir / tmp_ts.name)
        except Exception:
            pass
        try:
            if out_mp4.exists():
                out_mp4.replace(fail_build_dir / out_mp4.name)
        except Exception:
            pass

        err_txt = fail_build_dir / f"{timestamp}.error.txt"
        err_txt.write_text(f"build_highlight failed: {e}\n", encoding="utf-8")
        return None

    finally:
        try:
            concat_list_path.unlink(missing_ok=True)
        except Exception:
            pass


def _sha256_file(p: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def ffprobe_metadata(path: Path) -> Dict[str, Any]:
    """
    Usa ffprobe para extrair metadados básicos.
    Requer ffprobe no PATH.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,r_frame_rate:format=duration",
        "-of",
        "json",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    info = json.loads(r.stdout)
    stream = info.get("streams", [{}])[0]
    fmt = info.get("format", {})
    fps_str = stream.get("r_frame_rate", "0/1")
    try:
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 0.0
    except Exception:
        fps = 0.0
    return {
        "codec": stream.get("codec_name"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "fps": fps,
        "duration_sec": float(fmt.get("duration", 0.0)),
    }


def enqueue_clip(cfg: CaptureConfig, clip_path: Path) -> Path:
    print("Enqueueando clipe...")
    """
    Move o arquivo para a fila (queue_dir) e salva metadados .json ao lado.
    """
    clip_path = clip_path.resolve()
    size_bytes = clip_path.stat().st_size
    # Em modo leve, evitamos hash caro neste momento
    light_mode = (os.getenv("GN_LIGHT_MODE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }

    sha256 = None if light_mode else _sha256_file(clip_path)
    meta = ffprobe_metadata(clip_path)
    payload = {
        "type": "highlight_raw",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_name": clip_path.name,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "meta": meta,
        "pre_seconds": cfg.pre_seconds,
        "post_seconds": cfg.post_seconds,
        "seg_time": cfg.seg_time,
        "pre_segments": cfg.pre_segments,
        "post_segments": cfg.post_segments,
        "status": "queued",
    }

    dst = cfg.queue_dir / clip_path.name
    meta_path = cfg.queue_dir / (clip_path.stem + ".json")

    # move para a fila e grava sidecar
    shutil.move(str(clip_path), str(dst))
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Enfileirado para tratamento: {dst}")
    return dst


# ---- Watermark util (MoviePy v2) -------------------------------------------
# Posição corrigida para canto inferior direito
def add_image_watermark(
    input_path: str,
    watermark_path: str,
    output_path: str,
    margin: int = 24,
    opacity: float = 0.6,
    rel_width: float = 0.2,
    codec: str = "libx264",
    crf: int = 20,
    preset: str = "medium",
) -> None:
    """
    Aplica marca d'água de imagem no canto inferior direito usando ffmpeg.

    - Dimensiona a marca d'água para `rel_width * largura_do_vídeo`.
    - Aplica opacidade (canal alpha) e sobrepõe com margens.
    - Requer ffmpeg no PATH. Não requer MoviePy.
    """
    print("\n\nAdicionando marca d'água...\n\n")
    in_p = Path(input_path)
    wm_p = Path(watermark_path)
    if not in_p.exists():
        raise FileNotFoundError(f"Vídeo inexistente: {input_path}")
    if not wm_p.exists():
        raise FileNotFoundError(f"Watermark inexistente: {watermark_path}")

    meta = ffprobe_metadata(in_p)
    vw = int(meta.get("width") or 0)
    if vw <= 0:
        raise RuntimeError("Não foi possível obter largura do vídeo via ffprobe.")

    # Largura alvo da marca d'água (em pixels)
    wm_w = max(1, int(vw * float(rel_width)))
    # Filtro: escala watermark, aplica alpha e sobrepõe com margem
    # - format=rgba garante canal alpha; colorchannelmixer ajusta opacidade
    filt = (
        f"[1:v]scale={wm_w}:-1,format=rgba,colorchannelmixer=aa={float(opacity):.3f}[wm];"
        f"[0:v][wm]overlay=x=(main_w-overlay_w)/2:y=main_h-overlay_h-{int(margin)}[v]"
    )

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(in_p),
        "-i",
        str(wm_p),
        "-filter_complex",
        filt,
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        codec,
        "-preset",
        preset,
        "-crf",
        str(int(crf)),
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


# ---- Thumbnail helper (opcional) -------------------------------------------
def generate_thumbnail(
    input_path: Path, output_path: Path, at_sec: float | None = None
) -> None:
    """Gera thumbnail .jpg no meio do vídeo (ou em at_sec) usando ffmpeg."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Vídeo inexistente: {input_path}")

    meta = ffprobe_metadata(input_path)
    dur = float(meta.get("duration_sec") or 0.0)
    t = at_sec if at_sec is not None else (dur * 0.5 if dur > 0 else 0.0)

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-ss",
        f"{t:.3f}",
        "-i",
        str(input_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


# ---- HTTP helper & registration --------------------------------------------
def _http_post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()  # Lança HTTPError para respostas 4xx/5xx
        return response.json() if response.text else {}

    except requests.exceptions.HTTPError as e:
        try:
            body = e.response.text()
        except Exception:
            body = ""
        raise RuntimeError(
            f"HTTP {e.response.status_code} ao POST {url}: {body}"
        ) from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Erro de rede ao POST {url}: {e}") from e


def register_clip_metadados(
    api_base: str,
    metadados: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    Envia metadados do clipe para o backend e retorna o payload de resposta.

    Espera que o backend exponha POST {api_base}/api/videos/metadados.
    Se `token` for fornecido, envia como `Authorization: Bearer <token>`.
    """
    client_id = os.getenv("CLIENT_ID")
    venue_id = os.getenv("VENUE_ID")
    base = api_base.rstrip("/")
    token = os.getenv("GN_API_TOKEN") or os.getenv("API_TOKEN")

    url = f"{base}/api/videos/metadados/client/{client_id}/venue/{venue_id}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _http_post_json(url, metadados, headers=headers, timeout=timeout)


# ---- Signed URL upload ------------------------------------------------------
def upload_file_to_signed_url(
    upload_url: str,
    file_path: Path,
    content_type: str = "video/mp4",
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: float = 180.0,
) -> Tuple[int, str, Dict[str, str]]:
    """
    Envia o arquivo via HTTP PUT para uma URL assinada (S3/GCS/etc).
    Retorna (status_code, reason, response_headers). Lança exceção em erros de conexão.
    """
    try:
        with open(file_path, "rb") as f:
            headers = {"Content-Type": content_type}
            if extra_headers:
                headers.update(extra_headers)
            response = requests.put(
                upload_url, data=f, headers=headers, timeout=timeout
            )
            return response.status_code, response.reason, dict(response.headers)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"Erro de rede durante o upload para {upload_url}: {e}"
        ) from e


# ---- Finalize uploaded clip -------------------------------------------------
def finalize_clip_uploaded(
    api_base: str,
    clip_id: str,
    size_bytes: int,
    sha256: str,
    *,
    etag: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    Notifica o backend que o upload foi concluído e valida integridade.

    POST {api_base}/api/videos/{clip_id}/uploaded
    Body: { "size_bytes": number, "sha256": string }
    """
    base = api_base.rstrip("/")
    url = f"{base}/api/videos/{clip_id}/uploaded"
    token = os.getenv("GN_API_TOKEN") or os.getenv("API_TOKEN")

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload: Dict[str, Any] = {"size_bytes": int(size_bytes), "sha256": str(sha256)}
    if etag:
        payload["etag"] = etag
    return _http_post_json(url, payload, headers=headers, timeout=timeout)
