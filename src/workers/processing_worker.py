from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.services.api_client import GravaNoisAPIClient
from src.services.api_error_policy import extract_api_error_from_exception
from src.utils.logger import logger
from src.video.processor import (
    _sha256_file,
    ffprobe_metadata,
)


class ProcessingWorker:
    def __init__(
        self,
        queue_dir: Path,  # queue_raw/
        out_wm_dir: Path,  # highlights_wm/
        failed_dir_highlight: Path,  # failed_clips/
        watermark_path: Path,  # assets/logo.png
        scan_interval: float = 1,  # varredura a cada 1
        max_attempts: int = 3,
        wm_margin: int = 24,
        wm_opacity: float = 0.8,
        wm_rel_width: float = 0.1,
        light_mode: bool = True,  # Ativa o Light Mode (MVP)
        retry_failed: bool = True,
        retry_min_age_sec: float = 120.0,  # idade mínima do arquivo/sidecar p/ tentar de novo
        retry_backoff_base_sec: float = 30.0,
    ):
        self.queue_dir = queue_dir
        self.out_wm_dir = out_wm_dir
        self.failed_dir_highlight = failed_dir_highlight
        self.watermark_path = watermark_path
        self.scan_interval = scan_interval
        self.max_attempts = max_attempts
        self.wm_margin = wm_margin
        self.wm_opacity = wm_opacity
        self.wm_rel_width = wm_rel_width
        self.light_mode = light_mode
        self.retry_failed = retry_failed
        self.retry_min_age_sec = retry_min_age_sec
        self.retry_backoff_base_sec = retry_backoff_base_sec
        self._last_noapi_log = 0.0

        self._stop = threading.Event()
        self._t = None

        if not self.light_mode:
            self.out_wm_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir_highlight.mkdir(parents=True, exist_ok=True)

    def _scan_retry_failed(self):
        # diretórios candidatos a retry ( só com upload_failed)
        retry_dirs = [self.failed_dir_highlight / "upload_failed"]
        # futuramente incluir outros diretorios:
        # retry_dirs += [ self.failed_dir / "enqueue_failed", self.failed_dir / "build_failed" ]

        api_base = os.getenv("GN_API_BASE") or os.getenv("API_BASE_URL") or "";

        now = time.time()
        for rdir in retry_dirs:
            if not rdir.exists():
                continue

            # aceita .mp4 e .ts
            vids = list(rdir.glob("*.mp4")) + list(rdir.glob("*.ts"))
            for vid in sorted(vids):
                stem = vid.stem
                meta_path = rdir / f"{stem}.json"
                lock_path = rdir / f"{stem}.lock"

                # lock atômico para evitar corrida entre workers
                try:
                    fd = os.open(
                        str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                    )
                    os.close(fd)
                except FileExistsError:
                    continue

                try:
                    # sidecar mínimo, se faltar
                    if not meta_path.exists():
                        payload = {
                            "type": "highlight_raw",
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "file_name": vid.name,
                            "size_bytes": vid.stat().st_size,
                            "sha256": None,
                            "meta": ffprobe_metadata(vid),
                            "status": "upload_pending",
                            "attempts": 0,
                            "local_fallback": {
                                "status": "upload_pending",
                                "reason": "unknown",
                            },
                        }
                        meta_path.write_text(
                            json.dumps(payload, ensure_ascii=False, indent=2)
                        )

                    # carrega e decide retry
                    meta = json.loads(meta_path.read_text())
                    attempts = int(meta.get("attempts", 0))
                    status = meta.get("status") or (
                        meta.get("local_fallback") or {}
                    ).get("status", "")

                    # 1) respeita limite de tentativas
                    if attempts >= self.max_attempts:
                        # já excedeu; deixa no diretório de falhas
                        continue

                    # 2) evita thrash sem API configurada
                    if not api_base:
                        # loga esporadicamente para não poluir stdout
                        if now - self._last_noapi_log > 15:
                            logger.warning(
                                "GN_API_BASE ausente — ignorando retries por enquanto"
                            )
                            self._last_noapi_log = now
                        continue

                    # 3) aguarda idade mínima / backoff
                    #    usa mtime do sidecar como referência
                    age_sec = now - meta_path.stat().st_mtime
                    backoff_need = self.retry_backoff_base_sec * (1 + attempts)
                    if age_sec < max(self.retry_min_age_sec, backoff_need):
                        # ainda "verde" para tentar de novo
                        continue

                    # 4) só reprocessa estados elegíveis
                    if status not in {
                        "upload_pending",
                        "queued_retry",
                        "watermarked",
                        "ready_for_upload",
                    }:
                        # estado final ou não relacionado a upload pendente
                        continue

                    # 5) marca tentativa e dispara o mesmo pipeline
                    meta["attempts"] = attempts + 1
                    meta["status"] = "queued_retry"
                    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
                    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

                    logger.info(
                        f"Reprocessando {vid.name} (tentativa {meta['attempts']}/{self.max_attempts})"
                    )
                    self._process_one(vid, meta_path)

                except Exception as e:
                    logger.error(f"Falha ao reprocessar {vid.name}: {e}")
                    # reaproveita tratamento padrão de falhas
                    self._handle_failure(vid, meta_path, e)
                finally:
                    try:
                        lock_path.unlink(missing_ok=True)
                    except Exception:
                        pass

    def start(self):
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self, timeout: float = 2.0):
        self._stop.set()
        if self._t:
            self._t.join(timeout=timeout)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._scan_once()

                if self.retry_failed:
                    self._scan_retry_failed()
            except Exception as e:
                # loga e continua o loop
                logger.exception(f"Erro inesperado no loop do worker: {e}")
            self._stop.wait(self.scan_interval)

    def _scan_once(self):
        # ----------------------------------------------------------------------------
        # Varre a pasta de fila (self.queue_dir) em busca de vídeos para processar e
        # despacha cada item com segurança entre múltiplos workers.
        #
        # O QUE FAZ (alto nível)
        # - Lista todos os arquivos "*.mp4" na fila.
        # - Garante a existência do sidecar JSON (metadados) para cada vídeo; se não
        #   existir, cria um mínimo (tipo, tamanho, sha256=None, metadados via ffprobe).
        # - Faz um *lock* atômico por arquivo (".lock") para impedir que outro worker
        #   processe o mesmo item em paralelo.
        # - Chama _process_one(mp4, meta_path) para executar o pipeline
        #   (watermark/thumbnail no modo completo, registro no backend, upload com URL
        #   assinada, finalize). Em exceções, delega a _handle_failure(...).
        # - Remove o ".lock" no bloco `finally`, garantindo liberação do item.
        #
        # CONCORRÊNCIA / LOCK
        # - O lock é implementado criando o arquivo "<stem>.lock" com flags
        #   O_CREAT|O_EXCL. Se já existir, outro worker está processando; este worker
        #   ignora o item.
        # - O lock é sempre removido no `finally`, evitando deadlocks mesmo em erros.
        #
        # SIDE-CAR JSON
        # - Arquivo: "<stem>.json" na mesma pasta da fila.
        # - Criado automaticamente se ausente, contendo:
        #     type="highlight_raw", created_at, file_name, size_bytes, sha256=None,
        #     meta=(ffprobe do vídeo), status="queued", attempts=0.
        #
        # ERROS E RETENTATIVAS
        # - Exceções em _process_one(...) são capturadas, logadas e encaminhadas para
        #   _handle_failure(...), que incrementa `attempts` e decide entre refileirar
        #   com backoff ou mover para "failed/" quando exceder `max_attempts`.
        #
        # EFEITOS COLATERAIS (efeitos esperados do pipeline chamado)
        # - No sucesso, o vídeo pode ser removido da fila (após upload concluído) ou
        #   movido para pastas de pendência/erro conforme o status do upload/registro.
        # - O sidecar é atualizado continuamente com os campos:
        #   remote_registration, remote_upload, remote_finalize, status, attempts, etc.
        #
        # ASSUNÇÕES/OBSERVAÇÕES
        # - Esta função apenas DESCOBRE e REIVINDICA jobs; o trabalho pesado está em
        #   _process_one(...).
        # - Filtra somente "*.mp4". Se a fila também puder conter ".ts", adapte o glob
        #   conforme necessário (ex.: "*.mp4" + "*.ts").
        # - Requer que ffprobe/ffmpeg estejam no PATH (indiretamente, via _process_one
        #   e ffprobe_metadata).
        # - Não retorna valor; processa N itens por chamada.
        # ---------------------------------------------------------------------------
        # procura .mp4 na fila (queue_dir/)
        for mp4 in sorted(self.queue_dir.glob("*.mp4")):
            stem = mp4.stem
            meta_path = self.queue_dir / f"{stem}.json"
            lock_path = self.queue_dir / f"{stem}.lock"

            # exige sidecar
            if not meta_path.exists():
                payload = {
                    "type": "highlight_raw",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "file_name": mp4.name,
                    "size_bytes": mp4.stat().st_size,
                    "sha256": None,
                    "meta": ffprobe_metadata(mp4),
                    "status": "queued",
                    "attempts": 0,
                }
                meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

            # Em modo DEV, itens já preservados localmente não devem ser reprocessados.
            try:
                existing_meta = json.loads(meta_path.read_text())
                if existing_meta.get("status") == "dev_local_preserved":
                    continue
            except Exception:
                pass

            # tenta lock atômico (claim do job)
            try:
                fd = os.open(
                    str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                )
                os.close(fd)
            except FileExistsError:
                continue

            try:
                self._process_one(mp4, meta_path)
            except Exception as e:
                logger.error(f"Falha ao processar {mp4.name}: {e}")
                self._handle_failure(mp4, meta_path, e)
            finally:
                # libera o lock (remove .lock)
                try:
                    lock_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _process_one(self, mp4: Path, meta_path: Path):
        # carrega/atualiza sidecar
        meta = json.loads(meta_path.read_text())
        attempts = int(meta.get("attempts", 0))

        # idempotência e pré-processamento conforme modo
        upload_target = mp4
        out_mp4 = None

        if not self.light_mode:
            # O highlight já deve chegar watermarked da etapa de build.
            out_mp4 = self.out_wm_dir / mp4.name
            if not out_mp4.exists():
                # Mantém escrita atômica do artefato watermarked sem nova transcodificação.
                tmp_out = self.out_wm_dir / f"{mp4.stem}.wm_tmp.mp4"
                shutil.copy2(mp4, tmp_out)
                tmp_out.replace(out_mp4)

            meta.update(
                {
                    "status": "watermarked",
                    "attempts": attempts,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "wm_path": str(out_mp4),
                    "meta_wm": ffprobe_metadata(out_mp4),
                }
            )
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            upload_target = out_mp4
        else:
            # Modo leve: sem watermark/thumbnail — upload do arquivo da fila
            meta.update(
                {
                    "status": "ready_for_upload",
                    "attempts": attempts,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "meta_raw": meta.get("meta") or ffprobe_metadata(mp4),
                }
            )
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        # Faz verificação se esta em ambiente de desenvolvimento
        is_dev = os.getenv("DEV", "").strip().lower() in {"true", "1", "yes"}

        # 3.1) registra intenção de upload no backend (POST /api/videos/metadados)
        api_client = GravaNoisAPIClient()

        file_size_mb = upload_target.stat().st_size / (1024 * 1024)
        logger.info(f"Tamanho do arquivo: {file_size_mb:.2f} MB")

        if is_dev:
            logger.info(
                "Modo DEV ativado. Pulando comunicação com a API e upload para a nuvem."
            )
            meta.setdefault("remote_registration", {})
            meta["remote_registration"].update(
                {
                    "status": "skipped",
                    "reason": "DEV mode",
                }
            )
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        if not is_dev and api_client.is_configured():
            try:  # Tenta fazer o registro com o servidor
                size_upload = upload_target.stat().st_size
                sha256_upload = _sha256_file(upload_target)
                meta_up = ffprobe_metadata(upload_target)

                # Verifica se já registramos os metadados antes (Retentativa)
                already_uploaded = (
                    meta.get("remote_upload", {}).get("status") == "uploaded"
                )
                already_finalized = (
                    meta.get("remote_finalize", {}).get("status") == "ok"
                )

                # ---------------------------------------------------------
                # 1) REGISTRO DE METADADOS (Pula se já fez upload AWS antes)
                # ---------------------------------------------------------
                if already_uploaded:
                    logger.info(
                        "Vídeo já enviado à AWS anteriormente. Reutilizando metadados locais para finalizar."
                    )
                    resp = meta.get("remote_registration", {}).get("response", {})
                else:
                    # Verifica se o modo de envio de vídeo esta em desenvolvimento
                    is_dev_video_mode = os.getenv(
                        "DEV_VIDEO_MODE", ""
                    ).strip().lower() in {
                        "true",
                        "1",
                        "yes",
                    }
                    payload = {
                        "venue_id": api_client.venue_id,
                        "duration_sec": float(meta_up.get("duration_sec") or 0.0),
                        "captured_at": meta.get("created_at"),
                        "meta": meta_up,
                        "sha256": sha256_upload,
                        "dev": is_dev_video_mode,
                    }

                    # Enviando registro de metadados ao backend..
                    resp = api_client.register_clip_metadados(payload, timeout=15.0)

                    # Aguarda Resposta do Backend
                    logger.debug(f"Resposta do backend: {json.dumps(resp)[:300]}")
                    meta.setdefault("remote_registration", {})
                    meta["remote_registration"].update(
                        {
                            "status": "registered",
                            "registered_at": datetime.now(timezone.utc).isoformat(),
                            "response": resp,
                        }
                    )
                    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

                resp_data = (resp or {}).get("data") or {}
                resp_clip = resp_data.get("clip") or {}
                clip_id = resp_clip.get("clip_id")
                logger.info(f"Registro remoto OK: clip_id={clip_id}")

                # ---------------------------------------------------------
                # 2) UPLOAD PARA AWS S3 (Pula se já foi feito com sucesso)
                # ---------------------------------------------------------
                upload_url = resp_clip.get("upload_url")
                if upload_url and not already_uploaded:
                    logger.info("Iniciando upload para URL assinada (AWS S3)")
                    t0 = time.time()
                    try:
                        status_code, reason, resp_headers = (
                            api_client.upload_file_to_signed_url(
                                upload_url,
                                upload_target,
                                content_type="video/mp4",
                                extra_headers=None,
                                timeout=180.0,
                            )
                        )
                        dt_ms = int((time.time() - t0) * 1000)

                        meta.setdefault("remote_upload", {})
                        meta["remote_upload"].update(
                            {
                                "status": (
                                    "uploaded" if 200 <= status_code < 300 else "failed"
                                ),
                                "http_status": status_code,
                                "reason": reason,
                                "attempted_at": datetime.now(timezone.utc).isoformat(),
                                "duration_ms": dt_ms,
                                "file_size": size_upload,
                                "etag": (resp_headers or {}).get("etag"),
                            }
                        )
                        meta_path.write_text(
                            json.dumps(meta, ensure_ascii=False, indent=2)
                        )
                        logger.info(
                            f"Upload finalizado: HTTP {status_code} {reason} em {dt_ms} ms"
                        )

                    except Exception as e:
                        dt_ms = int((time.time() - t0) * 1000)
                        meta.setdefault("remote_upload", {})
                        meta["remote_upload"].update(
                            {
                                "status": "failed",
                                "error": str(e),
                                "attempted_at": datetime.now(timezone.utc).isoformat(),
                                "duration_ms": dt_ms,
                                "file_size": size_upload,
                            }
                        )
                        meta_path.write_text(
                            json.dumps(meta, ensure_ascii=False, indent=2)
                        )
                        logger.error(f"Upload falhou: {e}")

                elif not upload_url and not already_uploaded:
                    logger.warning("Nenhuma upload_url na resposta; pulando upload")

                # ---------------------------------------------------------
                # 3) FINALIZAÇÃO NO BACKEND (Validação S3/Database) - (validação de integridade)
                # ---------------------------------------------------------
                # Atualizamos a variável com o estado após o passo 2
                is_currently_uploaded = (
                    meta.get("remote_upload", {}).get("status") == "uploaded"
                )
                if is_currently_uploaded and not already_finalized:
                    clip_id = resp_clip.get("clip_id", None)
                    if clip_id:
                        try:
                            saved_etag = meta.get("remote_upload", {}).get("etag")

                            fin = api_client.finalize_clip_uploaded(
                                clip_id=clip_id,
                                size_bytes=size_upload,
                                sha256=sha256_upload,
                                etag=saved_etag,
                                timeout=20.0,
                            )
                            meta.setdefault("remote_finalize", {})
                            meta["remote_finalize"].update(
                                {
                                    "status": "ok",
                                    "finalized_at": datetime.now(
                                        timezone.utc
                                    ).isoformat(),
                                    "response": fin,
                                }
                            )
                            meta_path.write_text(
                                json.dumps(meta, ensure_ascii=False, indent=2)
                            )
                            logger.info("Finalização confirmada pelo backend")
                        except Exception as e:
                            logger.error(f"Falha ao finalizar upload no backend: {e}")
                            meta.setdefault("remote_finalize", {})
                            meta["remote_finalize"].update(
                                {
                                    "status": "failed",
                                    "error": str(e),
                                    "attempted_at": datetime.now(
                                        timezone.utc
                                    ).isoformat(),
                                }
                            )
                            meta_path.write_text(
                                json.dumps(meta, ensure_ascii=False, indent=2)
                            )

            except Exception as e:
                def _delete_blocked_clip(log_reason: str):
                    logger.warning(f"{log_reason} Arquivo será excluído: {mp4.name}")
                    try:
                        upload_target.unlink(missing_ok=True)
                    except Exception:
                        pass
                    if upload_target != mp4:
                        try:
                            mp4.unlink(missing_ok=True)
                        except Exception:
                            pass
                    try:
                        meta_path.unlink(missing_ok=True)
                    except Exception:
                        pass

                api_error = extract_api_error_from_exception(e)
                if api_error and api_error.should_delete_local_record:
                    _delete_blocked_clip(
                        "Registro removido por erro não-retriável da API "
                        f"({api_error.short_label()})."
                    )
                    return

                http_response = None
                if isinstance(e, requests.exceptions.HTTPError):
                    http_response = getattr(e, "response", None)
                else:
                    cause = getattr(e, "__cause__", None)
                    while cause is not None:
                        if isinstance(cause, requests.exceptions.HTTPError):
                            http_response = getattr(cause, "response", None)
                            break
                        cause = getattr(cause, "__cause__", None)

                if http_response is not None:
                    err_code = ""
                    err_message = ""
                    try:
                        err_payload = http_response.json()
                    except ValueError:
                        err_payload = {}
                    except Exception:
                        err_payload = {}

                    if isinstance(err_payload, dict):
                        err_obj = err_payload.get("error")
                        if isinstance(err_obj, dict):
                            err_code = str(err_obj.get("code") or "").strip()
                            err_message = str(err_obj.get("message") or "").strip()
                        if not err_message:
                            err_message = str(
                                err_payload.get("message")
                                or err_payload.get("detail")
                                or ""
                            ).strip()

                    if not err_message:
                        try:
                            err_message = str(http_response.text or "").strip()
                        except Exception:
                            err_message = ""

                    msg_l = err_message.lower()
                    code_is_time_window = (
                        err_code == "request_outside_allowed_time_window"
                    )
                    msg_is_time_window = any(
                        token in msg_l
                        for token in (
                            "request_outside_allowed_time_window",
                            "outside allowed time window",
                            "outside the allowed time window",
                            "allowed time window",
                            "business hours",
                            "horário comercial",
                            "horario comercial",
                            "fora do horário",
                            "fora do horario",
                        )
                    )

                    if (
                        http_response.status_code == 403
                        and (code_is_time_window or msg_is_time_window)
                    ):
                        _delete_blocked_clip("Upload rejeitado por horário.")
                        return

                    msg_is_reupload_blocked = any(
                        token in msg_l
                        for token in (
                            "transição inválida para reupload",
                            "transicao invalida para reupload",
                            "invalid transition for reupload",
                        )
                    )
                    code_is_reupload_conflict = (
                        http_response.status_code == 409
                        and err_code.strip().upper() == "CONFLICT"
                        and "reupload" in msg_l
                    )

                    if http_response.status_code == 409 and (
                        msg_is_reupload_blocked or code_is_reupload_conflict
                    ):
                        _delete_blocked_clip(
                            "Upload bloqueado pelo backend (reupload não permitido)."
                        )
                        return

                raw_error_l = str(e).lower()
                if (
                    "http 409" in raw_error_l
                    and (
                        "transição inválida para reupload" in raw_error_l
                        or "transicao invalida para reupload" in raw_error_l
                        or "invalid transition for reupload" in raw_error_l
                    )
                ):
                    _delete_blocked_clip(
                        "Upload bloqueado pelo backend (reupload não permitido)."
                    )
                    return

                logger.error(f"Registro remoto falhou: {e}")
                meta.setdefault("remote_registration", {})
                meta["remote_registration"].update(
                    {
                        "status": "failed",
                        "error": str(e),
                        "attempted_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        elif not is_dev:
            logger.warning("API não configurada, pulando registro remoto")
            # sem configuração de API, apenas registra um hint no sidecar
            meta.setdefault("remote_registration", {})
            meta["remote_registration"].update(
                {
                    "status": "skipped",
                    "reason": "GN_API_BASE ausente",
                }
            )
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        # 4) pós-processamento local conforme sucesso/fracasso de upload
        #    - Se DEV: mantém artefatos locais (sem apagar)
        #    - Se upload OK: remove o original da fila
        #    - Se upload NÃO OK (falhou, sem URL, sem API): move para pasta de pendências
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            meta = {}

        uploaded_ok = (
            isinstance(meta.get("remote_upload"), dict)
            and meta["remote_upload"].get("status") == "uploaded"
        )
        finalized_ok = (
            isinstance(meta.get("remote_finalize"), dict)
            and meta["remote_finalize"].get("status") == "ok"
        )

        if is_dev:
            meta.setdefault("local_fallback", {})
            meta["local_fallback"].update(
                {
                    "status": "dev_local_preserved",
                    "reason": "DEV mode",
                    "kept_at": datetime.now(timezone.utc).isoformat(),
                    "dest_dir": str(self.queue_dir),
                }
            )
            meta["status"] = "dev_local_preserved"
            meta["updated_at"] = datetime.now(timezone.utc).isoformat()
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            logger.info(
                f"Modo DEV: artefatos locais preservados para o clipe {mp4.name}."
            )
            return

        if uploaded_ok and finalized_ok:
            # remove artefatos da fila E o arquivo com watermark final
            logger.info(f"Limpando artefatos locais para o clipe {mp4.name}...")
            for file_to_delete in [mp4, meta_path, upload_target]:
                try:
                    if file_to_delete and file_to_delete.exists():
                        file_to_delete.unlink()
                except Exception as e:
                    logger.warning(
                        f"Aviso: Não foi possível apagar o artefato {file_to_delete}: {e}"
                    )
        else:
            # Cria pasta para pendências de upload dentro de failed_clips/
            pend_dir = self.failed_dir_highlight / "upload_failed"
            pend_dir.mkdir(parents=True, exist_ok=True)

            # Determina motivo para log/sidecar
            reason = "unknown"
            if not (os.getenv("API_BASE_URL")):
                reason = "no_api_configured"
            elif (
                isinstance(meta.get("remote_registration"), dict)
                and meta["remote_registration"].get("status") != "registered"
            ):
                reason = "registration_failed"
            elif not isinstance(meta.get("remote_upload"), dict):
                reason = "no_upload_url"
            else:
                reason = meta.get("remote_upload", {}).get("status") or "upload_failed"

            # Atualiza sidecar com status de pendência
            try:
                meta.setdefault("local_fallback", {})
                meta["local_fallback"].update(
                    {
                        "status": "upload_pending",
                        "reason": reason,
                        "moved_at": datetime.now(timezone.utc).isoformat(),
                        "dest_dir": str(pend_dir),
                    }
                )
                meta["status"] = "upload_pending"
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            except Exception:
                pass

            # Decide qual arquivo preservar: prioriza o arquivo realmente usado no upload
            file_to_preserve = None
            try:
                # Se existiu arquivo processado (watermarked) use-o, senão o da fila
                if not self.light_mode:
                    cand = self.out_wm_dir / mp4.name
                    if cand.exists():
                        file_to_preserve = cand
                if file_to_preserve is None:
                    file_to_preserve = mp4
            except Exception:
                file_to_preserve = mp4

            # Move o vídeo preservado e o sidecar para a pasta de pendências
            try:
                dst_vid = pend_dir / file_to_preserve.name
                if file_to_preserve.resolve() != dst_vid.resolve():
                    shutil.move(str(file_to_preserve), str(dst_vid))
            except Exception as e:
                logger.warning(
                    f"Falha ao mover vídeo para pendências: {file_to_preserve} - {e}"
                )
            try:
                dst_json = pend_dir / meta_path.name
                if meta_path.resolve() != dst_json.resolve():
                    # meta_path.replace(dst_json)
                    shutil.move(str(meta_path), str(dst_json))
            except Exception as e:
                logger.warning(
                    f"Falha ao mover sidecar para pendências: {meta_path} - {e}"
                )
            # Garante limpeza da fila para não reprocessar
            try:
                if mp4.exists():
                    mp4.unlink()
            except Exception:
                pass

    def _handle_failure(self, mp4: Path, meta_path: Path, err: Exception):
        # incrementa tentativas e decide o que fazer
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            meta = {"attempts": 0, "status": "queued"}

        meta["attempts"] = int(meta.get("attempts", 0)) + 1
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        meta["last_error"] = str(err)

        if meta["attempts"] >= self.max_attempts:
            meta["status"] = "failed"
            # move para failed/
            fail_mp4 = self.failed_dir_highlight / mp4.name
            fail_json = self.failed_dir_highlight / meta_path.name
            # grava erro detalhado
            err_path = self.failed_dir_highlight / (mp4.stem + ".error.txt")
            err_path.write_text(traceback.format_exc())

            try:
                # mp4.replace(fail_mp4)
                shutil.move(str(mp4), str(fail_mp4))
            except FileNotFoundError:
                pass
            try:
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            except Exception:
                pass
            try:
                # meta_path.replace(fail_json)
                shutil.move(str(meta_path), str(fail_json))
            except FileNotFoundError:
                pass
        else:
            # volta para fila com backoff (deixa lá p/ próxima rodada)
            meta["status"] = "queued_retry"
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            time.sleep(1.0 * meta["attempts"])  # backoff linear simples
