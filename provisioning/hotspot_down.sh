#!/usr/bin/env bash
# =============================================================================
# Grava Nóis — Derrubada do Hotspot WiFi
# =============================================================================
# Para o hotspot e restaura a interface WiFi para modo cliente normal.
# Executado pelo provisioning_server.py após configuração bem-sucedida.
#
# Uso:
#   sudo bash provisioning/hotspot_down.sh
# =============================================================================
set -euo pipefail

PID_FILE="/tmp/hotspot.pid"
WAIT_SECONDS="${WIFI_RECONNECT_WAIT:-30}"
LOG_FILE="${LOG_FILE:-/var/log/grava-provisioning/hotspot.log}"

mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null || true

log() {
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[${ts}] $*" | tee -a "${LOG_FILE}"
}

# =============================================================================
# 1. Detectar interface WiFi
# =============================================================================
detect_wifi_interface() {
  local iface=""

  if command -v iw &>/dev/null; then
    iface="$(iw dev 2>/dev/null | awk '/Interface/ {print $2}' | head -1)"
  fi

  if [[ -z "${iface}" ]]; then
    for dev in /sys/class/net/*/wireless; do
      if [[ -d "${dev}" ]]; then
        iface="$(basename "$(dirname "${dev}")")"
        break
      fi
    done
  fi

  echo "${iface}"
}

WIFI_IFACE="$(detect_wifi_interface)"

# =============================================================================
# 2. Encerrar hostapd e dnsmasq via PID file
# =============================================================================
log "Encerrando hotspot..."

if [[ -f "${PID_FILE}" ]]; then
  while IFS= read -r pid; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null && log "Processo ${pid} encerrado." || log "Falha ao encerrar PID ${pid}."
    else
      log "PID ${pid} já não está em execução."
    fi
  done < "${PID_FILE}"
  rm -f "${PID_FILE}"
else
  log "PID file não encontrado — encerrando por nome de processo..."
fi

# Garantir encerramento por nome (fallback)
pkill -f "hostapd /tmp/hostapd.conf" 2>/dev/null && log "hostapd encerrado via pkill." || true
pkill -f "dnsmasq --conf-file=/tmp/dnsmasq-hotspot.conf" 2>/dev/null && log "dnsmasq encerrado via pkill." || true

# Aguardar encerramento
sleep 2

# Limpar arquivo PID do dnsmasq (se existir)
rm -f /tmp/dnsmasq-hotspot.pid

# =============================================================================
# 3. Remover IP estático da interface
# =============================================================================
if [[ -n "${WIFI_IFACE}" ]]; then
  ip addr del "192.168.4.1/24" dev "${WIFI_IFACE}" 2>/dev/null \
    && log "IP 192.168.4.1 removido de ${WIFI_IFACE}." \
    || log "IP 192.168.4.1 já não estava na interface (ou interface ausente)."
fi

# =============================================================================
# 4. Restaurar configuração via Netplan
# =============================================================================
log "Aplicando Netplan para restaurar WiFi cliente..."

if sudo netplan apply 2>&1 | tee -a "${LOG_FILE}"; then
  log "Netplan aplicado com sucesso."
else
  log "AVISO: netplan apply retornou erro — conexão pode demorar mais."
fi

# =============================================================================
# 5. Aguardar reconexão
# =============================================================================
if [[ -z "${WIFI_IFACE}" ]]; then
  log "Interface WiFi não detectada — não é possível aguardar reconexão."
  exit 0
fi

log "Aguardando reconexão WiFi em ${WIFI_IFACE} (até ${WAIT_SECONDS}s)..."

ELAPSED=0
SLEEP_INTERVAL=2

while [[ ${ELAPSED} -lt ${WAIT_SECONDS} ]]; do
  IP="$(ip -4 addr show "${WIFI_IFACE}" 2>/dev/null \
        | awk '/inet / {print $2}' \
        | grep -v '^169\.254\.' \
        | head -1)"

  if [[ -n "${IP}" ]]; then
    log "Reconectado! Interface ${WIFI_IFACE} com IP ${IP}."
    exit 0
  fi

  sleep "${SLEEP_INTERVAL}"
  ELAPSED=$(( ELAPSED + SLEEP_INTERVAL ))
  log "Aguardando IP em ${WIFI_IFACE}... (${ELAPSED}/${WAIT_SECONDS}s)"
done

log "AVISO: sem IP em ${WIFI_IFACE} após ${WAIT_SECONDS}s. Verifique credenciais no Netplan."
exit 1
