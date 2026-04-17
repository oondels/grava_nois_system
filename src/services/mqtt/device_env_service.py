"""Serviço MQTT para gerenciamento remoto de .env (admin-only).

Tópicos:
  - env/request  (inbound): backend solicita snapshot do .env atual
  - env/desired  (inbound): backend envia .env editado pelo admin
  - env/reported (outbound): edge publica snapshot/status

Todo conteúdo de .env trafega criptografado via envelope AES-256-GCM.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.security.env_envelope import open_env_envelope, seal_env_envelope
from src.security.hmac import hmac_sha256_base64
from src.services.docker_action_request import DockerActionRequestService
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger
from src.utils.logger import setup_logger

_AUDIT_CONSOLE_LEVEL = logging.CRITICAL + 1
_env_audit_logger = setup_logger(
    name="grava_nois_env_audit",
    file_name="env_audit.log",
    console_level=_AUDIT_CONSOLE_LEVEL,
    file_level=logging.INFO,
)

_ENV_KEYS_NEVER_LOG = {
    "DEVICE_SECRET",
    "GN_DEVICE_SECRET",
    "GN_API_TOKEN",
    "GN_MQTT_PASSWORD",
    "GN_MQTT_USERNAME",
}


def _audit_log(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    _env_audit_logger.info(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_env_keys(content: str) -> list[str]:
    """Extrai nomes de chaves do .env (sem valores)."""
    keys: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        eq = stripped.find("=")
        if eq > 0:
            keys.append(stripped[:eq].strip())
    return keys


def _content_hash(content: str) -> str:
    import hashlib
    import base64

    return base64.b64encode(
        hashlib.sha256(content.encode("utf-8")).digest()
    ).decode("ascii")


class DeviceEnvService:
    """Gerencia .env admin via MQTT com envelope criptografado."""

    def __init__(
        self,
        mqtt_client: MQTTClient,
        *,
        device_id: str,
        client_id: str,
        venue_id: str,
        request_topic: str,
        desired_topic: str,
        reported_topic: str,
        env_path: str | Path | None = None,
        device_secret: str = "",
        agent_version: str = "local-dev",
    ):
        self.mqtt_client = mqtt_client
        self.device_id = device_id
        self.client_id = client_id
        self.venue_id = venue_id
        self.request_topic = request_topic
        self.desired_topic = desired_topic
        self.reported_topic = reported_topic
        self.env_path = Path(
            env_path
            or os.getenv("GN_HOST_ENV_PATH", "/usr/src/app/host_config/.env")
        )
        self.device_secret = device_secret
        self.agent_version = agent_version
        self._connect_listener_registered = False

    def start(self) -> bool:
        if not self.mqtt_client.is_enabled:
            mqtt_logger.info("DeviceEnvService não iniciado: MQTT desabilitado")
            return False
        if not self.device_secret:
            mqtt_logger.warning(
                "DeviceEnvService não iniciado: DEVICE_SECRET ausente"
            )
            return False
        mqtt_logger.info(
            "DeviceEnvService iniciando: request_topic=%s desired_topic=%s reported_topic=%s env_path=%s has_secret=%s mqtt_connected=%s",
            self.request_topic,
            self.desired_topic,
            self.reported_topic,
            self.env_path,
            bool(self.device_secret),
            self.mqtt_client.is_connected,
        )
        if not self.env_path.exists():
            mqtt_logger.warning(
                "DeviceEnvService iniciado sem .env acessível em %s; sync admin retornará rejected até montar host_config/GN_HOST_ENV_PATH corretamente",
                self.env_path,
            )
        if not self._connect_listener_registered:
            self.mqtt_client.add_on_connect_listener(self._handle_mqtt_connect)
            self._connect_listener_registered = True
        request_subscribed = self.mqtt_client.subscribe(self.request_topic, self._handle_message)
        desired_subscribed = self.mqtt_client.subscribe(self.desired_topic, self._handle_message)
        mqtt_logger.info(
            "DeviceEnvService subscriptions registradas: request=%s desired=%s",
            request_subscribed,
            desired_subscribed,
        )
        if self.mqtt_client.is_connected:
            self._handle_mqtt_connect()
        return True

    def stop(self) -> None:
        pass

    def _handle_mqtt_connect(self) -> None:
        mqtt_logger.info("DeviceEnvService: conexão MQTT estabelecida")

    def _handle_message(self, topic: str, raw_payload: bytes) -> None:
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload deve ser um objeto JSON")

            _audit_log(
                "env_message_received",
                deviceId=self.device_id,
                topic=topic,
                messageType=payload.get("type"),
                requestId=payload.get("request_id"),
            )
            mqtt_logger.info(
                "DeviceEnvService recebeu mensagem: topic=%s type=%s request_id=%s",
                topic,
                payload.get("type"),
                payload.get("request_id"),
            )

            if topic == self.request_topic:
                self._handle_env_request(payload)
            elif topic == self.desired_topic:
                self._handle_env_desired(payload)
            else:
                raise ValueError(f"tópico de env não suportado: {topic}")

        except Exception as exc:
            _audit_log(
                "env_message_rejected",
                deviceId=self.device_id,
                topic=topic,
                reason=str(exc),
            )
            self._publish_error_report(
                request_id=_safe_str(raw_payload, "request_id"),
                reason=str(exc),
            )

    # ─── env/request: backend pede snapshot ────────────────────────────────

    def _handle_env_request(self, payload: dict[str, Any]) -> None:
        request_id = _required_str(payload, "request_id")
        msg_type = payload.get("type", "")
        mqtt_logger.info("DeviceEnvService processando env.request: request_id=%s", request_id)
        if msg_type != "env.request":
            raise ValueError(f"type inválido para env.request: {msg_type}")
        if _required_str(payload, "device_id") != self.device_id:
            raise ValueError("device_id divergente")

        # Verificar assinatura do request
        self._verify_request_signature(payload)
        mqtt_logger.info("DeviceEnvService env.request validado: request_id=%s", request_id)

        # Ler .env atual
        env_content = self._read_env_file()
        env_hash = _content_hash(env_content)
        mqtt_logger.info(
            "DeviceEnvService .env lido para snapshot: request_id=%s env_path=%s key_count=%s",
            request_id,
            self.env_path,
            len(_parse_env_keys(env_content)),
        )

        # Criar envelope criptografado
        envelope = seal_env_envelope(
            device_secret=self.device_secret,
            request_id=request_id,
            device_id=self.device_id,
            plaintext=env_content,
        )

        report: dict[str, Any] = {
            "type": "env.reported",
            "device_id": self.device_id,
            "client_id": self.client_id,
            "venue_id": self.venue_id,
            "request_id": request_id,
            "status": "snapshot",
            "env_hash": env_hash,
            "env_keys": _parse_env_keys(env_content),
            "envelope": envelope,
            "reported_at": _now_iso(),
            "agent_version": self.agent_version,
        }

        published = self.mqtt_client.publish_json(self.reported_topic, report)
        mqtt_logger.info(
            "DeviceEnvService publicou env.reported snapshot: request_id=%s topic=%s published=%s",
            request_id,
            self.reported_topic,
            published,
        )
        _audit_log(
            "env_snapshot_sent",
            deviceId=self.device_id,
            requestId=request_id,
            envHash=env_hash,
            keyCount=len(_parse_env_keys(env_content)),
            published=published,
        )

    # ─── env/desired: backend envia .env editado ──────────────────────────

    def _handle_env_desired(self, payload: dict[str, Any]) -> None:
        request_id = _required_str(payload, "request_id")
        msg_type = payload.get("type", "")
        mqtt_logger.info("DeviceEnvService processando env.desired: request_id=%s", request_id)
        if msg_type != "env.desired":
            raise ValueError(f"type inválido para env.desired: {msg_type}")
        if _required_str(payload, "device_id") != self.device_id:
            raise ValueError("device_id divergente")

        envelope_data = payload.get("envelope")
        if not isinstance(envelope_data, dict):
            raise ValueError("envelope ausente ou inválido")

        restart_after_apply = bool(payload.get("restart_after_apply", False))

        # Descriptografar e validar envelope
        new_env_content = open_env_envelope(
            device_secret=self.device_secret,
            envelope=envelope_data,
        )

        # Validar conteúdo básico do .env
        self._validate_env_content(new_env_content)

        # Ler estado anterior para auditoria
        old_content = ""
        old_hash = ""
        if self.env_path.exists():
            old_content = self._read_env_file()
            old_hash = _content_hash(old_content)

        # Criar backup
        backup_path = self._create_backup()

        # Escrita atômica
        self._write_env_atomic(new_env_content)

        new_hash = _content_hash(new_env_content)
        old_keys = set(_parse_env_keys(old_content))
        new_keys = set(_parse_env_keys(new_env_content))
        changed_keys = list((old_keys ^ new_keys) | self._diff_keys(old_content, new_env_content))
        # Filtrar chaves sensíveis da auditoria de changed_keys
        safe_changed = [k for k in changed_keys if k not in _ENV_KEYS_NEVER_LOG]

        status = "applied_requires_restart"

        _audit_log(
            "env_applied",
            deviceId=self.device_id,
            requestId=request_id,
            oldHash=old_hash,
            newHash=new_hash,
            changedKeys=safe_changed,
            backupPath=str(backup_path) if backup_path else None,
            status=status,
            restartAfterApply=restart_after_apply,
        )

        report: dict[str, Any] = {
            "type": "env.reported",
            "device_id": self.device_id,
            "client_id": self.client_id,
            "venue_id": self.venue_id,
            "request_id": request_id,
            "status": status,
            "env_hash": new_hash,
            "env_keys": _parse_env_keys(new_env_content),
            "reported_at": _now_iso(),
            "agent_version": self.agent_version,
        }

        published = self.mqtt_client.publish_json(self.reported_topic, report)
        mqtt_logger.info(
            "DeviceEnvService publicou env.reported apply: request_id=%s topic=%s published=%s",
            request_id,
            self.reported_topic,
            published,
        )

        if restart_after_apply:
            self._schedule_restart()

    # ─── Helpers internos ─────────────────────────────────────────────────

    def _read_env_file(self) -> str:
        if not self.env_path.exists():
            raise ValueError(f".env não encontrado: {self.env_path}")
        return self.env_path.read_text(encoding="utf-8")

    def _validate_env_content(self, content: str) -> None:
        """Validação básica: não pode ser binário, deve ter pelo menos uma chave."""
        if "\x00" in content:
            raise ValueError(".env contém bytes nulos (possivelmente binário)")
        lines = content.splitlines()
        has_key = any(
            "=" in line and not line.strip().startswith("#")
            for line in lines
            if line.strip()
        )
        if not has_key and content.strip():
            raise ValueError(".env não contém nenhuma chave válida (formato KEY=VALUE)")

    def _create_backup(self) -> Path | None:
        if not self.env_path.exists():
            return None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = self.env_path.with_suffix(f".bak.grn.{timestamp}")
        shutil.copy2(str(self.env_path), str(backup_path))
        mqtt_logger.info("Backup .env criado: %s", backup_path)
        return backup_path

    def _write_env_atomic(self, content: str) -> None:
        """Escrita atômica: escreve em temp, move para destino, aplica chmod 600."""
        parent = self.env_path.parent
        parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            dir=str(parent), prefix=".env.tmp.", suffix=".grn"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
            os.replace(tmp_path, str(self.env_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _diff_keys(self, old_content: str, new_content: str) -> set[str]:
        """Retorna chaves cujos valores mudaram."""
        old_map = self._parse_env_map(old_content)
        new_map = self._parse_env_map(new_content)
        changed: set[str] = set()
        for key in old_map.keys() | new_map.keys():
            if old_map.get(key) != new_map.get(key):
                changed.add(key)
        return changed

    @staticmethod
    def _parse_env_map(content: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            eq = stripped.find("=")
            if eq > 0:
                result[stripped[:eq].strip()] = stripped[eq + 1:]
        return result

    def _verify_request_signature(self, payload: dict[str, Any]) -> None:
        """Verifica HMAC do request de sync."""
        if not self.device_secret:
            raise ValueError("DEVICE_SECRET ausente para validar request")
        signature = _required_str(payload, "signature")
        canonical = ":".join(
            [
                "v1",
                "ENV_REQUEST",
                _required_str(payload, "device_id"),
                _required_str(payload, "request_id"),
                _required_str(payload, "requested_at"),
            ]
        )
        import hmac as hmac_mod

        expected = hmac_sha256_base64(self.device_secret, canonical)
        if not hmac_mod.compare_digest(signature, expected):
            raise ValueError("assinatura de env.request inválida")

    def _publish_error_report(self, request_id: str | None, reason: str) -> None:
        report: dict[str, Any] = {
            "type": "env.reported",
            "device_id": self.device_id,
            "client_id": self.client_id,
            "venue_id": self.venue_id,
            "request_id": request_id or "",
            "status": "rejected",
            "rejection_reason": reason[:200],
            "reported_at": _now_iso(),
            "agent_version": self.agent_version,
        }
        published = self.mqtt_client.publish_json(self.reported_topic, report)
        mqtt_logger.info(
            "DeviceEnvService publicou env.reported rejeitado: request_id=%s topic=%s published=%s",
            request_id or "",
            self.reported_topic,
            published,
        )

    def _schedule_restart(self) -> None:
        """Agenda restart do container Docker após publicar status."""
        mqtt_logger.info(
            "Restart solicitado após aplicar .env; agendando em 3 segundos..."
        )
        import threading

        def _do_restart() -> None:
            _audit_log("env_restart_triggered", deviceId=self.device_id)
            mqtt_logger.info("Solicitando recriacao do container via runner Docker do host...")
            requested = DockerActionRequestService.from_env(
                logger=mqtt_logger
            ).request_action(
                "restart_container",
                source="admin_env",
                fallback_on_failure=True,
            )
            if requested:
                return
            mqtt_logger.warning(
                "Runner Docker indisponivel; aplicando fallback com TERM no PID 1"
            )
            os.system("kill -TERM 1")  # noqa: S605 - PID 1 = init do container

        threading.Timer(3.0, _do_restart).start()


# ─── Helpers de parsing ──────────────────────────────────────────────────────


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"campo obrigatório ausente: {key}")
    return value.strip()


def _safe_str(raw_payload: bytes, key: str) -> str | None:
    try:
        data = json.loads(raw_payload.decode("utf-8"))
        return str(data.get(key, ""))
    except Exception:
        return None
