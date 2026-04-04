from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from src.config.settings import CaptureConfig
from src.utils.logger import logger
from src.video.buffer import SegmentBuffer

load_dotenv()


def build_highlight(cfg: CaptureConfig, segbuf: SegmentBuffer) -> Optional[Path]:
    logger.info("Botão apertado! Aguardando pós-buffer...")

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
    logger.info(f"{need} segmentos são necessários para o highlight")

    def _segnum_from_path(s):
        try:
            return int(Path(s).stem.replace("buffer", ""))
        except:
            return -1

    # Ultimos videos em buffer
    selected_videos = sorted(segbuf.snapshot_last(need), key=_segnum_from_path)
    logger.info(f"Total de segmentos selecionados: {len(selected_videos)}")

    if not selected_videos:
        logger.warning("Nenhum segmento capturado — encerrando")
        return None

    # Cria uma pasta de staging apenas para o arquivo de manifesto (concat list)
    timestamp = datetime.fromtimestamp(click_ts, tz=timezone.utc).strftime(
        "%Y%m%d-%H%M%S-%fZ"
    )

    # Usamos a pasta de clipes gravados para o arquivo de lista temporário
    cfg.clips_dir.mkdir(parents=True, exist_ok=True)
    concat_list_path = cfg.clips_dir / f"concat_{timestamp}.txt"

    valid_segments = [
        p
        for p in (Path(s) for s in selected_videos)
        if p.exists() and p.stat().st_size > 0
    ]
    if not valid_segments or len(valid_segments) < 2:
        logger.warning("Nenhum segmento válido encontrado — encerrando")
        return None

    with open(concat_list_path, "w") as f:
        for seg_path in valid_segments:
            f.write(f"file '{seg_path.resolve()}'\n")

    out_mp4 = cfg.clips_dir / f"highlight_{cfg.camera_id}_{timestamp}.mp4"
    out_tmp_mp4 = cfg.clips_dir / f"highlight_{cfg.camera_id}_{timestamp}.tmp.mp4"

    try:
        # Concat segmentos TS → MP4 direto com -c copy (sem re-encode).
        # Os segmentos já foram re-encoded na captura com error concealment,
        # então o stream H.264 está limpo. A concatenação só precisa:
        # - genpts: regenerar PTS caso algum segmento tenha descontinuidade
        # - ignore_err: tolerar eventuais resíduos de corrupção sem abortar
        # - avoid_negative_ts: normalizar base temporal para MP4
        # - faststart: moov atom no início para streaming progressivo
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-fflags",
                "+genpts",
                "-err_detect",
                "ignore_err",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-avoid_negative_ts",
                "make_zero",
                str(out_tmp_mp4),
            ],
            check=True,
        )

        out_tmp_mp4.replace(out_mp4)

        logger.info(f"Highlight salvo: {out_mp4}")
        return out_mp4

    except Exception as e:
        logger.exception(f"Falha ao construir highlight: {e}")

        # Move quaisquer saídas parciais para a pasta de falha
        try:
            if out_mp4.exists():
                out_mp4.replace(fail_build_dir / out_mp4.name)
        except Exception:
            pass
        try:
            if out_tmp_mp4.exists():
                out_tmp_mp4.replace(fail_build_dir / out_tmp_mp4.name)
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
        try:
            if "out_tmp_mp4" in locals() and out_tmp_mp4.exists():
                out_tmp_mp4.unlink(missing_ok=True)
        except Exception:
            pass



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
    """
    Move o arquivo para a fila (queue_dir) e salva metadados .json ao lado.
    """
    logger.info("Enfileirando clipe...")
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
    logger.info(f"Enfileirado para tratamento: {dst}")
    return dst


