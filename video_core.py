from __future__ import annotations
import os, re, json, time, hashlib, subprocess, threading, platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from typing import Deque, List, Optional, Tuple, Dict, Any
import urllib.request
import urllib.error
import ssl
import http.client
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()


# ---- CONFIG & TYPES ---------------------------------------------------------
@dataclass
class CaptureConfig:
    buffer_dir: Path
    clips_dir: Path  # onde o highlight nasce
    queue_dir: Path  # fila para tratamento posterior (raw)
    device: str = "/dev/video0"
    seg_time: int = 1
    pre_seconds: int = 25
    post_seconds: int = 5
    scan_interval: float = 0.5
    max_buffer_seconds: int = 40

    @property
    def max_segments(self) -> int:
        return max(1, int(self.max_buffer_seconds / self.seg_time))

    def ensure_dirs(self) -> None:
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.queue_dir.mkdir(parents=True, exist_ok=True)


# ---- FFmpeg recorder --------------------------------------------------------
def _calc_start_number(buffer_dir: Path) -> int:
    pattern = re.compile(r"buffer(\d{3,})\.mp4$")
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
    out_pattern = str(cfg.buffer_dir / "buffer%06d.mp4")
    # Old -> Camera do notebook
    # ffmpeg_cmd = [
    #     "ffmpeg",
    #     "-nostdin",
    #     "-f",
    #     "v4l2",
    #     "-i",
    #     cfg.device,
    #     "-c:v",
    #     "libx264",
    #     "-preset",
    #     "ultrafast",
    #     "-tune",
    #     "zerolatency",
    #     "-force_key_frames",
    #     f"expr:gte(t,n_forced*{cfg.seg_time})",
    #     "-f",
    #     "segment",
    #     "-segment_time",
    #     str(cfg.seg_time),
    #     "-segment_start_number",
    #     str(start_num),
    #     "-reset_timestamps",
    #     "1",
    #     out_pattern,
    # ]

    # Camera Dedicada
    ffmpeg_cmd = [
        "ffmpeg",
        "-rtsp_transport",
        "tcp",
        "-i",
        "rtsp://admin:wa0i4Ochu@192.168.68.104:554/cam/realmonitor?channel=1&subtype=0",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-force_key_frames",
        "expr:gte(t,n_forced*1)",
        "-an",
        "-f",
        "segment",
        "-segment_time",
        str(cfg.seg_time),
        "-segment_start_number",
        str(start_num),
        "-reset_timestamps",
        "1",
        out_pattern,
    ]

    return subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


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
            files = sorted(
                self.cfg.buffer_dir.glob("buffer*.mp4"), key=lambda p: p.stat().st_mtime
            )
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
    click_ts = time.time()
    print("Botão apertado! Aguardando pós-buffer…")
    time.sleep(max(0, cfg.post_seconds) + 0.30)

    need = max(1, int(round((cfg.pre_seconds + cfg.post_seconds) / cfg.seg_time)))
    selected = segbuf.snapshot_last(need)
    if not selected:
        print("Nenhum segmento disponível — encerrando.")
        return None

    # Move os segmentos correspondentes para a pasta dedicada e limpa o buffer
    target_dir = Path(__file__).resolve().parent / "buffered_seguiments_post_clique"
    target_dir.mkdir(parents=True, exist_ok=True)

    moved_paths: List[Path] = []
    for seg in selected:
        src = Path(seg)
        if not src.exists():
            continue
        dst = target_dir / src.name
        try:
            src.replace(dst)  # move (substitui se já existir)
            moved_paths.append(dst)
        except Exception:
            # Falha ao mover este segmento — segue para o próximo
            pass

    # Exclui todos os arquivos do diretório de buffer original (/tmp/recorded_videos)
    try:
        for p in cfg.buffer_dir.glob("*"):
            try:
                if p.is_file():
                    p.unlink()
            except Exception:
                pass
    except Exception:
        pass

    if not moved_paths:
        print("Nenhum segmento movido — encerrando.")
        return None

    # Cria a lista de concat a partir dos arquivos movidos
    list_txt = target_dir / f"to_concat_{int(click_ts)}.txt"
    with open(list_txt, "w") as f:
        for p in moved_paths:
            f.write(f"file '{str(p)}'\n")

    out = (
        cfg.clips_dir
        / f"highlight_{datetime.fromtimestamp(click_ts, tz=timezone.utc).strftime('%Y%m%d-%H%M%SZ')}.mp4"
    )
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_txt),
                "-c",
                "copy",
                str(out),
            ],
            check=True,
        )
        print(f"Saved {out}")
        return out
    finally:
        # Limpa arquivos temporários: lista de concat e segmentos movidos
        try:
            list_txt.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        try:
            for p in moved_paths:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
        except Exception:
            pass


