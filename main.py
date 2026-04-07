from __future__ import annotations

import sys
import shutil
from pathlib import Path
import os
import json
import time
import uuid
import select
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import queue
from dataclasses import dataclass, field

from dotenv import load_dotenv

from src.config.config_loader import get_effective_config
from src.config.settings import CaptureConfig, load_capture_configs, load_mqtt_config
from src.services.mqtt.command_dispatcher import CommandDispatcher
from src.services.mqtt.device_config_service import DeviceConfigService
from src.services.mqtt.device_presence_service import (
    DevicePresenceService,
    build_runtime_snapshot,
)
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger
from src.utils.logger import logger
from src.utils.pico import get_pico_serial_port, resolve_trigger_source
from src.utils.time_utils import is_within_business_hours
from src.video.buffer import SegmentBuffer, clear_buffer
from src.video.capture import start_ffmpeg
from src.video.processor import build_highlight, enqueue_clip
from src.workers.processing_worker import ProcessingWorker

load_dotenv()


@dataclass
class CameraRuntime:
    cfg: CaptureConfig
    proc: object
    segbuf: SegmentBuffer
    capture_lock: threading.Lock = field(default_factory=threading.Lock)
    _cooldown_until: float = field(default=0.0)


def _trigger_fan_out(
    runtimes: list[CameraRuntime],
    failed_dir_highlight: Path,
    executor: ThreadPoolExecutor,
    trigger_id: str,
) -> None:
    """Dispatch trigger concurrently to all active cameras."""

    def _process_one(rt: CameraRuntime) -> None:
        cfg = rt.cfg
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
    _trigger_fan_out([rt], failed_dir_highlight, executor, trigger_id)


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


