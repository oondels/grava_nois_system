from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from src.config.config_loader import get_effective_config
from src.config.settings import CaptureConfig, load_capture_configs, load_mqtt_config
from src.services.docker_action_request import DockerActionRequestService
from src.services.mqtt.capture_event_service import CaptureEventService
from src.services.mqtt.command_dispatcher import CommandDispatcher
from src.services.mqtt.device_config_service import (
    DeviceConfigService,
    apply_pending_config_on_startup,
)
from src.services.mqtt.device_diagnostic_service import DeviceDiagnosticEventService
from src.services.mqtt.device_env_service import DeviceEnvService
from src.services.mqtt.device_presence_service import (
    DevicePresenceService,
    build_runtime_snapshot,
)
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger
from src.services.pico_operations import (
    ConfirmedActionArm,
    MaintenanceMode,
    PicoOperationalMonitor,
)
from src.services.pico_serial_controller import (
    PICO_ACK_STARTED,
    PICO_STARTED_COMMAND,
    PicoSerialController,
    PicoStartedHandshake,
)
from src.services.rental_offline_service import RentalOfflineService
from src.services.pico_serial_controller import (
    send_pico_command as _send_pico_command_impl,
)
from src.utils.logger import logger
from src.utils.pico import get_pico_serial_port, resolve_trigger_source
from src.utils.time_utils import is_within_business_hours
from src.video.buffer import SegmentBuffer, clear_buffer
from src.video.capture import start_ffmpeg
from src.video.processor import build_highlight, enqueue_clip
from src.workers.processing_worker import ProcessingWorker

__all__ = (
    "PICO_ACK_STARTED",
    "PICO_STARTED_COMMAND",
    "PicoStartedHandshake",
)

load_dotenv()

CAMERA_STALE_AFTER_SEC = 10.0
CAMERA_STALE_RESTART_AFTER_SEC = 30.0
CAMERA_STALE_RESTART_CYCLES = 3
CAMERA_STALE_RESTART_STATUSES = {"STALE", "MISSING", "UNKNOWN"}
CAMERA_SUPERVISOR_INTERVAL_SEC = 5.0


def _send_pico_command(
    fd: int,
    command: str,
    _logger: object | None = None,
    write_timeout_sec: float = 0.2,
) -> bool:
    return _send_pico_command_impl(
        fd,
        command,
        _logger or logger,
        write_timeout_sec=write_timeout_sec,
    )


@dataclass
class CameraRuntime:
    cfg: CaptureConfig
    proc: subprocess.Popen | None = None
    segbuf: SegmentBuffer | None = None
    capture_lock: threading.Lock = field(default_factory=threading.Lock)
    _cooldown_until: float = field(default=0.0)
    camera_status: str = "STARTING"
    last_error: str = ""
    last_error_at: str = ""
    restart_attempts: int = 0


def _camera_readiness(rt: CameraRuntime) -> dict[str, object]:
    ffmpeg_alive = rt.proc is not None and rt.proc.poll() is None
    if rt.segbuf is None:
        buffer_status = "NO_BUFFER"
        diagnostics = None
    else:
        diagnostics = rt.segbuf.diagnostics(stale_after_sec=CAMERA_STALE_AFTER_SEC)
        buffer_status = diagnostics.buffer_status

    ready = ffmpeg_alive and rt.segbuf is not None and buffer_status == "FRESH"
    if ready:
        rt.camera_status = "OK"
        if rt.last_error.startswith("Buffer"):
            rt.last_error = ""
            rt.last_error_at = ""
    else:
        rt.camera_status = "UNAVAILABLE"
        if not ffmpeg_alive:
            reason = "FFmpeg indisponível"
        elif rt.segbuf is None:
            reason = "SegmentBuffer indisponível"
        elif buffer_status == "STALE":
            reason = "Buffer sem segmentos novos"
        elif buffer_status == "EMPTY":
            reason = "Buffer sem segmentos"
        else:
            reason = f"Buffer status={buffer_status}"
        if rt.last_error != reason:
            rt.last_error = reason
            rt.last_error_at = datetime.now(timezone.utc).isoformat()

    return {
        "ready": ready,
        "ffmpeg_alive": ffmpeg_alive,
        "buffer_status": buffer_status,
        "segment_age_sec": diagnostics.segment_age_sec if diagnostics else None,
        "last_segment_at": diagnostics.last_segment_at if diagnostics else None,
        "buffer_segment_count": diagnostics.segment_count if diagnostics else 0,
        "reason": rt.last_error,
    }


