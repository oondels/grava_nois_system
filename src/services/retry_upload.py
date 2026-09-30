"""
Auxilia no reenvio de uploads que ficaram na pasta de falhas (upload_failed).
"""

from __future__ import annotations

import argparse
import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from src.infrastructure.filesystem.deferred_repository import atomic_json
from src.services.api_client import GravaNoisAPIClient
from src.services.api_error_policy import extract_api_error_from_exception
from src.services.backend_response_sanitizer import sanitize_backend_response
from src.utils.logger import logger
from src.video.processor import _sha256_file, ffprobe_metadata

DEFAULT_CONTENT_TYPE = "video/mp4"


def _sanitize_backend_response(value):
    return sanitize_backend_response(value)


def _load_sidecar(sidecar_path: Path) -> dict:
    if not sidecar_path.is_file() or sidecar_path.is_symlink():
        raise ValueError("sidecar missing or unsafe")
    payload = json.loads(sidecar_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("invalid sidecar")
    if any(
        name in payload and not isinstance(payload[name], dict)
        for name in ("remote_registration", "remote_upload", "remote_finalize")
    ):
        raise ValueError("invalid remote checkpoint")
    return payload


def _confirmed_finalize_response(response: object, clip_id: str | None = None) -> bool:
    if (
        not isinstance(response, dict)
        or response.get("dry_run")
        or response.get("success") is False
    ):
        return False
    data = response.get("data", response)
    if not isinstance(data, dict):
        return False
    clip = data.get("clip", data)
    return (
        isinstance(clip, dict)
        and isinstance(clip.get("clip_id"), str)
        and (clip_id is None or clip["clip_id"] == clip_id)
        and clip.get("status") in {"uploaded", "uploaded_temp"}
    )


def _section(meta: dict, name: str) -> dict:
    value = meta.get(name)
    return value if isinstance(value, dict) else {}


def _finalize_confirmed(meta: dict) -> bool:
    registration = _section(meta, "remote_registration")
    upload = _section(meta, "remote_upload")
    finalize = _section(meta, "remote_finalize")
    return (
        meta.get("status") == "uploaded"
        and registration.get("status") == "registered"
        and upload.get("status") == "uploaded"
        and finalize.get("status") == "ok"
        and bool(finalize.get("finalized_at"))
        and _confirmed_finalize_response(finalize.get("response"))
    )


def _remove_confirmed(video_path: Path, sidecar_path: Path) -> bool:
    try:
        video_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)
        return True
    except OSError:
        logger.warning("Retry: limpeza de upload finalizado pendente")
        return False


