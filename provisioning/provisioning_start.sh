#!/usr/bin/env bash
# =============================================================================
# Grava Nóis — Script Orquestrador de Provisionamento
# =============================================================================
# Executado pelo grava-provisioning.service no boot.
# Coordena: wifi_check → (se sem WiFi) hotspot_up + provisioning_server.
#
# Se já há WiFi ativo: encerra imediatamente (código 0).
# Se não há WiFi: sobe hotspot e bloqueia até o servidor encerrar.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${LOG_FILE:-/var/log/grava-provisioning/service.log}"

mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null || true

log() {
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[${ts}] [provisioning_start] $*" | tee -a "${LOG_FILE}"
}

# =============================================================================
# 1. Verificar conectividade WiFi
# =============================================================================
log "Iniciando verificação de conectividade WiFi..."

if bash "${SCRIPT_DIR}/wifi_check.sh"; then
  log "WiFi ativo — hotspot não necessário. Encerrando."
  exit 0
fi

log "Sem WiFi — iniciando modo hotspot."

# =============================================================================
# 2. Subir hotspot
# =============================================================================
if ! sudo bash "${SCRIPT_DIR}/hotspot_up.sh"; then
  log "ERRO: Falha ao subir hotspot. Encerrando com código 1."
  exit 1
fi

log "Hotspot ativo. Iniciando servidor de provisionamento..."

# =============================================================================
# 3. Iniciar servidor Flask (bloqueante — mantém o serviço ativo)
# =============================================================================
PROVISIONING_PORT="${PROVISIONING_PORT:-80}"
export PROVISIONING_PORT
export LOG_FILE

# Verificar se o Python e o Flask estão disponíveis
if ! command -v python3 &>/dev/null; then
  log "ERRO: python3 não encontrado. Instale via install_provisioning.sh."
  exit 1
fi

log "Iniciando provisioning_server.py na porta ${PROVISIONING_PORT}..."
exec sudo python3 "${SCRIPT_DIR}/provisioning_server.py"
