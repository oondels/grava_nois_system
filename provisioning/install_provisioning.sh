#!/usr/bin/env bash
# =============================================================================
# Grava Nóis — Instalação do Provisionamento WiFi via Hotspot
# =============================================================================
# Instala e configura os pacotes necessários para o modo hotspot local.
# Idempotente: pode ser executado mais de uma vez sem efeitos colaterais.
#
# Uso:
#   sudo bash provisioning/install_provisioning.sh
#
# Deve ser executado como root a partir do diretório raiz do grava_nois_system.
# =============================================================================
set -euo pipefail

# --- cores -------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✔${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
err()  { echo -e "  ${RED}✘${RESET}  $*" >&2; }
step() { echo -e "\n${CYAN}${BOLD}▶ $*${RESET}"; }

# --- validações iniciais -----------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
  err "Execute como root: sudo bash provisioning/install_provisioning.sh"
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SYSTEM_DIR="$(dirname "${SCRIPT_DIR}")"
PROVISIONING_DIR="${SCRIPT_DIR}"
TEMPLATES_DIR="${PROVISIONING_DIR}/templates"
SYSTEMD_DIR="${PROVISIONING_DIR}/systemd"

# Detectar usuário do sistema (não root) que executou via sudo
SYSTEM_USER="${SUDO_USER:-}"
if [[ -z "${SYSTEM_USER}" || "${SYSTEM_USER}" == "root" ]]; then
  # Tentar detectar pelo proprietário do diretório do projeto
  SYSTEM_USER="$(stat -c '%U' "${SYSTEM_DIR}" 2>/dev/null || echo "")"
fi
if [[ -z "${SYSTEM_USER}" || "${SYSTEM_USER}" == "root" ]]; then
  err "Não foi possível detectar o usuário do sistema. Execute via sudo a partir do usuário correto."
  exit 1
fi

echo -e "\n${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║   Grava Nóis — Instalação de Provisionamento WiFi    ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
echo -e "  Usuário do sistema : ${BOLD}${SYSTEM_USER}${RESET}"
echo -e "  Diretório base     : ${BOLD}${SYSTEM_DIR}${RESET}"

# =============================================================================
# 1. Pacotes de sistema
# =============================================================================
step "Instalando pacotes de sistema"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

PACKAGES=(hostapd dnsmasq python3-flask wireless-tools)
TO_INSTALL=()

for pkg in "${PACKAGES[@]}"; do
  if dpkg -s "${pkg}" &>/dev/null; then
    ok "${pkg} já instalado"
  else
    TO_INSTALL+=("${pkg}")
  fi
done

if [[ ${#TO_INSTALL[@]} -gt 0 ]]; then
  apt-get install -y --no-install-recommends "${TO_INSTALL[@]}"
  ok "Instalado: ${TO_INSTALL[*]}"
fi

# =============================================================================
# 2. Desabilitar hostapd e dnsmasq no boot
# Eles são gerenciados pelos scripts de hotspot, não pelo systemd diretamente.
# =============================================================================
step "Desabilitando hostapd e dnsmasq no boot"

for svc in hostapd dnsmasq; do
  if systemctl is-enabled "${svc}" &>/dev/null; then
    systemctl disable --now "${svc}" 2>/dev/null || true
    ok "${svc} desabilitado no boot"
  else
    ok "${svc} já estava desabilitado"
  fi
  # Garantir que não estejam rodando
  systemctl stop "${svc}" 2>/dev/null || true
done

# =============================================================================
# 3. Entrada no sudoers
# =============================================================================
step "Configurando sudoers para o usuário ${SYSTEM_USER}"

SUDOERS_FILE="/etc/sudoers.d/gravanois-provisioning"
SUDOERS_CONTENT="# Grava Nóis — permissões para provisionamento WiFi sem senha
${SYSTEM_USER} ALL=(ALL) NOPASSWD: /usr/sbin/netplan, /usr/bin/netplan, /usr/sbin/hostapd, /usr/sbin/dnsmasq, /usr/sbin/ip, /usr/bin/ip
"

if [[ -f "${SUDOERS_FILE}" ]]; then
  # Verificar se o conteúdo já está correto
  if grep -qF "${SYSTEM_USER}" "${SUDOERS_FILE}" 2>/dev/null; then
    ok "Entrada sudoers já existe para ${SYSTEM_USER}"
  else
    warn "Arquivo sudoers existe mas com usuário diferente — sobrescrevendo"
    echo "${SUDOERS_CONTENT}" > "${SUDOERS_FILE}"
    chmod 0440 "${SUDOERS_FILE}"
    ok "Sudoers atualizado"
  fi
else
  echo "${SUDOERS_CONTENT}" > "${SUDOERS_FILE}"
  chmod 0440 "${SUDOERS_FILE}"

  # Validar sintaxe com visudo
  if visudo -c -f "${SUDOERS_FILE}" &>/dev/null; then
    ok "Sudoers criado e validado: ${SUDOERS_FILE}"
  else
    err "Erro de sintaxe no sudoers — removendo arquivo por segurança"
    rm -f "${SUDOERS_FILE}"
    exit 1
  fi
fi

# =============================================================================
# 4. Criar diretórios necessários
# =============================================================================
step "Criando estrutura de diretórios"

for dir in "${PROVISIONING_DIR}" "${TEMPLATES_DIR}" "${SYSTEMD_DIR}"; do
  if [[ -d "${dir}" ]]; then
    ok "Já existe: ${dir}"
  else
    mkdir -p "${dir}"
    ok "Criado: ${dir}"
  fi
done

# Diretório de logs do provisionamento
LOG_DIR="/var/log/grava-provisioning"
if [[ ! -d "${LOG_DIR}" ]]; then
  mkdir -p "${LOG_DIR}"
  chown "${SYSTEM_USER}:${SYSTEM_USER}" "${LOG_DIR}" 2>/dev/null || true
  ok "Criado: ${LOG_DIR}"
else
  ok "Já existe: ${LOG_DIR}"
fi

# =============================================================================
# 5. Instalar serviço systemd (se o arquivo existir)
# =============================================================================
step "Instalando serviço systemd grava-provisioning"

SERVICE_SRC="${SYSTEMD_DIR}/grava-provisioning.service"
SERVICE_DEST="/etc/systemd/system/grava-provisioning.service"

if [[ -f "${SERVICE_SRC}" ]]; then
  # Substituir placeholder do usuário e do diretório no arquivo de serviço
  sed \
    -e "s|{{SYSTEM_USER}}|${SYSTEM_USER}|g" \
    -e "s|{{SYSTEM_DIR}}|${SYSTEM_DIR}|g" \
    -e "s|{{PROVISIONING_DIR}}|${PROVISIONING_DIR}|g" \
    "${SERVICE_SRC}" > "${SERVICE_DEST}"
  chmod 644 "${SERVICE_DEST}"
  systemctl daemon-reload
  systemctl enable grava-provisioning.service
  ok "Serviço instalado e habilitado: grava-provisioning.service"
else
  warn "Arquivo de serviço não encontrado: ${SERVICE_SRC}"
  warn "Após criar o serviço em ${SERVICE_SRC}, execute novamente este script."
fi

# =============================================================================
# Resumo
# =============================================================================
echo -e "\n${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║           Instalação concluída com sucesso           ║${RESET}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${BOLD}Próximos passos:${RESET}"
echo -e "  1. Verifique/crie ${PROVISIONING_DIR}/templates/provisioning.html"
echo -e "  2. Configure o arquivo .env do sistema"
echo -e "  3. Reinicie para testar o boot sem WiFi configurado"
echo ""
ok "Provisionamento WiFi pronto."