# ---- Queueing & metadata ----------------------------------------------------
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
        "stream=codec_name,width,height,r_frame_rate",
        "-show_entries",
        "format=duration",
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
        "status": "queued",
    }

    dst = cfg.queue_dir / clip_path.name
    meta_path = cfg.queue_dir / (clip_path.stem + ".json")

    # move para a fila e grava sidecar
    clip_path.replace(dst)
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
        f"[0:v][wm]overlay=x=main_w-overlay_w-{int(margin)}:y=main_h-overlay_h-{int(margin)}"
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
        "0:v:0",
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
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    # permissivo para ambientes com certificados locais
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset)
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {e.code} ao POST {url}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Erro de rede ao POST {url}: {e}")


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
    client_id = os.getenv("GN_CLIENT_ID") or os.getenv("CLIENT_ID")
    venue_id = os.getenv("GN_VENUE_ID") or os.getenv("VENUE_ID")
    base = api_base.rstrip("/")

    url = f"{base}/api/videos/metadados/client/{client_id}/venue/{venue_id}"
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _http_post_json(url, metadados, headers=headers, timeout=timeout)


# ---- Signed URL upload ------------------------------------------------------
def upload_file_to_signed_url(
    upload_url: str,
    file_path: Path,
    content_type: str = "video/mp4",
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: float = 120.0,
) -> Tuple[int, str, Dict[str, str]]:
    """
    Envia o arquivo via HTTP PUT para uma URL assinada (S3/GCS/etc).

    Retorna (status_code, reason). Lança exceção em erros de conexão.
    """
    parsed = urlparse(upload_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL inválida: {upload_url}")

    # Prepara conexão
    conn_cls = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    netloc = parsed.netloc
    path_qs = parsed.path or "/"
    if parsed.query:
        path_qs += f"?{parsed.query}"

    file_size = file_path.stat().st_size

    # Debug básico
    print(f"[upload] URL: {parsed.scheme}://{parsed.netloc}{parsed.path}...")
    print(f"[upload] Tamanho: {file_size} bytes | Tipo: {content_type}")

    headers = {
        "Content-Type": content_type,
        "Content-Length": str(file_size),
    }
    if extra_headers:
        headers.update(extra_headers)

    conn = conn_cls(netloc, timeout=timeout)
    try:
        conn.putrequest("PUT", path_qs)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()

        with file_path.open("rb") as f:
            # Envia em blocos para evitar alto uso de memória
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)

        resp = conn.getresponse()
        # Debug de resposta
        print(f"[upload] HTTP {resp.status} {resp.reason}")
        try:
            body = resp.read(512)
            if body:
                print(f"[upload] Resumo corpo: {body[:200]!r}")
        except Exception:
            pass
        # Normaliza headers em minúsculo para conveniência
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp.reason, resp_headers
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload: Dict[str, Any] = {"size_bytes": int(size_bytes), "sha256": str(sha256)}
    if etag:
        payload["etag"] = etag
    return _http_post_json(url, payload, headers=headers, timeout=timeout)
