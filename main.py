from __future__ import annotations

import sys
import shutil
from pathlib import Path
import os
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import queue
from dataclasses import dataclass, field

from dotenv import load_dotenv

from src.config.settings import CaptureConfig, load_capture_configs
from src.utils.logger import logger
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

    max_workers = _env_int("GN_TRIGGER_MAX_WORKERS", len(runtimes))
    trigger_executor = ThreadPoolExecutor(max_workers=max(1, max_workers))

    # pastas do worker
    out_wm_dir = base / "highlights_wm"
    failed_dir_highlight = base / "failed_clips"
    if not light_mode:
        out_wm_dir.mkdir(parents=True, exist_ok=True)
    failed_dir_highlight.mkdir(parents=True, exist_ok=True)

    watermark_path = base / "files" / "replay_grava_nois.png"
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
            scan_interval=1,
            max_attempts=worker_max_attempts,
            wm_margin=24,
            wm_opacity=0.6,
            wm_rel_width=0.11,  # largura da marca d'água relativa ao vídeo. Ex: 0.11 = 11%
            light_mode=light_mode,
        )
        worker.start()
        workers.append(worker)
        logger.info(f"Worker iniciado para {cfg.camera_id}: fila={cfg.queue_dir}")

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
            logger.error(
                f"Pino GPIO inválido em GN_GPIO_PIN/GPIO_PIN: {gpio_pin_env!r}"
            )
            gpio_pin = None

        if gpio_pin is not None:
            try:
                import pigpio, subprocess

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

    if primary_cfg.pre_segments is not None and primary_cfg.post_segments is not None:
        capture_desc = f"{primary_cfg.pre_segments} seg + {primary_cfg.post_segments} seg"
    else:
        capture_desc = f"{primary_cfg.pre_seconds}s + {primary_cfg.post_seconds}s"

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
            if trig in ("gpio", "enter"):
                now = time.time()
                elapsed = now - last_gpio_ok_ts
                if elapsed < gpio_cooldown_sec:
                    restante = int(gpio_cooldown_sec - elapsed)
                    logger.info(
                        f"GPIO ignorado: cooldown ativo ({restante}s restantes)"
                    )
                    continue
                last_gpio_ok_ts = now

            if not is_within_business_hours():
                logger.warning("Fora do horário de funcionamento")
                continue

            trigger_id = uuid.uuid4().hex[:8]
            _trigger_fan_out(runtimes, failed_dir_highlight, trigger_executor, trigger_id)

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
