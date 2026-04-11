"""
Grava Nóis — Servidor Flask de Provisionamento WiFi
====================================================
Roda na porta 80 enquanto o hotspot está ativo.
Permite ao cliente configurar a rede WiFi do dispositivo via browser.

Endpoints:
  GET  /          — página HTML de configuração (provisioning.html)
  GET  /scan      — lista de redes WiFi disponíveis (JSON)
  POST /configure — recebe ssid + password, testa e aplica
  GET  /status    — estado atual do processo de conexão

Senha nunca é logada em texto claro.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# Configuração de logging
# ---------------------------------------------------------------------------
LOG_DIR = Path(os.environ.get("LOG_FILE", "/var/log/grava-provisioning/server.log")).parent
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "server.log"),
    ],
)
logger = logging.getLogger("provisioning_server")

# ---------------------------------------------------------------------------
# Inicialização do Flask
# ---------------------------------------------------------------------------
TEMPLATES_DIR = Path(__file__).parent / "templates"
app = Flask(__name__, template_folder=str(TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Estado compartilhado (thread-safe via lock simples)
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_connection_state: str = "hotspot"   # hotspot | connecting | connected | failed
_connection_error: str = ""


def _detect_wifi_interface() -> str:
    try:
        out = subprocess.check_output(["iw", "dev"], text=True, timeout=5)
        for line in out.splitlines():
            if "Interface" in line:
                return line.strip().split()[-1]
    except Exception:
        pass
    for path in Path("/sys/class/net").iterdir():
        if (path / "wireless").is_dir():
            return path.name
    return ""


# Interface WiFi detectada na inicialização
_WIFI_IFACE: str = _detect_wifi_interface()
_HOTSPOT_DOWN_SCRIPT = Path(__file__).parent / "hotspot_down.sh"


def _set_state(state: str, error: str = "") -> None:
    with _state_lock:
        global _connection_state, _connection_error
        _connection_state = state
        _connection_error = error


def _get_state() -> tuple[str, str]:
    with _state_lock:
        return _connection_state, _connection_error


# ---------------------------------------------------------------------------
# Helpers de scan
# ---------------------------------------------------------------------------

def _scan_networks() -> list[dict]:
    """Retorna lista de redes WiFi disponíveis sem expor senhas."""
    networks: list[dict] = []

    # Tentar nmcli primeiro (mais confiável e parseable)
    if _try_nmcli(networks):
        return networks

    # Fallback: iwlist
    _try_iwlist(networks)
    return networks


def _try_nmcli(networks: list[dict]) -> bool:
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            text=True,
            timeout=15,
            stderr=subprocess.DEVNULL,
        )
        seen: set[str] = set()
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) < 2:
                continue
            ssid = parts[0].strip()
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            try:
                signal = int(parts[1])
            except (ValueError, IndexError):
                signal = 0
            security = parts[2].strip() if len(parts) > 2 else "open"
            networks.append({"ssid": ssid, "signal": signal, "security": security})
        networks.sort(key=lambda x: x["signal"], reverse=True)
        return bool(networks)
    except Exception:
        return False


def _try_iwlist(networks: list[dict]) -> None:
    if not _WIFI_IFACE:
        return
    try:
        out = subprocess.check_output(
            ["sudo", "iwlist", _WIFI_IFACE, "scan"],
            text=True,
            timeout=15,
            stderr=subprocess.DEVNULL,
        )
        seen: set[str] = set()
        current: dict = {}
        for line in out.splitlines():
            line = line.strip()
            m_ssid = re.search(r'ESSID:"([^"]*)"', line)
            m_signal = re.search(r"Signal level=(-?\d+)", line)
            m_enc = re.search(r"Encryption key:(on|off)", line)

            if "Cell" in line and current.get("ssid"):
                if current["ssid"] not in seen:
                    seen.add(current["ssid"])
                    networks.append(current)
                current = {}

            if m_ssid:
                current["ssid"] = m_ssid.group(1)
                current.setdefault("signal", 0)
                current.setdefault("security", "open")
            if m_signal:
                current["signal"] = int(m_signal.group(1))
            if m_enc:
                current["security"] = "WPA" if m_enc.group(1) == "on" else "open"

        if current.get("ssid") and current["ssid"] not in seen:
            networks.append(current)

        networks.sort(key=lambda x: x.get("signal", 0), reverse=True)
    except Exception as exc:
        logger.warning("iwlist scan falhou: %s", exc)


# ---------------------------------------------------------------------------
# Thread de tentativa de conexão
# ---------------------------------------------------------------------------

def _attempt_connection(ssid: str, password: str, interface: str) -> None:
    """Roda em thread separada. Testa conexão e aciona hotspot_down se sucesso."""
    from provisioning import netplan_writer  # import local para evitar ciclo

    _set_state("connecting")
    logger.info("Tentando conectar à rede: %s (interface: %s)", ssid, interface)

    success = netplan_writer.write_wifi(ssid, password, interface)
    if not success:
        logger.error("netplan_writer.write_wifi falhou para ssid=%s", ssid)
        _set_state("failed", "Falha ao escrever configuração de rede.")
        return

    # Aguardar conexão (até 20s)
    WAIT = 20
    INTERVAL = 2
    elapsed = 0

    while elapsed < WAIT:
        time.sleep(INTERVAL)
        elapsed += INTERVAL
        ip = _get_wifi_ip(interface)
        if ip:
            logger.info("Conectado! Interface=%s IP=%s", interface, ip)
            _set_state("connected")
            # Derrubar hotspot em background após pequeno delay
            threading.Thread(target=_run_hotspot_down, daemon=True).start()
            return

    # Timeout — reverter Netplan
    logger.warning("Timeout ao conectar em %s. Revertendo Netplan.", ssid)
    netplan_writer.restore_backup()
    _set_state("failed", "Não foi possível conectar. Verifique a senha e tente novamente.")


def _get_wifi_ip(interface: str) -> str:
    try:
        out = subprocess.check_output(
            ["ip", "-4", "addr", "show", interface],
            text=True,
            timeout=5,
        )
        for line in out.splitlines():
            if "inet " in line:
                ip = line.strip().split()[1]
                if not ip.startswith("169.254."):
                    return ip
    except Exception:
        pass
    return ""


def _run_hotspot_down() -> None:
    time.sleep(2)  # pequeno delay para o cliente receber a resposta /status
    logger.info("Acionando hotspot_down.sh...")
    try:
        subprocess.run(
            ["sudo", "bash", str(_HOTSPOT_DOWN_SCRIPT)],
            timeout=60,
            check=False,
        )
    except Exception as exc:
        logger.error("Erro ao acionar hotspot_down.sh: %s", exc)


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("provisioning.html")


@app.route("/scan")
def scan():
    """Retorna lista de redes WiFi disponíveis."""
    try:
        networks = _scan_networks()
        return jsonify({"ok": True, "networks": networks})
    except Exception as exc:
        logger.error("Erro em /scan: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/configure", methods=["POST"])
def configure():
    """Recebe SSID + senha, inicia tentativa de conexão em background."""
    data = request.get_json(force=True, silent=True) or {}
    ssid = (data.get("ssid") or "").strip()
    password = (data.get("password") or "").strip()

    if not ssid:
        return jsonify({"ok": False, "error": "SSID é obrigatório."}), 400

    state, _ = _get_state()
    if state == "connecting":
        return jsonify({"ok": False, "error": "Já há uma tentativa em andamento."}), 409

    logger.info("POST /configure ssid=%s (senha omitida do log)", ssid)

    interface = _WIFI_IFACE
    if not interface:
        return jsonify({"ok": False, "error": "Interface WiFi não detectada."}), 500

    thread = threading.Thread(
        target=_attempt_connection,
        args=(ssid, password, interface),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "message": "Conectando..."})


@app.route("/status")
def status():
    """Retorna estado atual da tentativa de conexão."""
    state, error = _get_state()
    return jsonify({"state": state, "error": error})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PROVISIONING_PORT", 80))
    logger.info("Servidor de provisionamento iniciando na porta %d", port)
    # Usar host="0.0.0.0" para aceitar conexões do hotspot
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
