from __future__ import annotations
from typing import Any
from src.services.docker_action_request import DockerActionRequestService

class CommandExecutor:
    def __init__(self, action_service: DockerActionRequestService | None = None) -> None:
        self.action_service = action_service or DockerActionRequestService.from_env()
    def execute(self, command_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id", ""))
        accepted = self.action_service.request_action(
            command_name, source="admin_remote", request_id=request_id,
            parameters=payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {},
        )
        return {"device_id": payload.get("device_id"), "request_id": request_id,
                "command": command_name, "status": "accepted" if accepted else "failed",
                "error_code": None if accepted else "host_intent_rejected"}
