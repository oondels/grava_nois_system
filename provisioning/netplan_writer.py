"""
Grava Nóis — Módulo de escrita do Netplan
==========================================
Persiste credenciais WiFi no arquivo Netplan do sistema.

Nunca loga a senha em texto claro.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

NETPLAN_FILE = Path("/etc/netplan/50-cloud-init.yaml")
NETPLAN_BACKUP = Path("/etc/netplan/50-cloud-init.yaml.bak")


def write_wifi(ssid: str, password: str, interface: str) -> bool:
    """Atualiza o Netplan com as credenciais WiFi fornecidas e aplica.

    Args:
        ssid: Nome da rede WiFi alvo.
        password: Senha da rede (nunca logada em texto claro).
        interface: Nome da interface WiFi (ex: wlp2s0).

    Returns:
        True se o Netplan foi escrito e aplicado com sucesso, False caso contrário.
    """
    if not ssid or not interface:
        logger.error("SSID e interface são obrigatórios.")
        return False

    # Ler configuração atual
    current_config: dict = {}
    if NETPLAN_FILE.exists():
        try:
            with NETPLAN_FILE.open("r") as f:
                current_config = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            logger.error("Falha ao ler Netplan existente: %s", exc)
            return False

    # Garantir estrutura mínima do Netplan
    current_config.setdefault("network", {})
    current_config["network"].setdefault("version", 2)
    current_config["network"].setdefault("renderer", "networkd")
    current_config["network"].setdefault("wifis", {})

    # Montar entrada da interface WiFi preservando outras chaves existentes
    iface_config: dict = current_config["network"]["wifis"].get(interface, {})
    iface_config["dhcp4"] = True
    iface_config["optional"] = True
    iface_config["access-points"] = {
        ssid: {"password": password}
    }

    current_config["network"]["wifis"][interface] = iface_config

    # Fazer backup do arquivo anterior
    if NETPLAN_FILE.exists():
        try:
            shutil.copy2(NETPLAN_FILE, NETPLAN_BACKUP)
            logger.info("Backup do Netplan criado em %s", NETPLAN_BACKUP)
        except OSError as exc:
            logger.warning("Falha ao criar backup do Netplan: %s", exc)

    # Escrever novo arquivo
    try:
        netplan_str = yaml.dump(
            current_config,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        NETPLAN_FILE.write_text(netplan_str, encoding="utf-8")
        NETPLAN_FILE.chmod(0o600)
        logger.info("Netplan escrito para interface=%s ssid=%s", interface, ssid)
    except OSError as exc:
        logger.error("Falha ao escrever Netplan: %s", exc)
        return False

    # Aplicar Netplan
    try:
        result = subprocess.run(
            ["sudo", "netplan", "apply"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.error("netplan apply falhou (código %d): %s", result.returncode, result.stderr)
            _restore_backup()
            return False
        logger.info("netplan apply executado com sucesso.")
        return True
    except subprocess.TimeoutExpired:
        logger.error("netplan apply excedeu timeout de 30s.")
        _restore_backup()
        return False
    except Exception as exc:
        logger.error("Erro ao executar netplan apply: %s", exc)
        _restore_backup()
        return False


def restore_backup() -> bool:
    """Restaura o backup do Netplan (chamado externamente em caso de falha de conexão)."""
    return _restore_backup()


def _restore_backup() -> bool:
    if not NETPLAN_BACKUP.exists():
        logger.warning("Backup do Netplan não encontrado — não é possível restaurar.")
        return False
    try:
        shutil.copy2(NETPLAN_BACKUP, NETPLAN_FILE)
        logger.info("Netplan restaurado a partir do backup.")
        subprocess.run(["sudo", "netplan", "apply"], timeout=30, check=False)
        return True
    except Exception as exc:
        logger.error("Falha ao restaurar backup do Netplan: %s", exc)
        return False
