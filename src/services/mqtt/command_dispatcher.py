from __future__ import annotations
import base64, hashlib, hmac, json, os, threading
from pathlib import Path
from typing import Any
from src.services.mqtt.command_executor import CommandExecutor
from src.services.mqtt.command_policy import CommandPolicy
from src.services.mqtt.mqtt_client import MQTTClient, mqtt_logger

def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

class CommandDispatcher:
    def __init__(self, mqtt_client: MQTTClient, *, device_id: str, command_in_topic: str,
                 command_out_topic: str, device_secret: str = "", policy: CommandPolicy | None = None,
                 executor: CommandExecutor | None = None, ledger_path: Path | None = None,
                 result_path: Path | None = None):
        self.mqtt_client, self.device_id, self.device_secret = mqtt_client, device_id, device_secret
        self.command_in_topic, self.command_out_topic = command_in_topic, command_out_topic
        self.policy, self.executor = policy or CommandPolicy(), executor or CommandExecutor()
        runtime = Path(os.getenv("GN_RUNTIME_CONFIG_DIR", "/usr/src/app/runtime_config"))
        self.ledger_path = ledger_path or runtime / "device-operation-ledger.json"
        self.result_path = result_path or runtime / "docker-action.last.json"
        self._stop = threading.Event()
    def start(self) -> bool:
        if not self.mqtt_client.is_enabled: return False
        ok = self.mqtt_client.subscribe(self.command_in_topic, self._handle_message)
        if ok: threading.Thread(target=self._report_results, daemon=True, name="device-operation-reporter").start()
        return ok
    def stop(self) -> None: self._stop.set()
    def _sign(self, payload: dict[str, Any]) -> str:
        digest = hmac.new(self.device_secret.encode(), canonical_json(payload).encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()
    def _publish(self, response: dict[str, Any]) -> None:
        unsigned = {**response, "signature_version": "hmac-sha256-v1"}
        self.mqtt_client.publish_json(self.command_out_topic, {**unsigned, "signature": self._sign(unsigned)}, retain=False, qos=1)
    def _load_ledger(self) -> dict[str, str]:
        try:
            value = json.loads(self.ledger_path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError): return {}
    def _store_ledger(self, ledger: dict[str, str]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger_path.with_name(f".{self.ledger_path.name}.tmp")
        tmp.write_text(json.dumps(dict(list(ledger.items())[-200:])))
        os.chmod(tmp, 0o600); tmp.replace(self.ledger_path)
    def _decrypt_wifi(self, parameters: dict[str, Any]) -> dict[str, Any]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        encrypted = parameters.get("password_encrypted")
        if not isinstance(encrypted, dict) or encrypted.get("algorithm") != "aes-256-gcm":
            raise ValueError("invalid encrypted Wi-Fi secret")
        key = hashlib.sha256(f"grn-device-wifi-v1:{self.device_secret}".encode()).digest()
        raw = AESGCM(key).decrypt(base64.b64decode(encrypted["iv"]),
                                  base64.b64decode(encrypted["ciphertext"]) + base64.b64decode(encrypted["tag"]), None)
        return {"ssid": parameters.get("ssid"), "wifi_password": raw.decode()}
    def _handle_message(self, topic: str, raw_payload: bytes) -> None:
        try:
            payload = json.loads(raw_payload.decode())
            signature = str(payload.pop("signature"))
            if payload.get("device_id") != self.device_id or payload.get("signature_version") != "hmac-sha256-v1":
                raise ValueError("identity mismatch")
            if not hmac.compare_digest(signature, self._sign(payload)): raise ValueError("invalid signature")
            request_id, command = str(payload.get("request_id", "")), str(payload.get("command", ""))
            if not request_id: raise ValueError("missing request id")
            ledger = self._load_ledger()
            if request_id in ledger:
                self._publish({"device_id": self.device_id, "request_id": request_id, "command": command, "status": ledger[request_id]}); return
            allowed, reason = self.policy.is_allowed(command, payload)
            if not allowed:
                status = "expired" if reason == "command expired" else "failed"
                ledger[request_id] = status; self._store_ledger(ledger)
                self._publish({"device_id": self.device_id, "request_id": request_id, "command": command,
                               "status": status, "error_code": reason.replace(" ", "_")}); return
            if command == "change_wifi": payload["parameters"] = self._decrypt_wifi(payload.get("parameters") or {})
            response = self.executor.execute(command, payload)
            ledger[request_id] = str(response["status"]); self._store_ledger(ledger); self._publish(response)
        except Exception as exc:
            mqtt_logger.warning("Comando remoto invalido ignorado: topic=%s reason=%s", topic, exc)
    def _report_results(self) -> None:
        last_mtime = 0
        while not self._stop.wait(2):
            try:
                stat = self.result_path.stat()
                if stat.st_mtime_ns == last_mtime: continue
                last_mtime = stat.st_mtime_ns
                result = json.loads(self.result_path.read_text())
                request_id = str(result.get("request_id", ""))
                if not request_id: continue
                status = "succeeded" if result.get("status") == "ok" else "failed"
                ledger = self._load_ledger()
                if ledger.get(request_id) == status: continue
                ledger[request_id] = status; self._store_ledger(ledger)
                safe = {k: v for k, v in result.items() if k in {"action", "status", "completed_at", "stage"}}
                self._publish({"device_id": self.device_id, "request_id": request_id,
                               "command": result.get("action"), "status": status, "result": safe,
                               "error_code": result.get("error_code")})
            except (OSError, ValueError): continue
