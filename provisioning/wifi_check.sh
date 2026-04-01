#!/usr/bin/env bash
# =============================================================================
# Grava Nóis — Verificação de Conectividade WiFi
# =============================================================================
# Executado no boot pelo grava-provisioning.service.
# Decide se o dispositivo já tem WiFi configurado e ativo.
#
# Saída:
#   0 — WiFi ativo e com IP válido (sistema pode iniciar normalmente)
#   1 — Sem WiFi configurado ou falha ao conectar (acionar modo hotspot)
#
# Uso:
#   bash provisioning/wifi_check.sh
# =============================================================================
set -euo pipefail

NETPLAN_FILE="${NETPLAN_FILE:-/etc/netplan/50-cloud-init.yaml}"
WAIT_SECONDS="${WIFI_CHECK_WAIT:-30}"
LOG_FILE="${LOG_FILE:-/var/log/grava-provisioning/wifi_check.log}"

# Garantir que o diretório de logs existe
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

  # Preferir iw dev (mais confiável)
  if command -v iw &>/dev/null; then
    iface="$(iw dev 2>/dev/null | awk '/Interface/ {print $2}' | head -1)"
  fi

  # Fallback: procurar em /sys/class/net por interfaces wireless
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

if [[ -z "${WIFI_IFACE}" ]]; then
  log "ERRO: Nenhuma interface WiFi detectada. Saindo com código 1 (acionar hotspot)."
  exit 1
fi

log "Interface WiFi detectada: ${WIFI_IFACE}"

# =============================================================================
# 2. Verificar se há SSID configurado no Netplan
# =============================================================================
if [[ ! -f "${NETPLAN_FILE}" ]]; then
  log "Arquivo Netplan não encontrado: ${NETPLAN_FILE}. Saindo com código 1."
  exit 1
fi

# Checar se há seção 'wifis' com pelo menos um SSID definido
if ! grep -qE '^\s+wifis:' "${NETPLAN_FILE}" 2>/dev/null; then
  log "Nenhuma seção 'wifis' encontrada no Netplan. Acionar hotspot."
  exit 1
fi

# Verificar se há ao menos uma entrada de access-points (SSID configurado)
if ! grep -qE '^\s+access-points:' "${NETPLAN_FILE}" 2>/dev/null; then
  log "Nenhum access-point configurado no Netplan. Acionar hotspot."
  exit 1
fi

log "Netplan com WiFi configurado encontrado. Aguardando conexão (até ${WAIT_SECONDS}s)..."

# =============================================================================
# 3. Aguardar IP válido na interface WiFi
# =============================================================================
ELAPSED=0
SLEEP_INTERVAL=2

while [[ ${ELAPSED} -lt ${WAIT_SECONDS} ]]; do
  # Verificar se a interface tem IP atribuído (exclui 169.254.x.x = APIPA)
  IP="$(ip -4 addr show "${WIFI_IFACE}" 2>/dev/null \
        | awk '/inet / {print $2}' \
        | grep -v '^169\.254\.' \
        | head -1)"

  if [[ -n "${IP}" ]]; then
    log "WiFi ativo na interface ${WIFI_IFACE} com IP ${IP}. Saindo com código 0."
    exit 0
  fi

  sleep "${SLEEP_INTERVAL}"
  ELAPSED=$(( ELAPSED + SLEEP_INTERVAL ))
  log "Aguardando IP em ${WIFI_IFACE}... (${ELAPSED}/${WAIT_SECONDS}s)"
done

log "Timeout: sem IP em ${WIFI_IFACE} após ${WAIT_SECONDS}s. Acionar hotspot."
exit 1
