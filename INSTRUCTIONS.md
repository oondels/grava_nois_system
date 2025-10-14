# Instruções de Instalação e Execução — Grava Nóis System

Este documento explica como preparar e rodar o sistema **Grava Nóis** em um equipamento novo (Raspberry Pi ou servidor Linux).

---

## 0. Instalar dependências do sistema

No host (fora do Docker):

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose ffmpeg pigpio
```

---

## 1. Criar arquivo `.env`

Na raiz do projeto, crie um arquivo `.env` com as variáveis abaixo:

```ini
API_BASE_URL=
CLIENT_ID=
VENUE_ID=
GN_LIGHT_MODE=1
GN_INPUT_FRAMERATE=30
GN_SEG_TIME=1
GN_VIDEO_SIZE=1280x720
GPIO_PIN=17
GN_GPIO_COOLDOWN_SEC=120
GN_GPIO_DEBOUNCE_MS=300
GN_RTSP_URL=rtsp://user:pass@ip:554/cam/realmonitor?channel=1&subtype=0
#TERM=xterm-256color
#API_TOKEN=
```

> **Atenção**: Ajuste `GN_RTSP_URL` para a URL da sua câmera IP ou use `/dev/video0` para webcam local.

---

## 2. Ativar serviço `pigpiod` (para GPIO físico no Raspberry Pi)

```bash
sudo apt install -y pigpio
sudo systemctl enable --now pigpiod
```

> Se não for usar botão físico, pode ignorar esta etapa.

---

## 3. Comandos de verificação para Docker

Verifique se o serviço do Docker já está habilitado:

```bash
sudo systemctl is-enabled docker
```

Se a resposta for `disabled`, habilite-o:

```bash
sudo systemctl enable docker
```

---

## 4. Inicializar o container Docker

Na raiz do projeto:

```bash
docker compose up -d
```

O container `grava_nois_system` será iniciado em segundo plano. Ele:

* Abre captura da câmera via `ffmpeg`
* Mantém buffer circular de segmentos (`.ts`)
* Aguarda **ENTER** no teclado ou disparo via **GPIO**
* Constrói clipes (`.mp4`) e os envia ao backend via **URL assinada**

---

## 5. Estrutura de diretórios

* `recorded_clips/` — clipes recém‑gerados
* `queue_raw/` — clipes aguardando upload
* `failed_clips/` — clipes com falha
* `files/` — arquivos auxiliares (ex: watermark)

---

## 6. Logs

Para acompanhar logs em tempo real:

```bash
docker logs -f grava_nois_system
```

---

## 7. Encerrar o sistema

```bash
docker compose down
```

---
