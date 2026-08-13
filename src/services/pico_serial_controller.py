from __future__ import annotations

import os
import queue
import select
import termios
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

PICO_STARTED_COMMAND = "GRN_STARTED"
PICO_ACK_STARTED = "ACK_GRN_STARTED"
PICO_STARTED_INITIAL_DELAY_SEC = 0.8
PICO_STARTED_RETRY_DELAYS_SEC = (0.25, 0.5, 1.0, 2.0, 5.0)
PICO_STARTED_WARNING_INTERVAL_SEC = 10.0


def configure_pico_serial(fd: int, logger: Any) -> None:
    """Configure USB CDC in non-canonical mode without resetting the Pico."""
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[3] = 0
        attrs[2] |= termios.CLOCAL | termios.CREAD
        if hasattr(termios, "HUPCL"):
            attrs[2] &= ~termios.HUPCL
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception as exc:
        logger.warning("[Pico] Nao foi possivel configurar modo serial: %s", exc)


def send_pico_command(
    fd: int,
    command: str,
    logger: Any,
    write_timeout_sec: float = 0.2,
) -> bool:
    payload = f"{command.strip()}\n".encode()
    offset = 0
    try:
        while offset < len(payload):
            _, writable, _ = select.select([], [fd], [], write_timeout_sec)
            if not writable:
                return False
            written = os.write(fd, payload[offset:])
            if written <= 0:
                return False
            offset += written
        return True
    except BlockingIOError:
        return False
    except OSError as exc:
        logger.error("[Pico] Falha ao enviar comando %s: %s", command, exc)
        return False


@dataclass
class PicoStartedHandshake:
    command: str = PICO_STARTED_COMMAND
    ack_received: bool = False
    attempts: int = 0
    next_send_at: float = 0.0
    retry_delay: float = PICO_STARTED_RETRY_DELAYS_SEC[0]
    first_attempt_at: float | None = None
    last_warning_at: float = 0.0

    def mark_ack(self) -> None:
        self.ack_received = True

    def maybe_send(
        self,
        fd: int,
        now: float | None = None,
        _logger: Any | None = None,
    ) -> bool:
        if self.ack_received:
            return False
        if _logger is None:
            raise ValueError("logger obrigatorio")
        current = time.monotonic() if now is None else now
        if current < self.next_send_at:
            return False

        self.attempts += 1
        if self.first_attempt_at is None:
            self.first_attempt_at = current
        sent = send_pico_command(fd, self.command, _logger)
        if sent:
            _logger.info(
                "[Pico] %s escrito na serial; aguardando %s (tentativa=%s)",
                self.command,
                PICO_ACK_STARTED,
                self.attempts,
            )

        elapsed = (
            current - self.first_attempt_at
            if self.first_attempt_at is not None
            else 0.0
        )
        if (
            self.attempts > 1
            and elapsed >= PICO_STARTED_WARNING_INTERVAL_SEC
            and (
                self.last_warning_at == 0.0
                or current - self.last_warning_at >= PICO_STARTED_WARNING_INTERVAL_SEC
            )
        ):
            _logger.warning(
                "[Pico] %s ainda sem ACK; reenviando (tentativa=%s)",
                self.command,
                self.attempts,
            )
            self.last_warning_at = current

        self.next_send_at = current + self.retry_delay
        index = PICO_STARTED_RETRY_DELAYS_SEC.index(self.retry_delay)
        self.retry_delay = PICO_STARTED_RETRY_DELAYS_SEC[
            min(len(PICO_STARTED_RETRY_DELAYS_SEC) - 1, index + 1)
        ]
        return sent


class PicoSerialController:
    """Single owner for Pico serial reads, writes, handshake and heartbeat."""

    def __init__(
        self,
        *,
        port: str,
        on_token: Callable[[str], None],
        logger: Any,
        heartbeat_interval_sec: float = 2.0,
    ) -> None:
        self.port = port
        self.on_token = on_token
        self.logger = logger
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.protocol_version = 1
        self.capabilities: set[str] = set()
        self.last_pong_at: float | None = None
        self._stop = threading.Event()
        self._outgoing: queue.Queue[str] = queue.Queue(maxsize=100)
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._connected.is_set()

    def has_recent_pong(self, max_age_sec: float = 6.0) -> bool:
        if self.protocol_version < 2:
            return self.is_running
        return (
            self.last_pong_at is not None
            and time.monotonic() - self.last_pong_at <= max_age_sec
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="pico-serial-controller",
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def send(self, command: str) -> bool:
        normalized = command.strip()
        if not normalized:
            return False
        try:
            self._outgoing.put_nowait(normalized)
            return True
        except queue.Full:
            self.logger.warning(
                "[Pico] Fila serial cheia; comando descartado: %s", normalized
            )
            return False

    def _handle_line(self, line: str, handshake: PicoStartedHandshake) -> None:
        upper = line.strip().upper()
        if upper == PICO_ACK_STARTED:
            handshake.mark_ack()
            self.logger.info("[Pico] Recebido ACK_GRN_STARTED")
            return
        if upper.startswith("PICO_CAPS:"):
            parts = upper.split(":", 2)
            try:
                self.protocol_version = int(parts[1])
            except (IndexError, ValueError):
                self.logger.warning("[Pico] Capacidades invalidas: %r", line)
                return
            self.capabilities = set(parts[2].split(",")) if len(parts) > 2 else set()
            self.logger.info(
                "[Pico] Protocolo v%s capacidades=%s",
                self.protocol_version,
                ",".join(sorted(self.capabilities)),
            )
            return
        if upper.startswith("PONG:"):
            self.last_pong_at = time.monotonic()
            return
        self.on_token(upper)

    def _run(self) -> None:
        try:
            fd = os.open(self.port, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
        except OSError as exc:
            self.logger.error("Falha ao abrir porta serial Pico (%s): %s", self.port, exc)
            return

        configure_pico_serial(fd, self.logger)
        time.sleep(PICO_STARTED_INITIAL_DELAY_SEC)
        handshake = PicoStartedHandshake()
        buffer = b""
        next_ping_at = time.monotonic() + self.heartbeat_interval_sec
        sequence = 0
        self._connected.set()
        self.logger.info("Controlador Pico serial ativo em %s", self.port)
        try:
            while not self._stop.is_set():
                handshake.maybe_send(fd, _logger=self.logger)
                now = time.monotonic()
                if handshake.ack_received and now >= next_ping_at:
                    sequence += 1
                    self.send(f"PING:{sequence}")
                    next_ping_at = now + self.heartbeat_interval_sec

                while True:
                    try:
                        command = self._outgoing.get_nowait()
                    except queue.Empty:
                        break
                    if not send_pico_command(fd, command, self.logger):
                        self.logger.warning("[Pico] Comando nao enviado: %s", command)
                        break

                try:
                    readable, _, _ = select.select([fd], [], [], 0.2)
                except OSError as exc:
                    self.logger.error("Erro no select() da serial Pico: %s", exc)
                    return
                if not readable:
                    continue
                try:
                    chunk = os.read(fd, 256)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    self.logger.error("Erro lendo serial Pico (%s): %s", self.port, exc)
                    return
                if not chunk:
                    continue
                buffer += chunk
                if len(buffer) > 4096 and b"\n" not in buffer:
                    self.logger.warning("[Pico] Linha serial excedeu 4096 bytes; descartando")
                    buffer = b""
                    continue
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if line:
                        self._handle_line(line, handshake)
        finally:
            self._connected.clear()
            os.close(fd)