def retry_failed_uploads(
    failed_upload_dir: Path,
    api_client: GravaNoisAPIClient | None = None,
    max_items: int | None = None,
) -> dict[str, int]:
    """
    Reenvia uploads dos videos em failed_upload_dir (ex.: failed_clips/upload_failed).

    Mantem pendencias para recuperacao; remove video e sidecar apenas depois de
    persistir uma confirmacao inequívoca de finalizacao.
    """
    api_client = api_client or GravaNoisAPIClient()
    configured = api_client.is_configured()
    if not configured:
        logger.warning("API nao configurada; apenas reconciliando finalizacoes locais")

    processed = 0
    uploaded = 0
    failed = 0

    videos = list(failed_upload_dir.glob("*.mp4")) + list(failed_upload_dir.glob("*.ts"))

    for video_path in sorted(videos):
        if max_items is not None and processed >= max_items:
            break

        sidecar_path = failed_upload_dir / f"{video_path.stem}.json"
        if video_path.is_symlink() or sidecar_path.is_symlink():
            failed += 1
            continue
        try:
            meta = _load_sidecar(sidecar_path)
        except (OSError, ValueError, TypeError):
            logger.warning("Retry: sidecar ausente ou invalido; reconciliacao manual necessaria")
            processed += 1
            failed += 1
            continue

        if _finalize_confirmed(meta):
            processed += 1
            if _remove_confirmed(video_path, sidecar_path):
                uploaded += 1
            else:
                failed += 1
            continue
        if _section(meta, "remote_upload").get("status") in {"uploaded", "uncertain"} or meta.get(
            "status"
        ) in {"uploaded", "retry_uploading"}:
            logger.warning(
                "Retry: upload anterior com finalizacao incerta; reconciliacao manual necessaria"
            )
            processed += 1
            failed += 1
            continue
        if not configured:
            processed += 1
            failed += 1
            continue

        attempts = int(meta.get("attempts", 0)) + 1
        meta["attempts"] = attempts
        meta["status"] = "retry_uploading"
        meta["updated_at"] = datetime.now(UTC).isoformat()
        try:
            atomic_json(sidecar_path, meta)
        except OSError:
            logger.warning("Retry: nao foi possivel persistir inicio da tentativa")
            processed += 1
            failed += 1
            continue

        processed += 1

        upload_started = False
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

            resp_clip = api_client.extract_clip_registration(resp or {})
            meta.setdefault("remote_registration", {})
            meta["remote_registration"].update(
                {
                    "status": "registered",
                    "registered_at": datetime.now(UTC).isoformat(),
                    "response": _sanitize_backend_response(resp),
                }
            )
            sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

            upload_url = resp_clip.get("upload_url")
            if not upload_url:
                logger.warning(f"Retry: sem upload_url para {video_path.name}; pulando")
                meta.setdefault("remote_upload", {})
                meta["remote_upload"].update(
                    {
                        "status": "failed",
                        "reason": "no_upload_url",
                        "attempted_at": datetime.now(UTC).isoformat(),
                        "file_size": size_upload,
                    }
                )
                meta["status"] = "upload_pending"
                sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                failed += 1
                continue

            logger.info(f"Retry: upload para URL assinada ({video_path.name})")
            upload_started = True
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
                    "attempted_at": datetime.now(UTC).isoformat(),
                    "file_size": size_upload,
                }
            )
            sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

            if 200 <= status_code < 300:
                clip_id = resp_clip.get("clip_id")
                if clip_id:
                    etag = None
                    try:
                        etag = (resp_headers or {}).get("etag")
                    except Exception:
                        etag = None

                    try:
                        fin = api_client.finalize_clip_uploaded(
                            clip_id=clip_id,
                            size_bytes=size_upload,
                            sha256=sha256_upload,
                            etag=etag,
                            timeout=20.0,
                        )
                    except Exception as error:
                        api_error = extract_api_error_from_exception(error)
                        if api_error and api_error.should_delete_local_record:
                            raise
                        meta.setdefault("remote_finalize", {})["status"] = "uncertain"
                        try:
                            atomic_json(sidecar_path, meta)
                        except OSError:
                            logger.warning("Retry: nao foi possivel persistir finalizacao incerta")
                        failed += 1
                        continue
                    if not _confirmed_finalize_response(fin, clip_id):
                        logger.warning("Retry: resposta de finalizacao sem confirmacao do clipe")
                        meta.setdefault("remote_finalize", {})["status"] = "uncertain"
                        try:
                            atomic_json(sidecar_path, meta)
                        except OSError:
                            logger.warning("Retry: nao foi possivel persistir finalizacao incerta")
                        failed += 1
                        continue
                    meta.setdefault("remote_finalize", {})
                    meta["remote_finalize"].update(
                        {
                            "status": "ok",
                            "finalized_at": datetime.now(UTC).isoformat(),
                            "response": fin,
                        }
                    )
                    meta["status"] = "uploaded"
                    try:
                        atomic_json(sidecar_path, meta)
                    except OSError:
                        logger.warning(
                            "Retry: finalizacao confirmada, mas recibo local nao persistiu"
                        )
                        failed += 1
                        continue
                    _remove_confirmed(video_path, sidecar_path)
                    uploaded += 1
                else:
                    logger.warning(f"Retry: clip_id ausente na resposta para {video_path.name}")
                    failed += 1
            else:
                meta["status"] = "upload_pending"
                sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                failed += 1

        except Exception as e:
            api_error = extract_api_error_from_exception(e)
            if api_error and api_error.should_delete_local_record:
                logger.warning(
                    "Retry: removendo registro local por erro nao-retriavel da API (%s)",
                    api_error.short_label(),
                )
                with suppress(OSError):
                    video_path.unlink(missing_ok=True)
                with suppress(OSError):
                    sidecar_path.unlink(missing_ok=True)
                failed += 1
                continue

            logger.error("Retry: falha em %s: %s", video_path.name, type(e).__name__)
            meta.setdefault("remote_upload", {})
            meta["remote_upload"].update(
                {
                    "status": "uncertain" if upload_started else "failed",
                    "error": api_error.short_label() if api_error else type(e).__name__,
                    "attempted_at": datetime.now(UTC).isoformat(),
                }
            )
            meta["status"] = "retry_uploading" if upload_started else "upload_pending"
            sidecar_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            failed += 1

    return {"processed": processed, "uploaded": uploaded, "failed": failed}


def _parse_args() -> argparse.Namespace:
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