def main() -> int:
    base = Path(__file__).resolve().parent

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

    runtimes: list[CameraRuntime] = []
    for cfg in camera_cfgs:
        clear_buffer(cfg)
        cfg.ensure_dirs()
        proc = start_ffmpeg(cfg)
        segbuf = SegmentBuffer(cfg)
        segbuf.start()
        runtimes.append(CameraRuntime(cfg=cfg, proc=proc, segbuf=segbuf))

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
    primary_runtime = runtimes[0]
    primary_cfg = primary_runtime.cfg

    # inicia 1 worker por câmera (cada um varre apenas sua queue_dir isolada)
    workers: list[ProcessingWorker] = []
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
            wm_rel_width=wm_rel_width,  # largura da marca d'água relativa ao vídeo. Ex: 0.11 = 11%
            light_mode=light_mode,
        )
        worker.start()
        workers.append(worker)
        logger.info(f"Worker iniciado para {cfg.camera_id}: fila={cfg.queue_dir}")

    mqtt_config = load_mqtt_config()
    mqtt_client = MQTTClient(mqtt_config)
    mqtt_presence: DevicePresenceService | None = None
    mqtt_dispatcher: CommandDispatcher | None = None
    mqtt_config_service: DeviceConfigService | None = None

    # --- Disparo por ENTER/GPIO/Pico ---
    trigger_q: queue.Queue[str] = queue.Queue()
    stop_evt = threading.Event()
    trigger_source = resolve_trigger_source(logger=logger)
    logger.info(f"Fonte de trigger físico selecionada: {trigger_source}")

    # Cooldown de botão físico (GPIO/Pico): por câmera via CameraRuntime._cooldown_until
    gpio_cooldown_sec = op_cfg.triggers.gpio.cooldown_seconds

    # Câmeras que participam do fan-out global (sem token Pico dedicado).
    # Se todas tiverem token dedicado, o fan-out global ainda dispara todas (fallback de debug).
    _fanout_runtimes = _get_fanout_targets(runtimes)

    pico_trigger_token = op_cfg.triggers.pico.global_token or "BTN_REPLAY"
    pico_serial_port: str | None = None
    gpio_enabled = False
    pico_enabled = False

    # Mapa de roteamento: token Pico dedicado → handler de câmera específica.
    # Câmeras sem pico_trigger_token participam apenas do fan-out global.
    token_map: dict[str, Callable[[], None]] = {}
    for _rt in runtimes:
        _token = _rt.cfg.pico_trigger_token
        if _token:
            def _make_handler(rt: CameraRuntime) -> Callable[[], None]:
                def _handler() -> None:
                    tid = uuid.uuid4().hex[:8]
                    logger.info(
                        f"[Pico] Token '{rt.cfg.pico_trigger_token}' → câmera '{rt.cfg.camera_id}' (dedicado)"
                    )
                    _trigger_single_camera(
                        rt, failed_dir_highlight, trigger_executor, tid, gpio_cooldown_sec,
                        skip_cooldown=dev_mode
                    )
                return _handler
            token_map[_token.strip().upper()] = _make_handler(_rt)

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
                import pigpio, subprocess

                debounce_ms = op_cfg.triggers.gpio.debounce_ms

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

            def _pico_serial_listener() -> None:
                try:
                    fd = os.open(
                        pico_serial_port,
                        os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY,
                    )
                except OSError as e:
                    logger.error(
                        f"Falha ao abrir porta serial do Pico ({pico_serial_port}): {e}"
                    )
                    return

                buffer = b""
                with os.fdopen(fd, "rb", buffering=0) as serial_stream:
                    logger.info(
                        f"Listener Pico serial ativo em {pico_serial_port} (token={pico_trigger_token!r})"
                    )
                    while not stop_evt.is_set():
                        try:
                            ready, _, _ = select.select([serial_stream], [], [], 0.3)
                        except Exception as e:
                            logger.error(f"Erro no select() da serial Pico: {e}")
                            return
                        if not ready:
                            continue

                        try:
                            chunk = serial_stream.read(256)
                        except BlockingIOError:
                            continue
                        except OSError as e:
                            logger.error(
                                f"Erro lendo serial Pico ({pico_serial_port}): {e}"
                            )
                            return

                        if not chunk:
                            continue
                        buffer += chunk

                        while b"\n" in buffer:
                            raw_line, buffer = buffer.split(b"\n", 1)
                            line = raw_line.decode("utf-8", errors="ignore").strip()
                            if not line:
                                continue
                            line_upper = line.upper()
                            if line_upper in token_map:
                                # Token dedicado a uma câmera: roteia diretamente.
                                token_map[line_upper]()
                            elif _serial_line_is_trigger(line, pico_trigger_token):
                                # Token global: enfileira fan-out.
                                logger.info(
                                    f"[Pico] Token '{line_upper}' → fan-out global"
                                )
                                trigger_q.put("pico")
                            else:
                                logger.warning(
                                    f"[Pico] Token desconhecido: {line_upper!r}"
                                )

            threading.Thread(target=_pico_serial_listener, daemon=True).start()
            pico_enabled = True
        else:
            logger.warning("Trigger Pico selecionado, mas nenhuma porta serial foi detectada")

    device_id = (
        (
            os.getenv("DEVICE_ID")
            or os.getenv("GN_DEVICE_ID")
            or os.getenv("GN_MQTT_CLIENT_ID")
            or ""
        ).strip()
    )
    client_id = (os.getenv("GN_CLIENT_ID") or os.getenv("CLIENT_ID") or "").strip()
    venue_id = (os.getenv("GN_VENUE_ID") or os.getenv("VENUE_ID") or "").strip()

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
            )
            snapshot["health"]["gpio_enabled"] = gpio_enabled
            snapshot["health"]["pico_enabled"] = pico_enabled
            return snapshot

        try:
            mqtt_presence = DevicePresenceService(
                mqtt_client,
                mqtt_config,
                device_id=device_id,
                client_id=client_id,
                venue_id=venue_id,
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
                device_secret=(
                    os.getenv("DEVICE_SECRET") or os.getenv("GN_DEVICE_SECRET") or ""
                ),
                agent_version=mqtt_config.agent_version,
            )
        except ValueError as exc:
            mqtt_presence = None
            mqtt_dispatcher = None
            mqtt_config_service = None
            mqtt_logger.warning(
                "MQTT habilitado, mas DEVICE_ID/GN_DEVICE_ID é inválido para tópico (%s); presença será ignorada",
                exc,
            )
        else:
            if mqtt_presence.start():
                mqtt_dispatcher.start()
                mqtt_config_service.start()
            elif mqtt_config.enabled:
                mqtt_logger.warning(
                    "Serviço MQTT não iniciou; captura e worker seguirão operando normalmente"
                )

    if primary_cfg.pre_segments is not None and primary_cfg.post_segments is not None:
        capture_desc = f"{primary_cfg.pre_segments} seg + {primary_cfg.post_segments} seg"
    else:
        capture_desc = f"{primary_cfg.pre_seconds}s + {primary_cfg.post_seconds}s"

    trigger_hints: list[str] = []
    if gpio_enabled and gpio_pin_env:
        trigger_hints.append(f"botão GPIO (BCM {gpio_pin_env})")
    if pico_enabled and pico_serial_port:
        trigger_hints.append(f"Pico serial ({pico_serial_port})")

    prompt = (
        f"Gravando… pressione ENTER"
        + (f" ou {' ou '.join(trigger_hints)}" if trigger_hints else "")
        + f" para capturar {capture_desc} (Ctrl+C sai)"
    )
    logger.info(prompt)

    try:
        while not stop_evt.is_set():  # Verifica se o evento foi acionado (Botao)
            try:
                trig = trigger_q.get(timeout=0.3)  # Procura triggers
            except queue.Empty:
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
                continue

            trigger_id = uuid.uuid4().hex[:8]
            _trigger_fan_out(fanout_targets, failed_dir_highlight, trigger_executor, trigger_id)

    except KeyboardInterrupt:
        logger.info("Encerrando...")
    finally:
        # Sinaliza para todos os loops/threads que devem encerrar.
        stop_evt.set()
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
                mqtt_presence.stop()
            except Exception:
                pass
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
        for runtime in runtimes:
            runtime.segbuf.stop(join_timeout=2)
        for runtime in runtimes:
            try:
                # Solicita término do processo ffmpeg (libera o dispositivo de vídeo).
                runtime.proc.terminate()
            except Exception:
                # Ignora falhas durante o desligamento.
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
