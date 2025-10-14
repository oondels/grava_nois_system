#!/bin/sh

#!/usr/bin/env bash
# Grava Nóis - Setup & Run Script
# Este script instala dependências, prepara o ambiente (.env) e sobe o container.
# Uso: bash gn_start.sh
#
# Opções:
#   --no-docker      Pula instalação/habilitação do Docker (se você já tem docker funcionando)
#   --no-pigpio      Pula instalação/habilitação do pigpio
#   --force-env      Força sobrescrever .env existente com o template
#   --start-only     Não instala nada; apenas 'docker compose up -d'
#
set -euo pipefail

PROJECT_ROOT="$(pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"
NO_DOCKER=0
NO_PIGPIO=0
FORCE_ENV=0
START_ONLY=0

log() { echo -e "\033[1;32m[grn]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
err() { echo -e "\033[1;31m[err]\033[0m $*" >&2; }

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "Comando obrigatório não encontrado: $1"
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-docker) NO_DOCKER=1; shift ;;
    --no-pigpio) NO_PIGPIO=1; shift ;;
    --force-env) FORCE_ENV=1; shift ;;
    --start-only) START_ONLY=1; shift ;;
    *) err "Parâmetro desconhecido: $1"; exit 1 ;;
  esac
done

# Detecta necessidade de sudo
SUDO=""
if [[ $EUID -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    err "Este script requer privilégios administrativos (root/sudo). Instale 'sudo' ou rode como root."
    exit 1
  fi
fi

# 0) Instalar dependências do sistema (Docker, FFmpeg, pigpio)
if [[ "$START_ONLY" -eq 0 ]]; then
  if [[ "$NO_DOCKER" -eq 0 ]]; then
    log "Atualizando pacotes e instalando Docker/Compose + ffmpeg..."
    $SUDO apt update -y
    $SUDO apt install -y docker.io docker-compose-plugin ffmpeg
    if ! command -v docker >/dev/null 2>&1; then
      err "Docker não foi instalado corretamente."
      exit 1
    fi
    log "Verificando serviço docker..."
    if $SUDO systemctl is-enabled docker >/dev/null 2>&1; then
      log "docker já está habilitado."
    else
      log "Habilitando serviço docker..."
      $SUDO systemctl enable docker
    fi
    log "Iniciando docker (se necessário)..."
    $SUDO systemctl start docker || true

    # Adiciona o usuário atual ao grupo docker (não efetivo até novo login)
    if id -nG "$USER" | grep -qw docker; then
      log "Usuário já pertence ao grupo 'docker'."
    else
      warn "Adicionando $USER ao grupo docker (você precisará relogar para efeito)."
      $SUDO usermod -aG docker "$USER" || true
    fi
  else
    warn "Pulando instalação do Docker por --no-docker"
  fi

  if [[ "$NO_PIGPIO" -eq 0 ]]; then
    log "Instalando e habilitando pigpio (para GPIO físico)..."
    $SUDO apt install -y pigpio
    $SUDO systemctl enable --now pigpiod || true
  else
    warn "Pulando instalação do pigpio por --no-pigpio"
  fi
fi

# 1) Criar arquivo .env (template) se não existir ou se --force-env
create_env() {
  cat > "$ENV_FILE" <<'EOF'
# ======== BACKEND ========
API_BASE_URL=
#API_TOKEN=
CLIENT_ID=
VENUE_ID=

# ======== MODO DE PROCESSAMENTO ========
# 1 = modo leve (sem watermark/thumbnail) | 0 = completo (futuro)
GN_LIGHT_MODE=1

# ======== CAPTURA / ENTRADA ========
# Framerate de entrada (usado quando webcam local via V4L2)
GN_INPUT_FRAMERATE=30
# Tamanho do vídeo (ex.: 1280x720) quando webcam local via V4L2
GN_VIDEO_SIZE=1280x720
# Segmentos de N segundos (típico: 1)
GN_SEG_TIME=1
# URL RTSP da câmera IP (substitua pelo seu endpoint)
GN_RTSP_URL=rtsp://user:pass@192.168.0.10:554/cam/realmonitor?channel=1&subtype=0

# ======== GPIO (opcional) ========
GPIO_PIN=17
GN_GPIO_COOLDOWN_SEC=120
GN_GPIO_DEBOUNCE_MS=300

# ======== MISC ========
#TERM=xterm-256color
EOF
}

if [[ -f "$ENV_FILE" && "$FORCE_ENV" -eq 0 ]]; then
  warn ".env já existe. Use --force-env para sobrescrever."
else
  log "Criando template .env em $ENV_FILE ..."
  create_env
fi

# 2.1) Criar estrutura de diretórios esperada
log "Garantindo estrutura de pastas..."
mkdir -p \
  "${PROJECT_ROOT}/recorded_clips" \
  "${PROJECT_ROOT}/queue_raw" \
  "${PROJECT_ROOT}/failed_clips" \
  "${PROJECT_ROOT}/files"

# 3) Checagens do Docker
if [[ "$NO_DOCKER" -eq 0 ]]; then
  if $SUDO systemctl is-enabled docker >/dev/null 2>&1; then
    log "docker habilitado ✅"
  else
    warn "docker está 'disabled'. Habilitando agora..."
    $SUDO systemctl enable docker || true
  fi
else
  warn "Pulando checagens do Docker (--no-docker)"
fi

# 4) Subir container
log "Subindo container com docker compose..."
if command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1; then
    docker compose up -d
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose up -d
  else
    err "Nenhum 'docker compose' encontrado (nem plugin v2, nem binário v1)."
    exit 1
  fi
else
  err "Docker não está disponível no PATH."
  exit 1
fi

log "Pronto! Logs em tempo real:"
echo "  docker logs -f grava_nois_system"
echo
log "Se você ajustou o grupo 'docker', faça logout/login para aplicar.