def _trigger_fan_out(
    runtimes: list[CameraRuntime],
    failed_dir_highlight: Path,
    executor: ThreadPoolExecutor,
    trigger_id: str,
    *,
    trigger_source: str = "unknown",
    capture_event_service: CaptureEventService | None = None,
) -> None:
    """Dispatch trigger concurrently to all active cameras."""

    def _process_one(rt: CameraRuntime) -> None:
        cfg = rt.cfg
        readiness = _camera_readiness(rt)
        if not readiness["ready"]:
            reason = str(readiness["reason"] or "camera_not_ready")
            logger.warning(
                f"[{cfg.camera_id}][{trigger_id}] câmera indisponível ({reason}) – skipping"
            )
            if capture_event_service is not None:
                capture_event_service.publish_trigger_rejected(
                    camera_id=cfg.camera_id,
                    trigger_id=trigger_id,
                    trigger_source=trigger_source,
                    reason=reason,
                    camera_status=rt.camera_status,
                    ffmpeg_alive=bool(readiness["ffmpeg_alive"]),
                    buffer_status=str(readiness["buffer_status"]),
                    segment_age_sec=readiness["segment_age_sec"],  # type: ignore[arg-type]
                    last_segment_at=readiness["last_segment_at"],  # type: ignore[arg-type]
                )
            return
        if not rt.capture_lock.acquire(blocking=False):
            logger.info(f"[{cfg.camera_id}][{trigger_id}] busy – skipping")
            return
        try:
            logger.info(f"[{cfg.camera_id}][{trigger_id}] building highlight")
            out = build_highlight(cfg, rt.segbuf)
            if out:
                try:
                    enqueue_clip(cfg, out)
                    logger.info(f"[{cfg.camera_id}][{trigger_id}] success: {out.name}")
                except Exception as e:
                    logger.error(f"[{cfg.camera_id}][{trigger_id}] enqueue failed: {e}")
                    pend = failed_dir_highlight / "enqueue_failed"
                    pend.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.move(str(out), str(pend / out.name))
                    except Exception:
                        pass
                    meta = {
                        "type": "highlight_raw",
                        "camera_id": cfg.camera_id,
                        "trigger_id": trigger_id,
                        "status": "enqueue_failed",
                        "file_name": out.name,
                        "error": str(e),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    (pend / f"{out.stem}.json").write_text(
                        json.dumps(meta, ensure_ascii=False, indent=2)
                    )
            else:
                logger.warning(f"[{cfg.camera_id}][{trigger_id}] no highlight built")
        except Exception as e:
            logger.error(f"[{cfg.camera_id}][{trigger_id}] error: {e}")
        finally:
            rt.capture_lock.release()

    futs = [executor.submit(_process_one, rt) for rt in runtimes]
    for fut in futs:
        try:
            fut.result()
        except Exception as e:
            logger.error(f"[{trigger_id}] unhandled error in fan-out: {e}")


def _trigger_single_camera(
    rt: CameraRuntime,
    failed_dir_highlight: Path,
    executor: ThreadPoolExecutor,
    trigger_id: str,
    cooldown_sec: float,
    skip_cooldown: bool = False,
    trigger_source: str = "unknown",
    capture_event_service: CaptureEventService | None = None,
) -> None:
    """Trigger a single camera respecting its per-camera cooldown (unless skip_cooldown=True)."""
    if not skip_cooldown:
        now = time.time()
        if now < rt._cooldown_until:
            remaining = int(rt._cooldown_until - now)
            logger.info(
                f"[{rt.cfg.camera_id}][{trigger_id}] cooldown ativo ({remaining}s restantes) – ignorado"
            )
            return
        rt._cooldown_until = now + cooldown_sec
    _trigger_fan_out(
        [rt],
        failed_dir_highlight,
        executor,
        trigger_id,
        trigger_source=trigger_source,
        capture_event_service=capture_event_service,
    )


def _get_fanout_targets(runtimes: list[CameraRuntime]) -> list[CameraRuntime]:
    """Returns cameras without a dedicated pico token for global fan-out.

    Falls back to all cameras if every camera has a dedicated token,
    so ENTER/GPIO always triggers at least something.
    """
    targets = [rt for rt in runtimes if rt.cfg.pico_trigger_token is None]
    return targets if targets else list(runtimes)


def _serial_line_is_trigger(line: str, token: str) -> bool:
    normalized_line = line.strip().upper()
    normalized_token = token.strip().upper()
    return bool(normalized_line) and normalized_line == normalized_token


def _terminate_ffmpeg_process(
    rt: CameraRuntime,
    *,
    reason: str,
    terminate_timeout: float = 5.0,
) -> None:
    proc = rt.proc
    if proc is None:
        return

    if proc.poll() is not None:
        rt.proc = None
        return

    logger.warning(f"[{rt.cfg.camera_id}] Encerrando FFmpeg para recuperar câmera: {reason}")
    try:
        proc.terminate()
        proc.wait(timeout=terminate_timeout)
    except subprocess.TimeoutExpired:
        logger.warning(
            f"[{rt.cfg.camera_id}] FFmpeg não encerrou em {terminate_timeout:.0f}s; forçando kill"
        )
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception as exc:
            logger.warning(f"[{rt.cfg.camera_id}] Falha ao finalizar FFmpeg com kill: {exc}")
    except Exception as exc:
        logger.warning(f"[{rt.cfg.camera_id}] Falha ao encerrar FFmpeg: {exc}")
    finally:
        rt.proc = None


def _camera_supervisor(
    rt: CameraRuntime,
    stop_evt: threading.Event,
    capture_event_service: CaptureEventService | None = None,
    max_backoff: float = 300.0,
    stale_restart_after_sec: float = CAMERA_STALE_RESTART_AFTER_SEC,
    stale_restart_cycles: int = CAMERA_STALE_RESTART_CYCLES,
    poll_interval: float = CAMERA_SUPERVISOR_INTERVAL_SEC,
) -> None:
    """Background thread that monitors and restarts FFmpeg for a camera."""
    retry_delay = 5.0
    next_start_delay = 0.0
    stale_since: float | None = None
    stale_cycles = 0
    while not stop_evt.is_set():
        needs_start = rt.proc is None or rt.proc.poll() is not None
        if not needs_start:
            readiness = _camera_readiness(rt)
            buffer_status = str(readiness.get("buffer_status") or "")
            if buffer_status in CAMERA_STALE_RESTART_STATUSES:
                now = time.monotonic()
                if stale_since is None:
                    stale_since = now
                    stale_cycles = 1
                    logger.warning(
                        f"[{rt.cfg.camera_id}] Buffer {buffer_status} detectado; "
                        "monitorando antes de reiniciar FFmpeg"
                    )
                else:
                    stale_cycles += 1

                stale_elapsed = now - stale_since
                if (
                    stale_elapsed >= stale_restart_after_sec
                    or stale_cycles >= stale_restart_cycles
                ):
                    reason = (
                        f"buffer {buffer_status} persistente por "
                        f"{stale_elapsed:.0f}s/{stale_cycles} ciclos"
                    )
                    _terminate_ffmpeg_process(rt, reason=reason)
                    needs_start = True
                    next_start_delay = 0.0
                    stale_since = None
                    stale_cycles = 0
                else:
                    retry_delay = 5.0
                    next_start_delay = 0.0
                    if stop_evt.wait(poll_interval):
                        break
                    continue
            else:
                stale_since = None
                stale_cycles = 0
                retry_delay = 5.0
                next_start_delay = 0.0
                if stop_evt.wait(poll_interval):
                    break
                continue

        if rt.camera_status == "OK":
            rt.camera_status = "ERROR"
            rt.last_error = "FFmpeg encerrou inesperadamente"
            rt.last_error_at = datetime.now(timezone.utc).isoformat()
            logger.warning(
                f"[{rt.cfg.camera_id}] FFmpeg morreu, tentando reiniciar em {retry_delay:.0f}s"
            )
            next_start_delay = retry_delay

        if stop_evt.wait(next_start_delay):
            break

        rt.restart_attempts += 1
        restart_reason = rt.last_error or "Iniciando FFmpeg"
        pre_restart_readiness = {
            "ffmpeg_alive": rt.proc is not None and rt.proc.poll() is None,
            "buffer_status": "NO_BUFFER",
            "segment_age_sec": None,
            "last_segment_at": None,
        }
        if rt.segbuf is not None:
            try:
                diagnostics = rt.segbuf.diagnostics(
                    stale_after_sec=CAMERA_STALE_AFTER_SEC
                )
                pre_restart_readiness["buffer_status"] = diagnostics.buffer_status
                pre_restart_readiness["segment_age_sec"] = diagnostics.segment_age_sec
                pre_restart_readiness["last_segment_at"] = diagnostics.last_segment_at
            except Exception:
                pre_restart_readiness["buffer_status"] = "UNKNOWN"
        rt.camera_status = "RECONNECTING"
        rt.last_error = restart_reason
        rt.last_error_at = datetime.now(timezone.utc).isoformat()
        logger.info(f"[{rt.cfg.camera_id}] Tentativa de start/restart #{rt.restart_attempts}")
        if capture_event_service is not None:
            capture_event_service.publish_camera_reconnecting(
                camera_id=rt.cfg.camera_id,
                reason=restart_reason,
                restart_attempts=rt.restart_attempts,
                ffmpeg_alive=bool(pre_restart_readiness["ffmpeg_alive"]),
                buffer_status=str(pre_restart_readiness["buffer_status"]),
                segment_age_sec=pre_restart_readiness["segment_age_sec"],  # type: ignore[arg-type]
                last_segment_at=pre_restart_readiness["last_segment_at"],  # type: ignore[arg-type]
            )

        try:
            if rt.segbuf is not None:
                try:
                    rt.segbuf.stop(join_timeout=2)
                except Exception:
                    pass

            clear_buffer(rt.cfg)
            proc = start_ffmpeg(rt.cfg)
            segbuf = SegmentBuffer(rt.cfg)
            segbuf.start()
            rt.proc = proc
            rt.segbuf = segbuf
            rt.camera_status = "OK"
            rt.last_error = ""
            retry_delay = 5.0
            next_start_delay = 0.0
            logger.info(f"[{rt.cfg.camera_id}] Câmera reiniciada com sucesso")
            if capture_event_service is not None:
                capture_event_service.publish_camera_reconnected(
                    camera_id=rt.cfg.camera_id,
                    reason="FFmpeg reiniciado com sucesso",
                    restart_attempts=rt.restart_attempts,
                )
        except Exception as e:
            rt.camera_status = "UNAVAILABLE"
            rt.last_error = str(e)
            rt.last_error_at = datetime.now(timezone.utc).isoformat()
            logger.error(f"[{rt.cfg.camera_id}] Falha no restart: {e}")
            if capture_event_service is not None:
                capture_event_service.publish_camera_restart_failed(
                    camera_id=rt.cfg.camera_id,
                    reason=str(e),
                    restart_attempts=rt.restart_attempts,
                    buffer_status=str(pre_restart_readiness["buffer_status"]),
                    segment_age_sec=pre_restart_readiness["segment_age_sec"],  # type: ignore[arg-type]
                    last_segment_at=pre_restart_readiness["last_segment_at"],  # type: ignore[arg-type]
                )
            next_start_delay = retry_delay
            retry_delay = min(retry_delay * 2, max_backoff)


def main() -> int:
    base = Path(__file__).resolve().parent

    startup_config_report = apply_pending_config_on_startup()

    # Carrega config operacional (config.json → env → defaults)
    op_cfg = get_effective_config()

    # DEV mode permanece em env (flag de desenvolvimento — não vai para config.json)
    dev_mode = os.getenv("DEV", "").strip().lower() in {"1", "true", "yes", "y", "on"}

    light_mode = op_cfg.processing.light_mode
    seg_time_env = op_cfg.capture.segment_seconds
    worker_max_attempts = op_cfg.processing.max_attempts

    mode_desc = f"modo leve: {light_mode}"
    if dev_mode:
        mode_desc += ", DEV=true (cooldown desativado)"
    logger.info(f"Segmento de {seg_time_env}s, {mode_desc}")

    camera_cfgs = load_capture_configs(base=base, seg_time=seg_time_env)
    logger.info(f"Câmeras ativas: {len(camera_cfgs)}")

    # --- Declarações antecipadas para snapshot MQTT ---
    runtimes: list[CameraRuntime] = []
    stop_evt = threading.Event()
    trigger_source = resolve_trigger_source(logger=logger)
    logger.info(f"Fonte de trigger físico selecionada: {trigger_source}")
    gpio_enabled = False
    pico_enabled = False

    # --- MQTT: inicia ANTES das câmeras para publicar status mesmo com falha de hardware ---
    device_id = (
        (
            os.getenv("DEVICE_ID")
            or os.getenv("GN_DEVICE_ID")
            or os.getenv("GN_MQTT_CLIENT_ID")
            or ""
        ).strip()
    )
    client_id = (os.getenv("GN_CLIENT_ID") or os.getenv("CLIENT_ID") or "").strip()
    device_mode = (os.getenv("GN_DEVICE_MODE") or "fixed").strip().lower()
    venue_id = (os.getenv("GN_VENUE_ID") or os.getenv("VENUE_ID") or "").strip() or None
    if device_mode not in {"fixed", "rental"}:
        raise RuntimeError("GN_DEVICE_MODE deve ser fixed ou rental")
    if device_mode == "fixed" and not venue_id:
        raise RuntimeError("GN_VENUE_ID é obrigatório para device fixed")
    if device_mode == "rental" and venue_id:
        raise RuntimeError("GN_VENUE_ID deve ficar vazio para device rental")
    if device_mode == "rental":
        client_id = None
    api_base = (os.getenv("GN_API_BASE") or os.getenv("API_BASE_URL") or "").strip()
    if api_base:
        if device_mode == "fixed" and not client_id:
            raise RuntimeError("GN_CLIENT_ID é obrigatório quando a API está habilitada")
        if not device_id:
            raise RuntimeError("DEVICE_ID é obrigatório quando a API está habilitada")
        if not (os.getenv("DEVICE_SECRET") or os.getenv("GN_DEVICE_SECRET") or "").strip():
            raise RuntimeError("DEVICE_SECRET é obrigatório quando a API está habilitada")

    mqtt_config = load_mqtt_config()
    mqtt_client = MQTTClient(mqtt_config)
    mqtt_presence: DevicePresenceService | None = None
    mqtt_dispatcher: CommandDispatcher | None = None
    mqtt_config_service: DeviceConfigService | None = None
    mqtt_env_service: DeviceEnvService | None = None
    capture_event_service: CaptureEventService | None = None
    diagnostic_event_service: DeviceDiagnosticEventService | None = None
    rental_offline_service: RentalOfflineService | None = None
    boot_id = str(uuid.uuid4())

    if mqtt_config.enabled and not device_id:
        mqtt_logger.warning(
            "MQTT habilitado, mas DEVICE_ID/GN_DEVICE_ID não foi configurado; presença será ignorada"
        )
    elif mqtt_config.enabled:
        def _runtime_snapshot_provider() -> dict[str, object]:
            snapshot = build_runtime_snapshot(
                runtimes=runtimes,
                light_mode=light_mode,
                dev_mode=dev_mode,
                trigger_source=trigger_source,
                camera_stale_after_sec=CAMERA_STALE_AFTER_SEC,
            )
            snapshot["health"]["gpio_enabled"] = gpio_enabled
            snapshot["health"]["pico_enabled"] = pico_enabled
            snapshot["runtime"]["boot_id"] = boot_id
            snapshot["runtime"]["mqtt_connected"] = mqtt_client.is_connected
            snapshot["runtime"]["mqtt_reconnect_count"] = mqtt_client.reconnect_count
            snapshot["runtime"]["last_mqtt_disconnect_at"] = mqtt_client.last_disconnect_at
            snapshot["runtime"]["last_mqtt_disconnect_reason"] = mqtt_client.last_disconnect_reason
            if capture_event_service is not None:
                capture_event_service.flush_outbox()
            return snapshot

        try:
            mqtt_presence = DevicePresenceService(
                mqtt_client,
                mqtt_config,
                device_id=device_id,
                client_id=client_id,
                venue_id=venue_id,
                boot_id=boot_id,
                runtime_snapshot_provider=_runtime_snapshot_provider,
            )
            mqtt_dispatcher = CommandDispatcher(
                mqtt_client,
                device_id=device_id,
                command_in_topic=mqtt_config.topic_for(device_id, "commands/in"),
                command_out_topic=mqtt_config.topic_for(device_id, "commands/out"),
            )
            mqtt_config_service = DeviceConfigService(
                mqtt_client,
                device_id=device_id,
                client_id=client_id,
                venue_id=venue_id,
                desired_topic=mqtt_config.topic_for(device_id, "config/desired"),
                reported_topic=mqtt_config.topic_for(device_id, "config/reported"),
                request_topic=mqtt_config.topic_for(device_id, "config/request"),
                state_topic=mqtt_config.topic_for(device_id, "config/state"),
                device_secret=(
                    os.getenv("DEVICE_SECRET") or os.getenv("GN_DEVICE_SECRET") or ""
                ),
                agent_version=mqtt_config.agent_version,
            )
            mqtt_env_service = DeviceEnvService(
                mqtt_client,
                device_id=device_id,
                client_id=client_id,
                venue_id=venue_id,
                request_topic=mqtt_config.topic_for(device_id, "env/request"),
                desired_topic=mqtt_config.topic_for(device_id, "env/desired"),
                reported_topic=mqtt_config.topic_for(device_id, "env/reported"),
                device_secret=(
                    os.getenv("DEVICE_SECRET") or os.getenv("GN_DEVICE_SECRET") or ""
                ),
                agent_version=mqtt_config.agent_version,
            )
            capture_event_service = CaptureEventService(
                mqtt_client,
                topic=mqtt_config.topic_for(device_id, "capture/events"),
                device_id=device_id,
                client_id=client_id,
                venue_id=venue_id,
                device_secret=(
                    os.getenv("DEVICE_SECRET") or os.getenv("GN_DEVICE_SECRET") or ""
                ),
                agent_version=mqtt_config.agent_version,
                outbox_dir=base / "runtime_config" / "capture_event_outbox",
            )
            diagnostic_event_service = DeviceDiagnosticEventService(
                mqtt_client,
                topic=mqtt_config.topic_for(device_id, "diagnostics/events"),
                device_id=device_id,
                client_id=client_id,
                venue_id=venue_id,
                device_secret=(
                    os.getenv("DEVICE_SECRET") or os.getenv("GN_DEVICE_SECRET") or ""
                ),
                boot_id=boot_id,
                agent_version=mqtt_config.agent_version,
            )
            if startup_config_report is not None:
                mqtt_config_service.queue_startup_report(startup_config_report)
        except ValueError as exc:
            mqtt_presence = None
            mqtt_dispatcher = None
            mqtt_config_service = None
            mqtt_env_service = None
            diagnostic_event_service = None
            mqtt_logger.warning(
                "MQTT habilitado, mas DEVICE_ID/GN_DEVICE_ID é inválido para tópico (%s); presença será ignorada",
                exc,
            )
        else:
            diagnostic_event_service.start()
            if mqtt_presence.start():
                mqtt_dispatcher.start()
                mqtt_config_service.start()
                mqtt_env_service.start()
            elif mqtt_config.enabled:
                mqtt_logger.warning(
                    "Serviço MQTT não iniciou; captura e worker seguirão operando normalmente"
                )

    # --- Estado de câmeras: cria runtimes sem bloquear o bootstrap em RTSP/FFmpeg ---
    for cfg in camera_cfgs:
        clear_buffer(cfg)
        cfg.ensure_dirs()
        runtimes.append(CameraRuntime(cfg=cfg))

    max_workers = op_cfg.triggers.max_workers if op_cfg.triggers.max_workers is not None else len(runtimes)
    trigger_executor = ThreadPoolExecutor(max_workers=max(1, max_workers))

    # pastas do worker
    out_wm_dir = base / "highlights_wm"
    failed_dir_highlight = base / "failed_clips"
    if not light_mode:
        out_wm_dir.mkdir(parents=True, exist_ok=True)
    failed_dir_highlight.mkdir(parents=True, exist_ok=True)

    default_wm_path = base / "files" / "replay_grava_nois.png"
    optimized_wm_path = base / "files" / "replay_grava_nois_wm.png"
    watermark_path = optimized_wm_path if optimized_wm_path.exists() else default_wm_path

    default_client_wm_path = base / "files" / "client_logo.png"
    optimized_client_wm_path = base / "files" / "client_logo_wm.png"
    client_watermark_path = (
        optimized_client_wm_path
        if optimized_client_wm_path.exists()
        else default_client_wm_path
    )
    wm_margin = op_cfg.processing.watermark.margin
    wm_opacity = op_cfg.processing.watermark.opacity
    wm_rel_width = op_cfg.processing.watermark.relative_width
    primary_cfg = camera_cfgs[0] if camera_cfgs else None

    # --- Disparo por ENTER/GPIO/Pico: inicia antes das câmeras para sinalizar runtime básico ---
    trigger_q: queue.Queue[str] = queue.Queue()

    # Cooldown de botão físico (GPIO/Pico): por câmera via CameraRuntime._cooldown_until
    gpio_cooldown_sec = op_cfg.triggers.gpio.cooldown_seconds

    # Câmeras que participam do fan-out global (sem token Pico dedicado).
    # Se todas tiverem token dedicado, o fan-out global ainda dispara todas (fallback de debug).
    _fanout_runtimes = _get_fanout_targets(runtimes)

    pico_trigger_token = op_cfg.triggers.pico.global_token or "BTN_REPLAY"
    docker_action_requests = DockerActionRequestService.from_env(logger=logger)
    pico_serial_port: str | None = None
    pico_controller: PicoSerialController | None = None
    pico_monitor: PicoOperationalMonitor | None = None
    maintenance_mode = MaintenanceMode(duration_sec=15 * 60)
    shutdown_arm = ConfirmedActionArm(window_sec=5.0)

    # Workers são iniciados antes dos triggers para qualquer clipe gerado já ter fila ativa.
    workers: list[ProcessingWorker] = []

    def _upload_rental_pending(rental_id: str) -> dict[str, int]:
        directory = Path(
            os.getenv("GN_RENTAL_CLIPS_DIR", str(base / "rental_clips_generated"))
        ) / rental_id
        processed = uploaded = failed = 0
        worker = workers[0] if workers else None
        if worker is None:
            return {"processed": 0, "uploaded": 0, "failed": 0}
        for video in sorted(directory.glob("*.mp4")) if directory.exists() else []:
            processed += 1
            if worker.process_manual_pending(video):
                uploaded += 1
            else:
                failed += 1
        return {"processed": processed, "uploaded": uploaded, "failed": failed}

    if device_mode == "rental":
        rental_offline_service = RentalOfflineService(
            mqtt_client=mqtt_client,
            device_id=device_id,
            device_secret=(
                os.getenv("DEVICE_SECRET") or os.getenv("GN_DEVICE_SECRET") or ""
            ),
            topic_for=lambda suffix: mqtt_config.topic_for(device_id, suffix),
            root_dir=Path(
                os.getenv("GN_RENTAL_CLIPS_DIR", str(base / "rental_clips_generated"))
            ),
            upload_callback=_upload_rental_pending,
            logger=logger,
            quarantine_ttl_hours=int(
                os.getenv("GN_RENTAL_QUARANTINE_TTL_HOURS", "48")
            ),
        )

    def _publish_worker_operational_event(reason: str) -> None:
        if pico_monitor is not None:
            pico_monitor.publish_feedback("TRIGGER", f"REJECTED:{reason}")

    for rt in runtimes:
        cfg = rt.cfg
        worker = ProcessingWorker(
            queue_dir=cfg.queue_dir,
            out_wm_dir=out_wm_dir,
            failed_dir_highlight=cfg.failed_dir_highlight,
            watermark_path=watermark_path,
            client_watermark_path=client_watermark_path,
            scan_interval=1,
            max_attempts=worker_max_attempts,
            wm_margin=wm_margin,
            wm_opacity=wm_opacity,
            wm_rel_width=wm_rel_width,
            light_mode=light_mode,
            retry_failed=device_mode != "rental",
            operational_event_callback=_publish_worker_operational_event,
            pending_destination=(
                rental_offline_service.destination_for
                if rental_offline_service is not None
                else None
            ),
        )
        worker.start()
        workers.append(worker)
        logger.info(f"Worker iniciado para {cfg.camera_id}: fila={cfg.queue_dir}")

    if rental_offline_service is not None:
        rental_offline_service.start()

    # Mapa de roteamento: token Pico dedicado → handler de câmera específica.
    # Câmeras sem pico_trigger_token participam apenas do fan-out global.
    token_map: dict[str, Callable[[], None]] = {}
    for _rt in runtimes:
        _token = _rt.cfg.pico_trigger_token
        if _token:
            def _make_handler(rt: CameraRuntime) -> Callable[[], None]:
                def _handler() -> None:
                    if maintenance_mode.active:
                        logger.warning(
                            "[Pico][%s] Trigger bloqueado: modo manutencao ativo",
                            rt.cfg.camera_id,
                        )
                        if pico_monitor is not None:
                            pico_monitor.publish_feedback(
                                "TRIGGER", "REJECTED:maintenance_mode"
                            )
                        return
                    if not is_within_business_hours():
                        logger.warning(
                            f"[Pico][{rt.cfg.camera_id}] Token dedicado ignorado: fora do horário de funcionamento"
                        )
                        if pico_monitor is not None:
                            pico_monitor.publish_feedback(
                                "TRIGGER", "REJECTED:outside_allowed_window"
                            )
                        return
                    tid = uuid.uuid4().hex[:8]
                    logger.info(
                        f"[Pico] Token '{rt.cfg.pico_trigger_token}' → câmera '{rt.cfg.camera_id}' (dedicado)"
                    )
                    _trigger_single_camera(
                        rt, failed_dir_highlight, trigger_executor, tid, gpio_cooldown_sec,
                        skip_cooldown=dev_mode,
                        trigger_source="pico",
                        capture_event_service=capture_event_service,
                    )
                return _handler
            token_map[_token.strip().upper()] = _make_handler(_rt)

    def _stdin_listener():
        try:
            while not stop_evt.is_set():
                try:
                    input()
                except EOFError:
                    break
                except KeyboardInterrupt:
                    stop_evt.set()
                    break
                trigger_q.put("enter")
        except Exception as e:
            logger.exception(f"Erro no listener de stdin: {e}")

    stdin_t = threading.Thread(target=_stdin_listener, daemon=True)
    stdin_t.start()

    # habilita GPIO se o modo selecionado permitir.
    # pin: lido do loader (config.json → GN_GPIO_PIN/GPIO_PIN via env fallback)
    _gpio_pin_from_cfg = op_cfg.triggers.gpio.pin
    gpio_pin_env = (
        str(_gpio_pin_from_cfg)
        if _gpio_pin_from_cfg is not None
        else (os.getenv("GN_GPIO_PIN") or os.getenv("GPIO_PIN"))
    )
    pi = None
    cb = None
    if trigger_source in {"gpio", "both"} and gpio_pin_env is not None:
        try:
            gpio_pin = int(gpio_pin_env)
        except ValueError:
            logger.error(
                f"Pino GPIO inválido (GN_GPIO_PIN/GPIO_PIN/triggers.gpio.pin): {gpio_pin_env!r}"
            )
            gpio_pin = None

        if gpio_pin is not None:
            try:
                import pigpio

                debounce_ms = op_cfg.triggers.gpio.debounce_ms

                def _connect_pi():
                    p = pigpio.pi()
                    if not p.connected:
                        try:
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
                    pi.set_mode(gpio_pin, pigpio.INPUT)
                    pi.set_pull_up_down(gpio_pin, pigpio.PUD_UP)

                    last_ts = 0.0

                    def on_edge(gpio, level, tick):
                        nonlocal last_ts
                        if level == 0:
                            now = time.time()
                            if (now - last_ts) * 1000.0 < debounce_ms:
                                return
                            last_ts = now
                            trigger_q.put("gpio")

                    cb = pi.callback(gpio_pin, pigpio.FALLING_EDGE, on_edge)
                    gpio_enabled = True
                    logger.info(
                        f"pigpio habilitado no pino BCM {gpio_pin} (debounce {int(debounce_ms)}ms)"
                    )
            except ImportError:
                logger.warning("pigpio não encontrado; seguindo apenas com ENTER")
            except Exception as e:
                logger.error(f"Falha ao configurar GPIO (pigpio): {e}")
    elif trigger_source in {"gpio", "both"}:
        logger.warning(
            "Trigger GPIO selecionado, mas GN_GPIO_PIN/GPIO_PIN não foi definido"
        )

    should_try_pico = trigger_source in {"pico", "both"}
    if trigger_source == "gpio" and not gpio_enabled:
        logger.warning(
            "Trigger em modo GPIO indisponível; tentando fallback para Pico serial"
        )
        should_try_pico = True

    if should_try_pico:
        pico_serial_port = get_pico_serial_port(logger=logger)
        if pico_serial_port:
            logger.info(f"Porta serial Pico selecionada: {pico_serial_port}")

            def _pico_snapshot_provider() -> dict[str, object]:
                return build_runtime_snapshot(
                    runtimes=runtimes,
                    light_mode=light_mode,
                    dev_mode=dev_mode,
                    trigger_source=trigger_source,
                    camera_stale_after_sec=CAMERA_STALE_AFTER_SEC,
                )

            def _handle_pico_token(line_upper: str) -> None:
                if line_upper in {"REQUEST_DIAGNOSTIC", "RUN_SELF_TEST"}:
                    if pico_monitor is not None:
                        threading.Thread(
                            target=pico_monitor.run_diagnostic,
                            daemon=True,
                            name="pico-diagnostic",
                        ).start()
                    return
                if line_upper == "TOGGLE_MAINTENANCE":
                    active = maintenance_mode.toggle()
                    logger.warning(
                        "[Pico] Modo manutencao %s",
                        "ativado por 15 minutos" if active else "desativado",
                    )
                    return
                if line_upper == "ARM_SHUTDOWN":
                    shutdown_arm.arm(docker_action_requests.shutdown_enabled)
                    if pico_monitor is not None:
                        result = (
                            "ARMED"
                            if docker_action_requests.shutdown_enabled
                            else "DENIED:shutdown_disabled"
                        )
                        pico_monitor.publish_feedback("ACTION", result)
                    return
                if line_upper == docker_action_requests.shutdown_token:
                    if not shutdown_arm.consume():
                        logger.warning(
                            "[Pico] Desligamento negado: confirmacao ausente ou expirada"
                        )
                        if pico_monitor is not None:
                            pico_monitor.publish_feedback(
                                "ACTION", "DENIED:shutdown_not_armed"
                            )
                        return
                action_tokens = {
                    docker_action_requests.pull_token,
                    docker_action_requests.restart_token,
                    docker_action_requests.shutdown_token,
                }
                if line_upper in action_tokens:
                    allowed = docker_action_requests.enabled and (
                        line_upper != docker_action_requests.shutdown_token
                        or docker_action_requests.shutdown_enabled
                    )
                    docker_action_requests.handle_token(line_upper)
                    if pico_monitor is not None:
                        pico_monitor.publish_feedback(
                            "ACTION",
                            "ACCEPTED" if allowed else "DENIED:action_disabled",
                        )
                    return
                if line_upper in token_map:
                    threading.Thread(
                        target=token_map[line_upper],
                        daemon=True,
                        name=f"pico-trigger-{line_upper.lower()}",
                    ).start()
                elif line_upper == "TRIGGER_GLOBAL" or _serial_line_is_trigger(
                    line_upper, pico_trigger_token
                ):
                    logger.info("[Pico] Token '%s' -> fan-out global", line_upper)
                    trigger_q.put("pico")
                else:
                    logger.warning("[Pico] Token desconhecido: %r", line_upper)

            pico_controller = PicoSerialController(
                port=pico_serial_port,
                on_token=_handle_pico_token,
                logger=logger,
            )
            pico_monitor = PicoOperationalMonitor(
                send=pico_controller.send,
                snapshot_provider=_pico_snapshot_provider,
                mqtt_enabled=mqtt_config.enabled,
                mqtt_connected=lambda: mqtt_client.is_connected,
                maintenance=maintenance_mode,
                serial_healthy=pico_controller.has_recent_pong,
                logger=logger,
            )
            pico_controller.start()
            pico_monitor.start()
            pico_enabled = True
        else:
            logger.warning("Trigger Pico selecionado, mas nenhuma porta serial foi detectada")

    # Supervisores fazem a primeira tentativa de câmera em background.
    # Assim MQTT e Pico/LED não dependem de câmera ligada ou RTSP acessível.
    supervisor_threads: list[threading.Thread] = []
    for rt in runtimes:
        t = threading.Thread(
            target=_camera_supervisor,
            args=(rt, stop_evt, capture_event_service),
            daemon=True,
            name=f"supervisor-{rt.cfg.camera_id}",
        )
        t.start()
        supervisor_threads.append(t)

    if primary_cfg is not None:
        if primary_cfg.pre_segments is not None and primary_cfg.post_segments is not None:
            capture_desc = f"{primary_cfg.pre_segments} seg + {primary_cfg.post_segments} seg"
        else:
            capture_desc = f"{primary_cfg.pre_seconds}s + {primary_cfg.post_seconds}s"
    else:
        capture_desc = "N/A"

    trigger_hints: list[str] = []
    if gpio_enabled and gpio_pin_env:
        trigger_hints.append(f"botão GPIO (BCM {gpio_pin_env})")
    if pico_enabled and pico_serial_port:
        trigger_hints.append(f"Pico serial ({pico_serial_port})")

    prompt = (
        "Gravando… pressione ENTER"
        + (f" ou {' ou '.join(trigger_hints)}" if trigger_hints else "")
        + f" para capturar {capture_desc} (Ctrl+C sai)"
    )
    logger.info(prompt)

    try:
        while not stop_evt.is_set():
            try:
                trig = trigger_q.get(timeout=0.3)
            except queue.Empty:
                continue

            if maintenance_mode.active:
                logger.warning("Trigger %s bloqueado: modo manutencao ativo", trig)
                if trig == "pico" and pico_monitor is not None:
                    pico_monitor.publish_feedback(
                        "TRIGGER", "REJECTED:maintenance_mode"
                    )
                continue

            # Cooldown por câmera para triggers físicos (gpio/pico global).
            # Em DEV mode, ignora cooldown para acelerar testes.
            if trig in ("gpio", "pico"):
                if dev_mode:
                    fanout_targets = _fanout_runtimes
                else:
                    now = time.time()
                    _ready: list[CameraRuntime] = []
                    for rt in _fanout_runtimes:
                        if now < rt._cooldown_until:
                            remaining = int(rt._cooldown_until - now)
                            logger.info(
                                f"Trigger físico ({trig}) ignorado para {rt.cfg.camera_id}: "
                                f"cooldown ativo ({remaining}s restantes)"
                            )
                        else:
                            rt._cooldown_until = now + gpio_cooldown_sec
                            _ready.append(rt)
                    if not _ready:
                        continue
                    fanout_targets = _ready
            else:
                fanout_targets = _fanout_runtimes

            if not is_within_business_hours():
                logger.warning("Fora do horário de funcionamento")
                if trig == "pico" and pico_monitor is not None:
                    pico_monitor.publish_feedback(
                        "TRIGGER", "REJECTED:outside_allowed_window"
                    )
                continue

            trigger_id = uuid.uuid4().hex[:8]
            _trigger_fan_out(
                fanout_targets,
                failed_dir_highlight,
                trigger_executor,
                trigger_id,
                trigger_source=trig,
                capture_event_service=capture_event_service,
            )
            if trig == "pico" and pico_monitor is not None:
                pico_monitor.publish_feedback("TRIGGER", "ACCEPTED")

    except KeyboardInterrupt:
        logger.info("Encerrando...")
    finally:
        stop_evt.set()
        if pico_monitor is not None:
            pico_monitor.stop()
        if pico_controller is not None:
            pico_controller.stop()
        if rental_offline_service is not None:
            rental_offline_service.stop()
        if mqtt_dispatcher is not None:
            try:
                mqtt_dispatcher.stop()
            except Exception:
                pass
        if mqtt_config_service is not None:
            try:
                mqtt_config_service.stop()
            except Exception:
                pass
        if mqtt_presence is not None:
            try:
                if diagnostic_event_service is not None:
                    diagnostic_event_service.publish_shutdown_clean()
                mqtt_presence.stop()
            except Exception:
                pass
        try:
            if cb is not None:
                cb.cancel()
        except Exception:
            pass
        try:
            if pi is not None:
                pi.stop()
        except Exception:
            pass
        for runtime in runtimes:
            if runtime.segbuf is not None:
                try:
                    runtime.segbuf.stop(join_timeout=2)
                except Exception:
                    pass
        for runtime in runtimes:
            if runtime.proc is not None:
                try:
                    runtime.proc.terminate()
                except Exception:
                    pass
        for worker in workers:
            try:
                worker.stop()
            except Exception:
                pass
        try:
            trigger_executor.shutdown(wait=False)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
