#!/usr/bin/env bash
# =============================================================================
# Grava Nóis — Subida do Hotspot WiFi
# =============================================================================
# Configura e sobe o hotspot usando hostapd + dnsmasq.
# Executado pelo grava-provisioning.service quando wifi_check.sh retorna 1.
#
# O SSID inclui os últimos 4 chars do MAC address para identificação única.
# Rede aberta (sem senha) para facilitar acesso do cliente via celular.
#
# Uso:
#   sudo bash provisioning/hotspot_up.sh
# =============================================================================
set -euo pipefail

HOTSPOT_IP="192.168.4.1"
DHCP_RANGE_START="192.168.4.10"
DHCP_RANGE_END="192.168.4.50"
DHCP_LEASE="12h"
WIFI_CHANNEL="${HOTSPOT_CHANNEL:-6}"
LOG_FILE="${LOG_FILE:-/var/log/grava-provisioning/hotspot.log}"
PID_FILE="/tmp/hotspot.pid"

mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null || true

log() {
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[${ts}] $*" | tee -a "${LOG_FILE}"
}

die() {
  log "ERRO: $*"
  exit 1
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
[[ -z "${WIFI_IFACE}" ]] && die "Nenhuma interface WiFi detectada."

log "Interface WiFi: ${WIFI_IFACE}"

# =============================================================================
# 2. Extrair MAC e compor SSID
# =============================================================================
MAC_ADDR="$(cat "/sys/class/net/${WIFI_IFACE}/address" 2>/dev/null || true)"
[[ -z "${MAC_ADDR}" ]] && die "Não foi possível ler o MAC de ${WIFI_IFACE}."

# Últimos 4 chars do MAC sem separadores (ex: AA:BB:CC:DD:EE:FF → EEFF)
MAC_SUFFIX="$(echo "${MAC_ADDR}" | tr -d ':' | tail -c 5 | tr '[:lower:]' '[:upper:]')"
SSID="GravaNois-${MAC_SUFFIX}"

log "MAC: ${MAC_ADDR} | SSID: ${SSID}"

# =============================================================================
# 3. Parar processos anteriores (se houver)
# =============================================================================
if [[ -f "${PID_FILE}" ]]; then
  log "PID file existente detectado — encerrando processos anteriores..."
  while IFS= read -r pid; do
    kill "${pid}" 2>/dev/null || true
  done < "${PID_FILE}"
  rm -f "${PID_FILE}"
  sleep 1
fi

# Garantir que não há instâncias perdidas
pkill -f "hostapd /tmp/hostapd.conf" 2>/dev/null || true
pkill -f "dnsmasq --conf-file=/tmp/dnsmasq-hotspot.conf" 2>/dev/null || true
sleep 1

# =============================================================================
# 4. Configurar IP estático na interface
# =============================================================================
log "Configurando IP estático ${HOTSPOT_IP}/24 em ${WIFI_IFACE}..."

# Remover IPs anteriores e configurar o estático
ip addr flush dev "${WIFI_IFACE}" 2>/dev/null || true
ip addr add "${HOTSPOT_IP}/24" dev "${WIFI_IFACE}"
ip link set "${WIFI_IFACE}" up

log "IP configurado: ${HOTSPOT_IP}/24 em ${WIFI_IFACE}"

# =============================================================================
# 5. Gerar configuração do hostapd
# =============================================================================
HOSTAPD_CONF="/tmp/hostapd.conf"

cat > "${HOSTAPD_CONF}" <<EOF
interface=${WIFI_IFACE}
driver=nl80211
ssid=${SSID}
hw_mode=g
channel=${WIFI_CHANNEL}
ieee80211n=1
wmm_enabled=1
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
EOF

log "Configuração hostapd gerada: ${HOSTAPD_CONF}"

# =============================================================================
# 6. Gerar configuração do dnsmasq
# =============================================================================
DNSMASQ_CONF="/tmp/dnsmasq-hotspot.conf"

cat > "${DNSMASQ_CONF}" <<EOF
interface=${WIFI_IFACE}
bind-interfaces
dhcp-range=${DHCP_RANGE_START},${DHCP_RANGE_END},${DHCP_LEASE}
# Captive portal: todo DNS resolve para o dispositivo
address=/#/${HOTSPOT_IP}
log-queries
no-resolv
EOF

log "Configuração dnsmasq gerada: ${DNSMASQ_CONF}"

# =============================================================================
# 7. Subir hostapd em background
# =============================================================================
log "Iniciando hostapd..."
hostapd "${HOSTAPD_CONF}" &>/tmp/hostapd.out &
HOSTAPD_PID=$!

sleep 2

if ! kill -0 "${HOSTAPD_PID}" 2>/dev/null; then
  die "hostapd falhou ao iniciar. Verifique /tmp/hostapd.out"
fi

log "hostapd rodando (PID ${HOSTAPD_PID})"

# =============================================================================
# 8. Subir dnsmasq em background
# =============================================================================
log "Iniciando dnsmasq..."
dnsmasq --conf-file="${DNSMASQ_CONF}" --pid-file=/tmp/dnsmasq-hotspot.pid &>/tmp/dnsmasq-hotspot.out &
DNSMASQ_PID=$!

sleep 1

if ! kill -0 "${DNSMASQ_PID}" 2>/dev/null; then
  die "dnsmasq falhou ao iniciar. Verifique /tmp/dnsmasq-hotspot.out"
fi

log "dnsmasq rodando (PID ${DNSMASQ_PID})"

# =============================================================================
# 9. Salvar PIDs para derrubada controlada
# =============================================================================
printf '%s\n' "${HOSTAPD_PID}" "${DNSMASQ_PID}" > "${PID_FILE}"

log "PIDs salvos em ${PID_FILE}: hostapd=${HOSTAPD_PID}, dnsmasq=${DNSMASQ_PID}"
log "Hotspot '${SSID}' ativo em ${HOTSPOT_IP} | DHCP: ${DHCP_RANGE_START}–${DHCP_RANGE_END}"