def add_image_watermark(
    input_path: str,
    watermark_path: str,
    output_path: str,
    secondary_watermark_path: Optional[str] = None,
    margin: int = 24,
    opacity: float = 0.8,
    rel_width: float = 0.2,
    secondary_rel_width: Optional[float] = None,
    codec: str = "libx264",
    crf: int = 20,
    preset: str = "medium",
    mobile_format: bool = True,
    vertical_format: bool = False,
) -> None:
    """
    Aplica marca d'água de imagem usando ffmpeg.

    - Dimensiona a(s) marca(s) d'água para `rel_width * largura_do_vídeo`.
    - Aplica opacidade (canal alpha) e sobrepõe com margens.
    - Se `mobile_format=True`: redimensiona para máx 720p horizontal.
    - Se `vertical_format=True`: recorta o centro do vídeo para 9:16 e entrega 1080x1920.
    - Em `vertical_format`, a marca d'água é posicionada no topo central dentro da safe zone.
    - Requer ffmpeg no PATH. Não requer MoviePy.
    """
    logger.info(
        f"Adicionando marca d'água ao vídeo "
        f"(mobile_format={mobile_format}, vertical_format={vertical_format})..."
    )
    in_p = Path(input_path)
    wm_p = Path(watermark_path)
    secondary_wm_p = Path(secondary_watermark_path) if secondary_watermark_path else None
    if not in_p.exists():
        raise FileNotFoundError(f"Vídeo inexistente: {input_path}")
    if not wm_p.exists():
        raise FileNotFoundError(f"Watermark inexistente: {watermark_path}")
    if secondary_wm_p is not None and not secondary_wm_p.exists():
        raise FileNotFoundError(
            f"Watermark secundária inexistente: {secondary_watermark_path}"
        )

    meta = ffprobe_metadata(in_p)
    vw = int(meta.get("width") or 0)
    vh = int(meta.get("height") or 0)
    if vw <= 0:
        raise RuntimeError("Não foi possível obter largura do vídeo via ffprobe.")

    # Constrói cadeia de filtros de transformação de vídeo (crop e/ou scale).
    # A ordem importa: crop sempre antes de scale.
    video_filters: list[str] = []

    if vertical_format:
        # Recorta o centro do vídeo para proporção 9:16.
        # crop=largura_alvo:altura:(x_offset):0
        # largura_alvo = altura * 9/16 (mantém altura, corta laterais)
        video_filters.append("crop=ih*9/16:ih:(iw-ih*9/16)/2:0")
        logger.info(f"Vertical format ativo: crop 9:16 — {vw}x{vh}")

    if vertical_format:
        video_filters.append("scale=1080:1920")
        logger.info("Vertical format ativo: scale final para 1080x1920")
    elif mobile_format:
        target_h = 720
        if vh > target_h:
            video_filters.append(f"scale=-2:{target_h}")
            logger.info(f"Mobile format ativo: scale para altura ≤{target_h}p")

    if video_filters:
        transform = ",".join(video_filters)
        input_video_label = "[v_transformed]"
        transform_filter = f"[0:v]{transform}[v_transformed]"
    else:
        input_video_label = "[0:v]"
        transform_filter = None

    # Calcula largura efetiva do vídeo após as transformações,
    # pois a watermark é dimensionada em relação à largura final.
    if vertical_format:
        vw_final = 1080
        vh_final = 1920
    elif mobile_format and vh > 720:
        # Scale horizontal: mantém aspect ratio, altura = 720
        vw_final = max(1, int(vw * 720 / vh))
        vh_final = 720
    else:
        vw_final = vw
        vh_final = vh

    # Largura alvo da(s) marca(s) d'água baseada na largura final do vídeo
    primary_rel_width = min(0.2, float(rel_width))
    secondary_base_rel = rel_width if secondary_rel_width is None else secondary_rel_width
    secondary_rel = min(0.2, float(secondary_base_rel))
    alpha = min(0.85, max(0.7, float(opacity)))
    wm_w = max(1, int(vw_final * primary_rel_width))
    wm2_w = max(1, int(vw_final * secondary_rel))
    overlay_y = (
        str(max(int(margin), int(vh_final * 0.08)))
        if vertical_format
        else f"main_h-overlay_h-{int(margin)}"
    )

    # Filtro base: watermark principal no topo central (vertical) ou rodapé (horizontal)
    filt_parts = []
    if transform_filter:
        filt_parts.append(transform_filter)
    filt_parts.append(
        f"[1:v]scale={wm_w}:-1,format=rgba,colorchannelmixer=aa={alpha:.3f}[wm1]"
    )

    # Se houver watermark secundária: logos lado a lado no mesmo eixo vertical
    final_video_label = "[v]"
    if secondary_wm_p is not None:
        pair_gap = max(8, int(margin) // 2)
        pair_total_w = int(wm_w) + int(wm2_w) + int(pair_gap)
        filt_parts.append(
            f"[2:v]scale={wm2_w}:-1,format=rgba,colorchannelmixer=aa={alpha:.3f}[wm2]"
        )
        filt_parts.append(
            (
                f"{input_video_label}[wm1]overlay="
                f"x=(main_w-{pair_total_w})/2:"
                f"y={overlay_y}[v1]"
            )
        )
        filt_parts.append(
            (
                f"[v1][wm2]overlay="
                f"x=(main_w-{pair_total_w})/2+{int(wm_w) + int(pair_gap)}:"
                f"y={overlay_y}[v]"
            )
        )
    else:
        filt_parts.append(
            f"{input_video_label}[wm1]overlay="
            f"x=(main_w-overlay_w)/2:y={overlay_y}[v]"
        )

    filt = ";".join(filt_parts)

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(in_p),
        "-i",
        str(wm_p),
    ]
    if secondary_wm_p is not None:
        cmd.extend(["-i", str(secondary_wm_p)])
    cmd.extend(
        [
        "-filter_complex",
        filt,
        "-map",
        final_video_label,
        "-map",
        "0:a?",
        "-c:v",
        codec,
        "-preset",
        preset,
        "-crf",
        str(int(crf)),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(output_path),
        ]
    )
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.error(
            f"Erro crítico no FFmpeg (Watermark):\nComando: {' '.join(cmd)}\nDetalhes:\n{e.stderr}"
        )
        raise RuntimeError(
            f"FFmpeg falhou ao aplicar marca d'água: {e.stderr[-200:]}"
        ) from e


def _sha256_file(p: Path, chunk: int = 1024 * 1024) -> str:
    """Calcula hash SHA-256 de um arquivo."""
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


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
