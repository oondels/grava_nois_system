from __future__ import annotations
import sys, shutil
from pathlib import Path
import os, json, time, traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import threading
import queue
from video_core import (
    CaptureConfig,
    SegmentBuffer,
    start_ffmpeg,
    build_highlight,
    enqueue_clip,
    add_image_watermark,
    ffprobe_metadata,
    _sha256_file,
)
from src.utils.logger import logger
from src.services.api_client import GravaNoisAPIClient
from dotenv import load_dotenv

load_dotenv()


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
        wm_opacity: float = 0.4,
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

        api_base = os.getenv("API_BASE_URL")

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
        # Varre a pasta de fila (self.queue_dir) em busca de vídeos a processar e
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
            # idempotência simples: se já existe saída final, não refazer
            out_mp4 = self.out_wm_dir / mp4.name
            if out_mp4.exists():
                meta.update(
                    {
                        "status": "watermarked",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "wm_path": str(out_mp4),
                    }
                )
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                # remove o original da fila
                try:
                    mp4.unlink()
                except FileNotFoundError:
                    pass
                return

            # 1) watermark canto inferior direito
            tmp_out = self.out_wm_dir / f"{mp4.stem}.wm_tmp.mp4"
            add_image_watermark(
                input_path=str(mp4),
                watermark_path=str(self.watermark_path),
                output_path=str(tmp_out),
                margin=self.wm_margin,
                opacity=self.wm_opacity,
                rel_width=self.wm_rel_width,
                codec="libx264",
                crf=20,
                preset="medium",
            )
            tmp_out.replace(out_mp4)  # atomic move

            # 3) atualiza sidecar
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

        # 3.1) registra intenção de upload no backend (POST /api/videos/metadados)
        api_client = GravaNoisAPIClient()

        file_size_mb = upload_target.stat().st_size / (1024 * 1024)
        logger.info(f"Tamanho do arquivo: {file_size_mb:.2f} MB")

        if api_client.is_configured():
            try:  # Tenta fazer o registro com o servidor
                size_upload = upload_target.stat().st_size
                sha256_upload = _sha256_file(upload_target)
                meta_up = ffprobe_metadata(upload_target)

                payload = {
                    "venue_id": api_client.venue_id,
                    "duration_sec": float(meta_up.get("duration_sec") or 0.0),
                    "captured_at": meta.get("created_at"),
                    "meta": meta_up,
                    "sha256": sha256_upload,
                }

                logger.info("Enviando registro de metadados ao backend...")
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
                # status opcional: manter "watermarked" e anotar registro remoto
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                logger.info(f"Registro remoto OK: clip_id={resp.get('clip_id')}")

                # 3.2) upload para a URL assinada, se fornecida
                upload_url = (resp or {}).get("upload_url")
                if upload_url:
                    logger.info("Iniciando upload para URL assinada (Supabase)")
                    t0 = time.time()
                    try:
                        status_code, reason, resp_headers = api_client.upload_file_to_signed_url(
                            upload_url,
                            upload_target,
                            content_type="video/mp4",
                            extra_headers=None,
                            timeout=180.0,
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
                            }
                        )
                        meta_path.write_text(
                            json.dumps(meta, ensure_ascii=False, indent=2)
                        )
                        logger.info(
                            f"Upload finalizado: HTTP {status_code} {reason} em {dt_ms} ms"
                        )

                        # 3.3) Finaliza upload no backend (validação de integridade)
                        if 200 <= status_code < 300:
                            clip_id = (resp or {}).get("clip_id")
                            if clip_id:
                                try:
                                    logger.info(
                                        f"Notificando backend sobre upload concluído (clip_id={clip_id})"
                                    )
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
                else:
                    logger.warning("Nenhuma upload_url na resposta; pulando upload")
            except Exception as e:
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
        else:
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

        if uploaded_ok and finalized_ok:
            # remove artefatos da fila após confirmação completa
            try:
                mp4.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                meta_path.unlink(missing_ok=True)
            except Exception:
                pass
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


def is_within_business_hours() -> bool:
    default_tz = "America/Sao_Paulo"
    default_start = "07:00"
    default_end = "23:30"

    tz_name = os.getenv("GN_TIME_ZONE", default_tz)
    start_str = os.getenv("GN_START_TIME", default_start)
    end_str = os.getenv("GN_END_TIME", default_end)

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.warning(
            f"GN_TIME_ZONE inválido ({tz_name!r}); usando padrão {default_tz!r}"
        )
        tz = ZoneInfo(default_tz)

    def _parse_hhmm(raw: str, fallback: str, env_name: str):
        try:
            return datetime.strptime(raw, "%H:%M").time()
        except Exception:
            logger.warning(
                f"{env_name} inválido ({raw!r}); usando padrão {fallback!r}"
            )
            return datetime.strptime(fallback, "%H:%M").time()

    start_t = _parse_hhmm(start_str, default_start, "GN_START_TIME")
    end_t = _parse_hhmm(end_str, default_end, "GN_END_TIME")
    now_t = datetime.now(tz).time().replace(second=0, microsecond=0)

    if start_t <= end_t:
        return start_t <= now_t <= end_t
    return now_t >= start_t or now_t <= end_t


