"""Solicitacoes seguras para o host executar manutencao Docker.

O container nao deve receber /var/run/docker.sock. Em vez disso, ele escreve um
arquivo de intencao em runtime_config; uma unit systemd no host executa a acao.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.infrastructure.filesystem.deferred_repository import LockBusy, exclusive_file
from src.security.durable_state import private_json as atomic_json
from src.security.env_control import timestamp


def _is_truthy(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class ActionSubmission:
    code: str
    request_id: str

    @property
    def accepted(self) -> bool:
        return self.code in {"created", "already_known"}


class DockerActionRequestService:
    def __init__(
        self,
        *,
        enabled: bool,
        request_path: Path,
        pull_token: str,
        restart_token: str,
        shutdown_enabled: bool = False,
        shutdown_token: str = "SHUTDOWN_HOST",
        logger: Any | None = None,
    ) -> None:
        self.enabled = enabled
        self.request_path = request_path
        self.pull_token = pull_token.strip().upper()
        self.restart_token = restart_token.strip().upper()
        self.shutdown_enabled = shutdown_enabled
        self.shutdown_token = shutdown_token.strip().upper()
        self.logger = logger

    @classmethod
    def from_env(cls, logger: Any | None = None) -> DockerActionRequestService:
        return cls(
            enabled=_is_truthy(os.getenv("GN_PICO_DOCKER_ACTIONS_ENABLED"), True),
            request_path=Path(
                os.getenv(
                    "GN_DOCKER_ACTION_REQUEST_PATH",
                    "/usr/src/app/runtime_config/docker-action.request.json",
                )
            ),
            pull_token=os.getenv("GN_PICO_DOCKER_PULL_TOKEN", "PULL_DOCKER"),
            restart_token=os.getenv("GN_PICO_DOCKER_RESTART_TOKEN", "RESTART_DOCKER"),
            shutdown_enabled=_is_truthy(os.getenv("GN_PICO_HOST_SHUTDOWN_ENABLED"), False),
            shutdown_token=os.getenv("GN_PICO_HOST_SHUTDOWN_TOKEN", "SHUTDOWN_HOST"),
            logger=logger,
        )

    def handle_token(self, token: str) -> bool:
        normalized = token.strip().upper()
        action = self._action_for_token(normalized)
        if not action:
            return False

        self.request_action(action, source="pico", token=normalized)
        # Consumed token is not an execution receipt.
        return True

    @property
    def action_root(self) -> Path:
        return self.request_path.parent / "device-actions"

    def request_action(
        self,
        action: str,
        *,
        source: str,
        token: str | None = None,
        fallback_on_failure: bool = False,
        request_id: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> bool:
        return self.submit_action(
            action, source=source, token=token, request_id=request_id, parameters=parameters
        ).accepted

    def submit_action(
        self,
        action: str,
        *,
        source: str,
        token: str | None = None,
        request_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        expires_at: str | None = None,
    ) -> ActionSubmission:
        request_id = request_id or str(uuid.uuid4())

        def result(code):
            return ActionSubmission(code, request_id)

        if action not in {
            "pull_and_recreate",
            "restart_container",
            "shutdown_host",
            "reboot_host",
            "change_wifi",
        }:
            return result("invalid_action")
        try:
            if str(uuid.UUID(request_id)) != request_id:
                return result("invalid_request_id")
        except (ValueError, TypeError, AttributeError):
            return result("invalid_request_id")
        source = "pico" if source == "pico" else "mqtt"
        if action == "shutdown_host" and (source != "pico" or not self.shutdown_enabled):
            return result("disabled")
        now = datetime.now(UTC)
        expires_at = expires_at or (now + timedelta(seconds=120)).isoformat()
        secret_path = None
        target = self.action_root / "requests" / f"{request_id}.json"
        try:
            with exclusive_file(self.action_root / "admission.lock"):
                for folder in ("requests", "processing", "results", "receipts"):
                    known = self.action_root / folder / f"{request_id}.json"
                    if known.exists():
                        previous = json.loads(known.read_text())
                        if previous.get("action", action) != action:
                            return result("request_id_conflict")
                        return result("already_known")
                if not self.enabled:
                    return result("disabled")
                if timestamp(expires_at) <= now or timestamp(expires_at) > now + timedelta(
                    seconds=150
                ):
                    return result("expired")
                if (
                    self.request_path.exists()
                    or self.request_path.with_name("docker-action.processing.json").exists()
                    or any((self.action_root / "requests").glob("*.json"))
                    or any((self.action_root / "processing").glob("*.json"))
                ):
                    return result("busy")
                payload = {
                    "schema_version": 2,
                    "request_id": request_id,
                    "requested_at": now.isoformat(),
                    "expires_at": expires_at,
                    "source": source,
                    "action": action,
                    "parameters": {},
                }
                if token:
                    payload["token"] = token
                if action == "change_wifi":
                    values = parameters or {}
                    ssid, password = values.get("ssid"), values.get("wifi_password")
                    valid_password = isinstance(password, str) and (
                        8 <= len(password) <= 63
                        or (
                            len(password) == 64
                            and all(c in "0123456789abcdefABCDEF" for c in password)
                        )
                    )
                    if (
                        not isinstance(ssid, str)
                        or not 1 <= len(ssid.encode()) <= 32
                        or not valid_password
                    ):
                        return result("invalid_wifi")
                    secret_dir = Path(
                        os.getenv("GN_HOST_ACTION_SECRET_DIR", "/usr/src/app/host_actions")
                    )
                    secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    secret_path = secret_dir / f"{request_id}.wifi.json"
                    atomic_json(secret_path, {"ssid": ssid, "password": password})
                    payload["parameters"] = {"ssid": ssid, "secret_ref": secret_path.name}
                target = self.action_root / "requests" / f"{request_id}.json"
                atomic_json(target, payload)
                return result("created")
        except LockBusy:
            return result("busy")
        except (OSError, ValueError, TypeError):
            # Admission was unlocked by unwinding. The host may already have
            # claimed/completed a published request before we inspect its path.
            try:
                with exclusive_file(self.action_root / "admission.lock"):
                    if any(
                        (self.action_root / folder / f"{request_id}.json").exists()
                        for folder in ("requests", "processing", "results", "receipts")
                    ):
                        return result("uncertain")
            except (LockBusy, OSError):
                return result("uncertain")
            if secret_path is not None:
                secret_path.unlink(missing_ok=True)
            self._log("error", "Falha ao persistir solicitacao do host")
            return result("persistence_failed")

    def _action_for_token(self, token: str) -> str | None:
        if token == self.pull_token:
            return "pull_and_recreate"
        if token == self.restart_token:
            return "restart_container"
        if token == self.shutdown_token:
            return "shutdown_host"
        return None

    def _log(self, level: str, message: str, *args: object) -> None:
        if self.logger is not None:
            getattr(self.logger, level)(message, *args)
