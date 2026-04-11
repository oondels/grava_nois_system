from __future__ import annotations

import json
import queue
import ssl
import threading
from collections.abc import Callable
from typing import Any

from src.config.settings import MQTTConfig
from src.utils.logger import setup_logger

try:
    import paho.mqtt.client as paho_mqtt
except ImportError:  # pragma: no cover - exercised via runtime fallback
    paho_mqtt = None


MQTTMessageHandler = Callable[[str, bytes], None]
mqtt_logger = setup_logger("grava_nois.mqtt", file_name="mqtt.log")


class MQTTClient:
    """Light wrapper around paho-mqtt with safe lifecycle and logging."""

    def __init__(self, config: MQTTConfig):
        self.config = config
        self._subscriptions: dict[str, MQTTMessageHandler] = {}
        self._connect_listeners: list[Callable[[], None]] = []
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._handler_queue: queue.Queue[tuple[MQTTMessageHandler, str, bytes]] = queue.Queue()
        self._handler_thread: threading.Thread | None = None
        self._handler_stop = threading.Event()
        self._client = self._build_client()

    @property
    def is_available(self) -> bool:
        return self._client is not None

    @property
    def is_enabled(self) -> bool:
        return self.config.enabled

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def _build_client(self):
        if not self.config.enabled:
            mqtt_logger.info("MQTT desabilitado por configuração")
            return None
        if not self.config.host:
            mqtt_logger.warning(
                "MQTT habilitado, mas broker não configurado (GN_MQTT_BROKER_URL/GN_MQTT_HOST)"
            )
            return None
        if paho_mqtt is None:
            mqtt_logger.warning(
                "paho-mqtt não está instalado; o edge seguirá sem MQTT ativo"
            )
            return None

        client = paho_mqtt.Client(client_id=self.config.client_id)
        if self.config.username:
            client.username_pw_set(self.config.username, self.config.password)
        if self.config.use_tls:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=120)
        return client

    def configure_last_will(self, topic: str, payload: dict[str, Any], *, retain: bool) -> None:
        if not self._client:
            return
        self._client.will_set(
            topic,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            qos=self.config.qos,
            retain=retain,
        )

    def _handler_worker(self) -> None:
        """Consome mensagens MQTT enfileiradas e executa handlers fora do loop Paho."""
        while not self._handler_stop.is_set():
            try:
                handler, topic, payload = self._handler_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                handler(topic, payload)
            except Exception as exc:
                mqtt_logger.exception("Erro no handler MQTT para %s: %s", topic, exc)

    def start(self) -> bool:
        if not self._client:
            return False
        with self._lock:
            if self._started:
                return True
            try:
                self._handler_stop.clear()
                self._handler_thread = threading.Thread(
                    target=self._handler_worker, daemon=True, name="mqtt-handler",
                )
                self._handler_thread.start()
                self._client.connect_async(
                    host=self.config.host,
                    port=self.config.port,
                    keepalive=self.config.keepalive,
                )
                self._client.loop_start()
                self._started = True
                mqtt_logger.info(
                    "Cliente MQTT iniciado: host=%s port=%s tls=%s client_id=%s",
                    self.config.host,
                    self.config.port,
                    self.config.use_tls,
                    self.config.client_id,
                )
                return True
            except Exception as exc:
                mqtt_logger.exception("Falha ao iniciar cliente MQTT: %s", exc)
                self._handler_stop.set()
                return False

    def stop(self) -> None:
        if not self._client:
            return
        with self._lock:
            if not self._started:
                return
            self._handler_stop.set()
            if self._handler_thread is not None:
                self._handler_thread.join(timeout=2.0)
            try:
                self._client.disconnect()
            except Exception as exc:
                mqtt_logger.warning("Erro no disconnect MQTT: %s", exc)
            try:
                self._client.loop_stop()
            except Exception as exc:
                mqtt_logger.warning("Erro ao parar loop MQTT: %s", exc)
            self._started = False
            self._connected.clear()

    def publish_json(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        retain: bool = False,
        qos: int | None = None,
    ) -> bool:
        if not self._client:
            return False
        if not self._started or not self.is_connected:
            mqtt_logger.debug("Publish ignorado sem conexão ativa: topic=%s", topic)
            return False
        try:
            info = self._client.publish(
                topic,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                qos=self.config.qos if qos is None else qos,
                retain=retain,
            )
            success = info.rc == paho_mqtt.MQTT_ERR_SUCCESS
            if not success:
                mqtt_logger.warning(
                    "Falha no publish MQTT: topic=%s rc=%s",
                    topic,
                    info.rc,
                )
            return success
        except Exception as exc:
            mqtt_logger.exception("Erro ao publicar em %s: %s", topic, exc)
            return False

    def subscribe(
        self,
        topic: str,
        handler: MQTTMessageHandler,
        *,
        qos: int | None = None,
    ) -> bool:
        self._subscriptions[topic] = handler
        if not self._client:
            return False
        if not self.is_connected:
            return True
        try:
            self._client.subscribe(topic, qos=self.config.qos if qos is None else qos)
            mqtt_logger.info("Inscrito no tópico MQTT %s", topic)
            return True
        except Exception as exc:
            mqtt_logger.exception("Falha ao inscrever no tópico %s: %s", topic, exc)
            return False

    def add_on_connect_listener(self, callback: Callable[[], None]) -> None:
        self._connect_listeners.append(callback)

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None):
        code = getattr(reason_code, "value", reason_code)
        if code == 0:
            self._connected.set()
            mqtt_logger.info("Conectado ao broker MQTT")
            for topic in self._subscriptions:
                try:
                    client.subscribe(topic, qos=self.config.qos)
                    mqtt_logger.info("Inscrição restaurada: %s", topic)
                except Exception as exc:
                    mqtt_logger.warning(
                        "Falha ao restaurar inscrição MQTT %s: %s",
                        topic,
                        exc,
                    )
            for callback in self._connect_listeners:
                try:
                    callback()
                except Exception as exc:
                    mqtt_logger.warning("Falha em listener pós-connect MQTT: %s", exc)
        else:
            self._connected.clear()
            mqtt_logger.warning("Conexão MQTT rejeitada: rc=%s", code)

    def _on_disconnect(self, _client, _userdata, reason_code, _properties=None):
        code = getattr(reason_code, "value", reason_code)
        self._connected.clear()
        if code in {0, None}:
            mqtt_logger.info("Cliente MQTT desconectado com limpeza")
        else:
            mqtt_logger.warning("Cliente MQTT desconectado inesperadamente: rc=%s", code)

    def _on_message(self, _client, _userdata, msg):
        topic = getattr(msg, "topic", "")
        payload = getattr(msg, "payload", b"")
        handler = self._subscriptions.get(topic)
        if handler is None:
            mqtt_logger.debug("Mensagem MQTT sem handler: topic=%s", topic)
            return
        self._handler_queue.put((handler, topic, payload))
