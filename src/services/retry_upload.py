"""
Auxilia no reenvio de uploads que ficaram na pasta de falhas (upload_failed).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Dict, Optional

from src.services.api_client import GravaNoisAPIClient
from src.services.api_error_policy import extract_api_error_from_exception
from src.utils.logger import logger
from src.video.processor import ffprobe_metadata, _sha256_file


DEFAULT_CONTENT_TYPE = "video/mp4"
SENSITIVE_RESPONSE_KEYS = {"upload_url", "signed_upload_url"}


def _sanitize_backend_response(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_RESPONSE_KEYS:
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_backend_response(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_backend_response(item) for item in value]
    return value


def _load_or_init_sidecar(video_path: Path, sidecar_path: Path) -> Dict:
    if sidecar_path.exists():
        try:
            return json.loads(sidecar_path.read_text())
        except Exception:
            pass

    payload = {
        "type": "highlight_raw",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_name": video_path.name,
        "size_bytes": video_path.stat().st_size,
        "sha256": None,
        "meta": ffprobe_metadata(video_path),
        "status": "upload_pending",
        "attempts": 0,
    }
    sidecar_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def retry_failed_uploads(
    failed_upload_dir: Path,
    api_client: Optional[GravaNoisAPIClient] = None,
    max_items: Optional[int] = None,
) -> Dict[str, int]:
    """
    Reenvia uploads dos videos em failed_upload_dir (ex.: failed_clips/upload_failed).

    Mantem os arquivos e o sidecar no local para debug. Atualiza o sidecar
    com status e respostas da API a cada etapa.
    """
    api_client = api_client or GravaNoisAPIClient()
    if not api_client.is_configured():
        logger.warning("API nao configurada; abortando retry de uploads")
        return {"processed": 0, "uploaded": 0, "failed": 0}

    processed = 0
    uploaded = 0
    failed = 0

    videos = list(failed_upload_dir.glob("*.mp4")) + list(
        failed_upload_dir.glob("*.ts")
    )

    for video_path in sorted(videos):
        if max_items is not None and processed >= max_items:
            break

        sidecar_path = failed_upload_dir / f"{video_path.stem}.json"
        meta = _load_or_init_sidecar(video_path, sidecar_path)

        attempts = int(meta.get("attempts", 0)) + 1
        meta["attempts"] = attempts
        meta["status"] = "retry_uploading"
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        processed += 1

        try:
            size_upload = video_path.stat().st_size
            sha256_upload = _sha256_file(video_path)
            meta_up = ffprobe_metadata(video_path)

            payload = {
                "venue_id": api_client.venue_id,
                "duration_sec": float(meta_up.get("duration_sec") or 0.0),
                "captured_at": meta.get("created_at"),
                "meta": meta_up,
                "sha256": sha256_upload,
            }

            logger.info(f"Retry: registrando metadados de {video_path.name}")
            resp = api_client.register_clip_metadados(payload, timeout=15.0)

            resp_data = (resp or {}).get("data") or {}
            meta.setdefault("remote_registration", {})
            meta["remote_registration"].update(
                {
                    "status": "registered",
                    "registered_at": datetime.now(timezone.utc).isoformat(),
                    "response": _sanitize_backend_response(resp),
                }
            )
            sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

            upload_url = resp_data.get("upload_url")
            if not upload_url:
                logger.warning(
                    f"Retry: sem upload_url para {video_path.name}; pulando"
                )
                meta.setdefault("remote_upload", {})
                meta["remote_upload"].update(
                    {
                        "status": "failed",
                        "reason": "no_upload_url",
                        "attempted_at": datetime.now(timezone.utc).isoformat(),
                        "file_size": size_upload,
                    }
                )
                sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                failed += 1
                continue

            logger.info(f"Retry: upload para URL assinada ({video_path.name})")
            status_code, reason, resp_headers = api_client.upload_file_to_signed_url(
                upload_url,
                video_path,
                content_type=DEFAULT_CONTENT_TYPE,
                extra_headers=None,
                timeout=180.0,
            )

            meta.setdefault("remote_upload", {})
            meta["remote_upload"].update(
                {
                    "status": "uploaded" if 200 <= status_code < 300 else "failed",
                    "http_status": status_code,
                    "reason": reason,
                    "attempted_at": datetime.now(timezone.utc).isoformat(),
                    "file_size": size_upload,
                }
            )
            sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

            if 200 <= status_code < 300:
                clip_id = resp_data.get("clip_id")
                if clip_id:
                    etag = None
                    try:
                        etag = (resp_headers or {}).get("etag")
                    except Exception:
                        etag = None

                    fin = api_client.finalize_clip_uploaded(
                        clip_id=clip_id,
                        size_bytes=size_upload,
                        sha256=sha256_upload,
                        etag=etag,
                        timeout=20.0,
                    )
                    meta.setdefault("remote_finalize", {})
                    meta["remote_finalize"].update(
                        {
                            "status": "ok",
                            "finalized_at": datetime.now(timezone.utc).isoformat(),
                            "response": fin,
                        }
                    )
                    meta["status"] = "uploaded"
                    sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                    uploaded += 1
                else:
                    logger.warning(
                        f"Retry: clip_id ausente na resposta para {video_path.name}"
                    )
                    failed += 1
            else:
                failed += 1

        except Exception as e:
            api_error = extract_api_error_from_exception(e)
            if api_error and api_error.should_delete_local_record:
                logger.warning(
                    "Retry: removendo registro local por erro nao-retriavel da API (%s)",
                    api_error.short_label(),
                )
                try:
                    video_path.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    sidecar_path.unlink(missing_ok=True)
                except Exception:
                    pass
                failed += 1
                continue

            logger.error(f"Retry: falha em {video_path.name}: {e}")
            meta.setdefault("remote_upload", {})
            meta["remote_upload"].update(
                {
                    "status": "failed",
                    "error": str(e),
                    "attempted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            meta["status"] = "upload_pending"
            sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            failed += 1

    return {"processed": processed, "uploaded": uploaded, "failed": failed}


def _parse_args() -> "argparse.Namespace":
    import argparse

    parser = argparse.ArgumentParser(
        description="Reenvia uploads que falharam (pasta upload_failed)."
    )
    parser.add_argument(
        "failed_upload_dir",
        type=Path,
        help="Diretorio com videos falhados (ex.: failed_clips/upload_failed)",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Limita a quantidade de itens processados",
    )
    return parser.parse_args()


def _cli() -> int:
    args = _parse_args()
    result = retry_failed_uploads(
        failed_upload_dir=args.failed_upload_dir,
        max_items=args.max_items,
    )
    logger.info(
        "Retry finalizado: processed=%s, uploaded=%s, failed=%s",
        result["processed"],
        result["uploaded"],
        result["failed"],
    )
    return 0 if result["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(_cli())