def main() -> int:
    base = Path(__file__).resolve().parent

    # Modo leve (pula watermark/thumbnail) por env GN_LIGHT_MODE=1/true/yes
    def _env_bool(name: str, default: bool = False) -> bool:
        v = os.getenv(name)
        if v is None:
            return default
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}

    light_mode = _env_bool("GN_LIGHT_MODE", False)

    # Permite configurar seg_time via env GN_SEG_TIME
    def _env_int(name: str, default: int) -> int:
        v = os.getenv(name)
        if v is None:
            return default
        try:
            return max(1, int(float(v)))
        except Exception:
            return default

    seg_time_env = _env_int("GN_SEG_TIME", 1)
    logger.info(f"Segmento de {seg_time_env}s, modo leve: {light_mode}")

    worker_max_attempts = _env_int("GN_MAX_ATTEMPTS", 3)

    rtsp_mode = bool((os.getenv("GN_RTSP_URL") or "").strip())
    if rtsp_mode:
        pre_seg_cfg = _env_int("GN_RTSP_PRE_SEGMENTS", 6)
        post_seg_cfg = _env_int("GN_RTSP_POST_SEGMENTS", 3)
        pre_sec_cfg = pre_seg_cfg * seg_time_env
        post_sec_cfg = post_seg_cfg * seg_time_env
    else:
        pre_seg_cfg = None
        post_seg_cfg = None
        pre_sec_cfg = 25
        post_sec_cfg = 10

    cfg = CaptureConfig(
        buffer_dir=Path(os.getenv("GN_BUFFER_DIR", "/dev/shm/grn_buffer")),
        clips_dir=base / "recorded_clips",
        queue_dir=base / "queue_raw",
        device="/dev/video0",
        seg_time=seg_time_env,
        pre_seconds=pre_sec_cfg,
        post_seconds=post_sec_cfg,
        scan_interval=1,
        max_buffer_seconds=40,
        failed_dir_highlight=base / "failed_clips",
        pre_segments=pre_seg_cfg,
        post_segments=post_seg_cfg,
    )

    # Limpa o buffer
    clear_buffer(cfg)

    # Verifica a existencia de todos os arquivos necessários
    cfg.ensure_dirs()

    # pastas do worker
    out_wm_dir = base / "highlights_wm"
    failed_dir_highlight = base / "failed_clips"
    if not light_mode:
        out_wm_dir.mkdir(parents=True, exist_ok=True)
    failed_dir_highlight.mkdir(parents=True, exist_ok=True)

    watermark_path = base / "files" / "replay_grava_nois.png"

    proc = start_ffmpeg(cfg)
    segbuf = SegmentBuffer(cfg)
    segbuf.start()

    # inicia worker
    worker = ProcessingWorker(
        queue_dir=cfg.queue_dir,
        out_wm_dir=out_wm_dir,
        failed_dir_highlight=failed_dir_highlight,
        watermark_path=watermark_path,
        scan_interval=1,
        max_attempts=worker_max_attempts,
        wm_margin=24,
        wm_opacity=0.6,
        wm_rel_width=0.11, # largura da marca d'água relativa ao vídeo. Ex: 0.11 = 11%
        light_mode=light_mode,
    )
    worker.start()

    # --- Disparo por ENTER ou GPIO (Raspberry Pi) ---
    trigger_q: queue.Queue[str] = queue.Queue()
    stop_evt = threading.Event()

    # Cooldown de botão GPIO: ignora novos disparos por 120s após um válido
    gpio_cooldown_sec = float(os.getenv("GN_GPIO_COOLDOWN_SEC", "120"))
    last_gpio_ok_ts = 0.0

    def _stdin_listener():
        # Bloqueia em input(); cada ENTER gera um trigger.
        try:
            while not stop_evt.is_set():
                try:
                    input()
                except EOFError:
                    # Sem stdin disponível; encerra listener.
                    break
                except KeyboardInterrupt:
                    # Propaga interrupção para o laço principal via stop_evt.
                    stop_evt.set()
                    break
                trigger_q.put("enter")
        except Exception as e:
            # Loga e encerra o listener sem derrubar o serviço.
            logger.exception(f"Erro no listener de stdin: {e}")

    stdin_t = threading.Thread(target=_stdin_listener, daemon=True)
    stdin_t.start()

    # abilita se GN_GPIO_PIN ou GPIO_PIN estiver definido.
    gpio_pin_env = os.getenv("GN_GPIO_PIN") or os.getenv("GPIO_PIN")
    pi = None
    cb = None
    if gpio_pin_env is not None:
        try:
            gpio_pin = int(gpio_pin_env)
        except ValueError:
            logger.error(f"Pino GPIO inválido em GN_GPIO_PIN/GPIO_PIN: {gpio_pin_env!r}")
            gpio_pin = None

        if gpio_pin is not None:
            try:
                import pigpio, time, subprocess

                debounce_ms = float(os.getenv("GN_GPIO_DEBOUNCE_MS", "300"))

                def _connect_pi():
                    p = pigpio.pi()
                    if not p.connected:
                        try:
                            # Tenta iniciar o daemon sem sudo, então reconecta
                            subprocess.Popen(
                                ["pigpiod"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            time.sleep(0.2)
                            p = pigpio.pi()
                        except Exception:
                            pass
                    return p

                pi = _connect_pi()
                if not pi or not pi.connected:
                    logger.error(
                        "pigpiod não está acessível. Rode 'pigpiod' e tente novamente"
                    )
                else:
                    # Configura pino como entrada com pull-up; botão ao GND.
                    pi.set_mode(gpio_pin, pigpio.INPUT)
                    pi.set_pull_up_down(gpio_pin, pigpio.PUD_UP)

                    last_ts = 0.0

                    def on_edge(gpio, level, tick):
                        nonlocal last_ts
                        # Considera borda de descida (pressionado)
                        if level == 0:
                            now = time.time()
                            if (now - last_ts) * 1000.0 < debounce_ms:
                                return
                            last_ts = now
                            trigger_q.put("gpio")

                    # Use FALLING_EDGE para já filtrar nível
                    cb = pi.callback(gpio_pin, pigpio.FALLING_EDGE, on_edge)
                    logger.info(
                        f"pigpio habilitado no pino BCM {gpio_pin} (debounce {int(debounce_ms)}ms)"
                    )
            except ImportError:
                logger.warning("pigpio não encontrado; seguindo apenas com ENTER")
            except Exception as e:
                logger.error(f"Falha ao configurar GPIO (pigpio): {e}")

    if cfg.pre_segments is not None and cfg.post_segments is not None:
        capture_desc = f"{cfg.pre_segments} seg + {cfg.post_segments} seg"
    else:
        capture_desc = f"{cfg.pre_seconds}s + {cfg.post_seconds}s"

    prompt = (
        f"Gravando… pressione ENTER"
        + (f" ou botão GPIO (BCM {gpio_pin_env})" if gpio_pin_env else "")
        + f" para capturar {capture_desc} (Ctrl+C sai)"
    )
    logger.info(prompt)

    try:
        while not stop_evt.is_set():  # Verifica se o evento foi acionado (Botao)
            try:
                trig = trigger_q.get(timeout=0.3)  # Procura triggers
            except queue.Empty:
                continue

            # Aplica cooldown apenas para o botão GPIO
            if trig == "gpio":
                now = time.time()
                elapsed = now - last_gpio_ok_ts
                if elapsed < gpio_cooldown_sec:
                    restante = int(gpio_cooldown_sec - elapsed)
                    logger.info(f"GPIO ignorado: cooldown ativo ({restante}s restantes)")
                    continue
                last_gpio_ok_ts = now

            if not is_within_business_hours():
                logger.warning("Fora do horário de funcionamento")
                continue

            out = build_highlight(
                cfg, segbuf
            )  # Constroi o clipe a partir dos seguimentos
            
            if out:
                try:
                    enqueue_clip(cfg, out)
                except Exception as e:
                    logger.error(f"Falha ao enfileirar {out.name}: {e}")
                    pend = failed_dir_highlight / "enqueue_failed"
                    pend.mkdir(parents=True, exist_ok=True)
                    try:
                        # move o arquivo gerado para falha
                        # (
                        #     (pend / out.name)
                        #     if not out.exists()
                        #     else out.replace(pend / out.name)
                        # )
                        shutil.move(str(out), str(pend / out.name))
                    except Exception:
                        pass
                    # sidecar mínimo com erro
                    meta = {
                        "type": "highlight_raw",
                        "status": "enqueue_failed",
                        "file_name": out.name,
                        "error": str(e),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    (pend / f"{out.stem}.json").write_text(
                        json.dumps(meta, ensure_ascii=False, indent=2)
                    )

    except KeyboardInterrupt:
        logger.info("Encerrando...")
    finally:
        # Sinaliza para todos os loops/threads que devem encerrar.
        stop_evt.set()
        try:
            if cb is not None:
                # Cancela o callback do GPIO (para de receber eventos do botão).
                cb.cancel()
        except Exception:
            # Ignora falhas durante o desligamento.
            pass
        try:
            if pi is not None:
                # Fecha a conexão com o daemon pigpio (não mata o pigpiod).
                pi.stop()
        except Exception:
            # Ignora falhas durante o desligamento.
            pass
        # Para a thread do SegmentBuffer e espera até 2s para concluir.
        segbuf.stop(join_timeout=2)
        try:
            # Solicita término do processo ffmpeg (libera o dispositivo de vídeo).
            proc.terminate()
        except Exception:
            # Ignora falhas durante o desligamento.
            pass
        try:
            worker.stop()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
