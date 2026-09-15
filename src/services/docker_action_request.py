"""Solicitacoes seguras para o host executar manutencao Docker.

O container nao deve receber /var/run/docker.sock. Em vez disso, ele escreve um
arquivo de intencao em runtime_config; uma unit systemd no host executa a acao.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _is_truthy(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


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
    def from_env(cls, logger: Any | None = None) -> "DockerActionRequestService":
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
            shutdown_enabled=_is_truthy(
                os.getenv("GN_PICO_HOST_SHUTDOWN_ENABLED"), False
            ),
            shutdown_token=os.getenv("GN_PICO_HOST_SHUTDOWN_TOKEN", "SHUTDOWN_HOST"),
            logger=logger,
        )

    def handle_token(self, token: str) -> bool:
        normalized = token.strip().upper()
        action = self._action_for_token(normalized)
        if not action:
            return False

        return self.request_action(action, source="pico", token=normalized)

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
        if action not in {"pull_and_recreate", "restart_container", "shutdown_host", "reboot_host", "change_wifi"}:
            self._log("warning", "Acao Docker invalida ignorada: %s", action)
            return False

        if not self.enabled:
            self._log("warning", "Acao Docker ignorada: recurso desabilitado")
            return not fallback_on_failure

        if action == "shutdown_host" and not self.shutdown_enabled:
            self._log("warning", "Desligamento do host ignorado: recurso desabilitado")
            return not fallback_on_failure

        if self.request_path.exists():
            self._log(
                "warning",
                "Acao Docker ignorada: ja existe requisicao pendente em %s",
                self.request_path,
            )
            return True

        try:
            secret_path: Path | None = None
            payload: dict[str, Any] = {
                "schema_version": 1,
                "request_id": request_id or str(uuid.uuid4()),
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "action": action,
                "pid": os.getpid(),
            }
            if token:
                payload["token"] = token
            if action == "change_wifi":
                values = parameters or {}
                ssid = values.get("ssid")
                password = values.get("wifi_password")
                if not isinstance(ssid, str) or not isinstance(password, str):
                    raise ValueError("Credenciais Wi-Fi invalidas")
                secret_dir = Path(os.getenv("GN_HOST_ACTION_SECRET_DIR", "/usr/src/app/host_actions"))
                secret_dir.mkdir(parents=True, exist_ok=True)
                secret_path = secret_dir / f"{payload['request_id']}.wifi.json"
                secret_path.write_text(json.dumps({"ssid": ssid, "password": password}, ensure_ascii=False))
                os.chmod(secret_path, 0o600)
                payload["parameters"] = {"ssid": ssid, "secret_ref": secret_path.name}
            self.request_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.request_path.with_name(f".{self.request_path.name}.tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            os.chmod(tmp_path, 0o600)
            tmp_path.replace(self.request_path)
            self._log(
                "warning",
                "Acao Docker solicitada: %s source=%s (request_id=%s)",
                action,
                source,
                payload["request_id"],
            )
        except Exception as exc:
            if "secret_path" in locals() and secret_path is not None:
                secret_path.unlink(missing_ok=True)
            self._log(
                "error",
                "Falha ao registrar acao Docker em %s: %s",
                self.request_path,
                exc,
            )
            return not fallback_on_failure
        return True

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
