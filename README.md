# Grava Nóis System — Sistema de Captura de Vídeos

> **Objetivo:** Capturar replays com pré/pós-buffer, gerar highlights, aplicar crop vertical opcional e marca d'água local, e fazer upload automático para backend via URL assinada. Otimizado para rodar em Raspberry Pi.
>
> **Regra de operação:** O sistema respeita janela de horário comercial configurável no trigger local e também descarta clipes rejeitados pela API por restrição de horário.
>
> **Presença operacional:** MQTT pode ser habilitado para publicar `online/offline`, heartbeat e estado resumido do device sem ativar comandos remotos nesta fase.

Lookup principal para auditoria e navegação técnica: [`docs/specs/DESIGN_SPEC.md`](docs/specs/DESIGN_SPEC.md).

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-required-green.svg)](https://ffmpeg.org/)

---

## 📋 Índice

- [Arquitetura do Sistema](#arquitetura-do-sistema)
- [Início Rápido](#início-rápido)
- [Fluxo de Funcionamento](#fluxo-de-funcionamento)
- [Estrutura de Diretórios](#estrutura-de-diretórios)
- [Configuração](#configuração)
- [Presença MQTT](#presença-mqtt)
- [Otimização de Captura RTSP](#otimização-de-captura-rtsp)
- [Provisionamento WiFi (Hotspot)](#provisionamento-wifi-hotspot)
- [GPIO (Botão Físico)](#gpio-botão-físico)
- [Modo Leve (Light Mode)](#modo-leve-light-mode)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)

---

## 🏗️ Arquitetura do Sistema

### Arquivos Principais

- **`main.py`**: Bootstrap, orquestração de câmeras, listeners de trigger e fan-out
- **`src/config/settings.py`**: Parsing de `CaptureConfig`, MQTT e fontes de câmera
- **`src/config/config_loader.py`**: Resolução da configuração operacional (`config.json` -> env legado -> defaults)
- **`src/video/`**: Captura FFmpeg, buffer circular e montagem de highlight
- **`src/workers/processing_worker.py`**: Worker de processamento, watermark, upload e retry
- **`src/utils/logger.py`**: Sistema de logging centralizado
- **`src/services/api_client.py`**: Cliente HTTP para comunicação com backend
- **`src/services/mqtt/`**: Cliente MQTT, presença do device, configuração remota e bloqueio explícito de command/control

### Dependências

- Python 3.10+
- FFmpeg/ffprobe
- pigpio (opcional, para GPIO)
- requests, python-dotenv e paho-mqtt (em `requirements.txt`)

### Fluxo Simplificado

```
[ Câmera RTSP/V4L2 ]
       ↓
[ FFmpeg - Segmentos de 1s ]
       ↓
[ SegmentBuffer - Mantém buffer circular ]
       ↓
[ ENTER / GPIO / Pico serial → build_highlight() ]
   ├─ token dedicado → câmera específica
   └─ token global / ENTER / GPIO → fan-out (câmeras sem token dedicado)
       ↓
[ Enqueue → queue_raw/ ]
       ↓
[ ProcessingWorker (1 por câmera) ]
   ├─ Crop vertical opcional
   ├─ Watermark local (HQ ou leve)
   └─ Upload via API
       ↓
[ Backend (URL assinada) ]

[ MQTT Presence Service ]
   ├─ Presence retained
   ├─ Heartbeat periódico
   └─ Estado resumido do runtime
       ↓
[ Broker MQTT ]
```

---

## 🚀 Início Rápido

### 1. Instalação no Raspberry Pi

```bash
# Atualizar sistema e instalar dependências
sudo apt update && sudo apt install -y ffmpeg pigpio python3-venv
sudo systemctl enable --now pigpiod

# Clonar repositório (ou transferir arquivos)
cd /home/pi/grava_nois_system

# Criar ambiente virtual
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Configuração Básica

Crie um arquivo `.env` na raiz do projeto:

```bash
# Configuração da câmera RTSP (obrigatório)
GN_RTSP_URL=rtsp://user:pass@192.168.1.100:554/cam/realmonitor?channel=1&subtype=0

# API Backend (opcional, mas recomendado)
GN_API_BASE=https://api.gravanois.com
GN_API_TOKEN=seu_token_jwt_aqui
GN_CLIENT_ID=uuid-do-cliente
GN_VENUE_ID=uuid-do-local
DEVICE_ID=raspberrypi-001
DEVICE_SECRET=troque_por_um_segredo_forte

# GPIO (opcional)
GN_GPIO_PIN=17
GN_GPIO_COOLDOWN_SEC=120

# Janela de funcionamento local (opcional)
GN_TIME_ZONE=America/Sao_Paulo
GN_START_TIME=07:00
GN_END_TIME=23:30

# Modo leve (recomendado para Pi 3B/1GB)
GN_LIGHT_MODE=1

# Modo desenvolvimento (sem chamadas externas)
DEV=true

# Dry-run da assinatura HMAC (sem chamar backend)
GN_HMAC_DRY_RUN=0

# MQTT opcional para presença do device
GN_MQTT_ENABLED=1
GN_MQTT_BROKER_URL=mqtt://broker.gravanois.local:1883
GN_MQTT_CLIENT_ID=raspberrypi-001
GN_MQTT_HEARTBEAT_INTERVAL_SEC=30
GN_MQTT_TOPIC_PREFIX=grn
GN_AGENT_VERSION=1.0.0-edge

# Configurações de buffer
GN_SEG_TIME=1
GN_RTSP_PRE_SEGMENTS=6
GN_RTSP_POST_SEGMENTS=3
```

Observacao operacional:

- o runtime continua representando **um device logico por processo/host provisionado**;
- o backend pode associar varios devices a mesma venue, entao varios hosts podem compartilhar o mesmo `GN_CLIENT_ID` e `GN_VENUE_ID`;
- nesses casos, cada host precisa manter `DEVICE_ID` e `DEVICE_SECRET` proprios.

### 3. Executar

```bash
source .venv/bin/activate
python3 main.py
```

**Gerar highlight:** Pressione `ENTER` no terminal, o botão físico conectado ao GPIO ou o botão Pico serial.

---

## 🔄 Fluxo de Funcionamento

### 1. Captura Contínua

O FFmpeg captura vídeo da fonte (RTSP ou V4L2) e grava segmentos de 1 segundo em `/dev/shm/grn_buffer/`:

```
buffer000000.ts
buffer000001.ts
buffer000002.ts
...
```

### 2. Buffer Circular

O `SegmentBuffer` mantém apenas os últimos ~40 segundos de vídeo, apagando segmentos antigos automaticamente.

### 3. Trigger (ENTER, GPIO ou Pico serial)

Ao pressionar ENTER, botão físico (GPIO) ou botão Pico serial:

1. Sistema valida se o horário atual está dentro da janela `GN_START_TIME` → `GN_END_TIME` no fuso `GN_TIME_ZONE`
2. Se estiver fora da janela, o trigger é ignorado e o `build_highlight()` não é executado
3. Se estiver dentro da janela, aguarda `post_seconds` (padrão: 3 segmentos = 3s)
4. Seleciona `pre_segments + post_segments` (padrão: 6 + 3 = 9 segmentos)
5. Gera manifesto de concat e concatena diretamente para `.mp4` temporário com `ffmpeg -c copy`
6. Promove o arquivo temporário para `recorded_clips/highlight_{camera_id}_{timestamp}.mp4`

### 4. Enfileiramento

O vídeo é movido para `queue_raw/` junto com um arquivo JSON contendo metadados:

```json
{
  "type": "highlight_raw",
  "status": "queued",
  "created_at": "2026-02-13T10:00:00Z",
  "file_name": "highlight_cam01_20260213-100000-123456Z.mp4",
  "size_bytes": 1234567,
  "sha256": "abc123...",
  "meta": {
    "codec": "h264",
    "width": 1280,
    "height": 720,
    "fps": 30.0,
    "duration_sec": 9.0
  },
  "pre_seconds": 6,
  "post_seconds": 3,
  "pre_segments": 6,
  "post_segments": 3,
  "seg_time": 1
}
```

### 5. Processamento (Worker)

O `ProcessingWorker` varre a fila periodicamente:

**Modo Normal:**
1. Aplica watermark sempre, com encode de alta qualidade (`GN_HQ_CRF` + `GN_HQ_PRESET`)
2. Se `VERTICAL_FORMAT=1`, recorta o clipe para `9:16` sem scale forçado
3. Salva o resultado em `highlights_wm/` e atualiza o sidecar com `meta_wm`, `wm_path` e `wm_encode`
4. Registra metadados no backend → recebe `upload_url`
5. Faz upload para URL assinada (S3/Supabase)
6. Notifica backend sobre conclusão
7. Remove os artefatos locais no sucesso
8. Observação: existe helper de thumbnail no código, mas ele não faz parte do pipeline ativo do worker

**Modo Leve (`GN_LIGHT_MODE=1`):**
1. Continua aplicando watermark local, mas com encode mais leve (`GN_LM_CRF` + `GN_LM_PRESET`)
2. Se `VERTICAL_FORMAT=1`, recorta o clipe para `9:16` sem scale forçado
3. Usa perfil de captura RTSP `compatible` por inferência quando `GN_RTSP_PROFILE` não estiver explícito
4. Registra metadados no backend → recebe `upload_url`
5. Faz upload do arquivo final
6. Notifica backend sobre conclusão
7. Remove os artefatos locais no sucesso

**Modo DEV (`DEV=true`):**
1. Executa processamento local normalmente (watermark no modo normal, ou fluxo leve)
2. Não chama API externa (`register/upload/finalize`)
3. Marca `remote_registration` como `skipped` com motivo `DEV mode`
4. Marca o item local como `dev_local_preserved`
5. Preserva os artefatos locais para inspeção e evita reprocessamento automático

### 6. Tratamento de Erros

- **Retry automático:** Até 3 tentativas com backoff
- **Pasta de falhas:** Vídeos que falharam vão para `failed_clips/upload_failed/`
- **Reprocessamento:** Sistema tenta reprocessar falhas periodicamente
- **Diagnóstico seguro:** O retry registra a resposta do backend no sidecar sanitizando `upload_url`/URLs assinadas antes de persistir metadados locais.
- **Exceção de horário comercial:** Se a API rejeitar o registro com `HTTP 403` por janela de horário (`request_outside_allowed_time_window`), o worker exclui o vídeo e sidecar local imediatamente (sem retry e sem enviar para `failed_clips`)
- **Erros HMAC/device não-retriáveis:** Quando a API retorna erros de autenticação/integridade do device (ex.: `signature_mismatch`, `client_mismatch`, `device_revoked`), o worker remove o registro local (vídeo + sidecar) para evitar loop infinito de retry.
- **Conflito de reupload não-retriável:** Se o backend responder `HTTP 409` indicando transição inválida para reupload, o worker exclui o registro local para não insistir em um estado já bloqueado pelo backend.

### 7. Presença MQTT

Quando `GN_MQTT_ENABLED=1`, o edge sobe um serviço dedicado em paralelo ao pipeline principal:

1. conecta ao broker sem bloquear captura e worker;
2. publica presença retida em `grn/devices/{device_id}/presence`;
3. publica heartbeat periódico em `grn/devices/{device_id}/heartbeat`;
4. publica estado resumido em `grn/devices/{device_id}/state`;
5. registra `last will` para marcar `offline` em queda abrupta;
6. consome `config/desired` e `config/request` para configuração operacional remota segura;
7. publica `config/reported` com resultado de aplicação/rejeição e `config/state` com snapshot da configuração efetiva;
8. mantém `commands/in` e `commands/out` reservados para a fase futura.

Falhas de MQTT não derrubam o loop principal de replay. O edge continua capturando e processando mesmo sem broker disponível.

Observação de tópico:
- `DEVICE_ID`/`GN_DEVICE_ID` usado no namespace MQTT deve ser um único nível de tópico. Valores com `/`, `+`, `#` ou byte nulo são rejeitados ao montar os tópicos para evitar wildcard/hierarquia inesperada; nesse caso a presença MQTT é ignorada sem derrubar captura/worker.
- configuração remota exige `DEVICE_SECRET`/`GN_DEVICE_SECRET` para validar assinatura HMAC; sem esse segredo, mensagens `config/desired` são rejeitadas.

---

## 📁 Estrutura de Diretórios

```
grava_nois_system/
├── main.py                      # Bootstrap, listeners de trigger e fan-out
├── requirements.txt             # Dependências Python
├── optimze_image.py             # Gera versões otimizadas das logos (PNG RGBA)
├── .env                         # Configuração (não commitado)
│
├── src/
│   ├── config/
│   │   └── settings.py          # CaptureConfig e parsing de fontes de câmera
│   ├── video/
│   │   ├── capture.py           # Comando FFmpeg por câmera
│   │   ├── buffer.py            # Buffer circular e indexação de segmentos
│   │   └── processor.py        # Concat highlight, ffprobe, watermark, enqueue
│   ├── workers/
│   │   └── processing_worker.py # Worker de processamento, upload e retry
│   ├── utils/
│   │   ├── logger.py            # Sistema de logging centralizado
│   │   ├── pico.py              # Detecção de porta serial do Pico
│   │   ├── device.py            # Detecção de Raspberry Pi
│   │   └── time_utils.py        # Validação de janela horária
│   ├── security/
│   │   ├── hmac.py              # Hash/HMAC/nonce/timestamp
│   │   └── request_signer.py    # Canonical string + headers HMAC
│   └── services/
│       ├── api_client.py        # Cliente HTTP para backend
│       ├── api_error_policy.py  # Regra de decisão para erros da API
│       ├── retry_upload.py      # Lógica de retry de upload
│       └── mqtt/
│           ├── mqtt_client.py            # Cliente MQTT e lifecycle
│           ├── device_presence_service.py# Presença, heartbeat e estado
│           ├── device_config_service.py  # Configuração remota assinada
│           ├── command_dispatcher.py     # Estrutura futura de command/control
│           ├── command_executor.py       # Placeholder sem execução real
│           └── command_policy.py         # Política que bloqueia comandos na fase 1
│
├── files/
│   ├── replay_grava_nois.png    # Logo principal (original)
│   ├── client_logo.png          # Logo secundária do cliente (original)
│   ├── replay_grava_nois_wm.png # Logo principal otimizada (fallback automático)
│   └── client_logo_wm.png       # Logo secundária otimizada (fallback automático)
│
├── logs/
│   ├── app.log                  # Logs da aplicação (DEBUG)
│   └── ffmpeg_<camera_id>.log   # Logs do FFmpeg por câmera
│
├── recorded_clips/              # Highlights após concat
├── queue_raw/                   # Fila de processamento (isolada por câmera em multi-cam)
├── highlights_wm/               # Vídeos finais com marca d'água
├── failed_clips/                # Vídeos que falharam
│   ├── upload_failed/           # Falhas de upload (retry automático)
│   ├── build_failed/            # Falhas na construção
│   └── enqueue_failed/          # Falhas ao enfileirar
│
├── tests/                       # Testes unitários e de integração
│   ├── test_trigger_fanout.py   # Fan-out, roteamento Pico e cooldown por câmera
│   ├── test_trigger_sources.py  # Parsing de token serial
│   ├── test_multi_camera_settings.py  # CaptureConfig multi-câmera
│   ├── test_security_signing.py # Assinatura HMAC
│   ├── test_api_error_policy.py # Política de erros da API
│   └── ...                      # Demais testes
│
└── docs/
    ├── fluxo-funcional.md       # Diagrama detalhado
    ├── grava_nois_fluxo.png     # Diagrama visual
    └── specs/                   # Especificações técnicas (lookup principal)
        ├── DESIGN_SPEC.md
        └── system/
            ├── ARCHITECTURE.md
            ├── CONFIGURATION.md
            ├── PIPELINE.md
            ├── BUSINESS_RULES.md
            ├── INTEGRATIONS.md
            └── OPERATIONS.md
```

### Diretórios Criados Automaticamente

O sistema cria os seguintes diretórios se não existirem:
- `/dev/shm/grn_buffer/` (ou `GN_BUFFER_DIR`)
- `recorded_clips/`
- `queue_raw/`
- `highlights_wm/`
- `failed_clips/`
- `logs/`

---

## ⚙️ Configuração

### config.json — Configuração operacional gerenciada

O sistema suporta um arquivo `config.json` na raiz do projeto para parâmetros operacionais não sensíveis.

**Política de precedência:**
```
defaults → variáveis de ambiente (fallback legado) → config.json (vence quando presente)
```

Instalações existentes sem `config.json` continuam funcionando via env sem alteração.

Para criar:
```bash
cp config.example.json config.json
# edite os campos desejados
```

Para converter um `.env` legado em `config.json` operacional:
```bash
./env_to_config.sh .env config.json
./env_to_config.sh .env config.json --dry-run
```

Em devices provisionados pelo `grava_nois_config`, use explicitamente os paths do host:
```bash
sudo ./env_to_config.sh /opt/.grn/config/.env /opt/.grn/config/runtime/config.json
```

- Documentação completa: [`docs/specs/system/CONFIGURATION.md`](docs/specs/system/CONFIGURATION.md)
- Override de path: `GN_CONFIG_PATH=/caminho/para/config.json`
- Em Docker provisionado, monte o diretorio runtime de config como volume gravavel e use `GN_CONFIG_PATH=/usr/src/app/runtime_config/config.json`; mantenha o `.env` separado e somente leitura.
- Para gerenciamento admin de `.env`, monte o diretório que contém o `.env` em `/usr/src/app/host_config:rw` e defina `GN_HOST_ENV_PATH=/usr/src/app/host_config/.env`. Se esse arquivo não existir no container, o edge responderá `env.reported` com `status=rejected`.

**Nunca coloque em `config.json`:** senhas, tokens, `DEVICE_SECRET`, URLs RTSP com `user:pass@`. Para câmeras com credenciais use `"rtspUrl": "env:GN_CAM01_RTSP_URL"`.

---

### Variáveis de Ambiente

Parâmetros de segredos, identidade e deploy são configurados exclusivamente via variáveis de ambiente ou arquivo `.env`. Parâmetros operacionais também podem vir de `config.json` (ver acima).

#### Câmera RTSP

```bash
# URL completa da câmera (obrigatório)
GN_RTSP_URL=rtsp://user:pass@192.168.1.100:554/cam/realmonitor?channel=1&subtype=0

# Múltiplas câmeras via CSV (opcional)
# GN_RTSP_URLS=rtsp://user:pass@192.168.1.101:554/stream1,rtsp://user:pass@192.168.1.102:554/stream1

# Múltiplas câmeras via JSON (opcional; tem prioridade sobre GN_RTSP_URLS)
# Cada câmera pode declarar pico_trigger_token para roteamento direto de botão → câmera
# GN_CAMERAS_JSON=[{"id":"cam01","name":"Quadra 1","rtsp_url":"rtsp://...","enabled":true,"pico_trigger_token":"BTN_1"},{"id":"cam02","name":"Quadra 2","rtsp_url":"rtsp://...","enabled":true,"pico_trigger_token":"BTN_2"}]

# Health check (opcional)
GN_RTSP_MAX_RETRIES=10          # Tentativas de conexão (padrão: 10)
GN_RTSP_TIMEOUT=5               # Timeout por tentativa em segundos (padrão: 5)
GN_FFMPEG_STARTUP_CHECK_SEC=1.0 # Tempo para validar boot do FFmpeg (padrão: 1s)

# Configuração de segmentos RTSP
GN_SEG_TIME=1                   # Duração de cada segmento (padrão: 1s)
GN_RTSP_PRE_SEGMENTS=6          # Segmentos antes do clique (padrão: 6)
GN_RTSP_POST_SEGMENTS=3         # Segmentos depois do clique (padrão: 3)

# Encoder RTSP
GN_RTSP_PROFILE=               # vazio=inferido por GN_LIGHT_MODE; "hq" ou "compatible"
GN_RTSP_REENCODE=              # vazio=usa o default do profile; 1/0 força override explicito
GN_RTSP_FPS=25                 # Filtro fps opcional (somente quando houver reencode)
GN_RTSP_GOP=25                 # GOP/keyframe interval para segmentacao estavel no reencode
GN_RTSP_PRESET=veryfast        # Preset x264 (somente quando houver reencode)
GN_RTSP_CRF=23                 # Qualidade x264 (somente quando houver reencode)
GN_RTSP_USE_WALLCLOCK=0        # Opt-in para cameras com timestamps instaveis
GN_RTSP_LOW_LATENCY_INPUT=0    # Experimental: adiciona -fflags nobuffer
GN_RTSP_LOW_DELAY_CODEC_FLAGS=0  # Experimental: adiciona -flags low_delay no reencode
```

Observação:
- `GN_RTSP_PROFILE=hq` prioriza qualidade e usa passthrough (`-c:v copy`) por padrão; ideal para câmeras com timestamps estáveis.
- `GN_RTSP_PROFILE=compatible` prioriza robustez e usa reencode libx264 com `fps_mode=vfr` por padrão; ideal para streams problemáticos.
- Se `GN_RTSP_PROFILE` estiver vazio, o profile é inferido por `GN_LIGHT_MODE`: `0 -> hq`, `1 -> compatible`.
- `GN_RTSP_REENCODE` é override opcional do default do profile, não a chave principal de decisão do modo.
- `GN_RTSP_LOW_LATENCY_INPUT` e `GN_RTSP_LOW_DELAY_CODEC_FLAGS` são tuning experimental e ficam desligados por padrão.
- Ordem de precedência da fonte RTSP: `GN_CAMERAS_JSON` > `GN_RTSP_URLS` > `GN_RTSP_URL`.

#### Backend API

```bash
GN_API_BASE=https://api.gravanois.com
GN_API_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
GN_CLIENT_ID=550e8400-e29b-41d4-a716-446655440000
GN_VENUE_ID=6ba7b810-9dad-11d1-80b4-00c04fd430c8
API_BASE_URL=https://api.gravanois.com           # fallback legado para GN_API_BASE
API_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9... # fallback legado para GN_API_TOKEN
CLIENT_ID=550e8400-e29b-41d4-a716-446655440000   # fallback legado para GN_CLIENT_ID
VENUE_ID=6ba7b810-9dad-11d1-80b4-00c04fd430c8    # fallback legado para GN_VENUE_ID
DEVICE_ID=raspberrypi-001
DEVICE_SECRET=troque_por_um_segredo_forte
GN_DEVICE_ID=raspberrypi-001                     # alias opcional de DEVICE_ID
GN_DEVICE_SECRET=troque_por_um_segredo_forte     # alias opcional de DEVICE_SECRET
GN_HMAC_DRY_RUN=0                                # 1=nao envia request; apenas monta, assina e loga
HMAC_DRY_RUN=0                                   # fallback legado para GN_HMAC_DRY_RUN
```

#### GPIO

```bash
GN_GPIO_PIN=17                  # Pino BCM (padrão: desabilitado)
GN_GPIO_DEBOUNCE_MS=300         # Debounce em ms (padrão: 300)
GN_GPIO_COOLDOWN_SEC=120        # Cooldown entre cliques (padrão: 120s)
```

#### Seleção da fonte de trigger

```bash
# auto (padrão): Raspberry -> GPIO; não Raspberry -> Pico serial
GN_TRIGGER_SOURCE=auto

# Opcional para forçar tipo de plataforma durante teste:
GN_FORCE_RASPBERRY_PI=1         # 1=true, 0=false
```

#### Pico USB Serial

```bash
# Porta do Raspberry Pi Pico (opcional; se vazio o sistema tenta auto-detectar)
GN_PICO_PORT=/dev/serial/by-id/usb-Raspberry_Pi_Pico_XXXXXXXXXXXXXXXX-if00

# Token global enviado pelo firmware do Pico (fan-out para câmeras sem token dedicado)
GN_PICO_TRIGGER_TOKEN=BTN_REPLAY
```

**Roteamento multi-botão (por câmera):** Ao usar `GN_CAMERAS_JSON`, cada câmera pode declarar um `pico_trigger_token` próprio. Quando o Pico envia esse token, apenas a câmera correspondente dispara — as demais não são acionadas.

```bash
GN_CAMERAS_JSON='[
  {"id":"cam_quadra1","name":"Quadra 1","rtsp_url":"rtsp://...","enabled":true,"pico_trigger_token":"BTN_1"},
  {"id":"cam_quadra2","name":"Quadra 2","rtsp_url":"rtsp://...","enabled":true,"pico_trigger_token":"BTN_2"}
]'
GN_PICO_TRIGGER_TOKEN=BTN_REPLAY  # fallback global (câmeras sem token dedicado)
```

Comunicação bidirecional com o Pico:
- **Edge → Pico:** ao abrir a serial, o edge envia `GRN_STARTED` para sinalizar que o runtime está operacional. O envio é repetido até o Pico responder `ACK_GRN_STARTED`; o ACK é a confirmação real de que o Pico recebeu o comando e acendeu o LED.
- **Pico → Edge:** tokens de botão, Docker e trigger são enviados pelo firmware.
- Após `PULL_DOCKER`/`RESTART_DOCKER`, o LED apaga e só reacende quando o novo container reenviar `GRN_STARTED` e receber `ACK_GRN_STARTED`.

Lógica de roteamento ao receber um token pela serial:
1. `ACK_GRN_STARTED` → log info, ignorado (confirmação do handshake)
2. Token de manutenção Docker (`PULL_DOCKER`/`RESTART_DOCKER`) → grava uma solicitação em `runtime_config` para o host executar via systemd
3. Token está no mapa dedicado → dispara só a câmera correspondente
4. Token é o global (`GN_PICO_TRIGGER_TOKEN`) → fan-out para câmeras sem token dedicado
5. Token desconhecido → `warning` no log, listener continua sem interrupção

Tokens de manutenção Docker:

```bash
GN_PICO_DOCKER_ACTIONS_ENABLED=1
GN_PICO_DOCKER_PULL_TOKEN=PULL_DOCKER
GN_PICO_DOCKER_RESTART_TOKEN=RESTART_DOCKER
GN_DOCKER_ACTION_REQUEST_PATH=/usr/src/app/runtime_config/docker-action.request.json
```

O edge **não executa Docker e não monta `/var/run/docker.sock`**. Ele apenas cria o arquivo de intenção acima. O `grava_nois_config` instala `grn-docker-action.path`/`grn-docker-action.service` no host para executar `docker compose pull && docker compose up -d --force-recreate --remove-orphans` ou `docker compose up -d --force-recreate --remove-orphans`. Antes de recriar, o runner do host regenera `config.json` a partir do `.env`, preservando identidade e segredos somente no `.env`. O `RESTART_DOCKER` recria o container para reler `env_file` e aplicar alterações no `.env`; não baixa imagem nova. O diretório `runtime_config` precisa ser volume persistente para não perder `config.json`, `config.pending.json`, `config.state.json`, `config.backup.json` e solicitações de ação.

Observações:
- O sistema tenta detectar automaticamente a porta do Pico nesta ordem:
  1. `/dev/serial/by-id/*` (preferencial)
  2. `/dev/ttyACM*`
  3. `/dev/ttyUSB*`
- Se nenhuma porta for detectada, o listener serial não é iniciado (não há fallback forçado para `/dev/ttyACM0`).
- Se `GN_PICO_PORT` estiver definido, ele só é usado quando o caminho existe no host.
- Instalações sem `pico_trigger_token` nas câmeras continuam funcionando com o token global.

#### Processamento

```bash
GN_LIGHT_MODE=1                 # 0=HQ/default, 1=modo leve para hardware fraco
GN_MAX_ATTEMPTS=3               # Tentativas de processamento (padrão: 3)
GN_TRIGGER_MAX_WORKERS=2        # Vazio=auto (número de câmeras); define paralelismo do trigger
GN_BUFFER_DIR=/dev/shm/grn_buffer  # Diretório de buffer (padrão: /dev/shm)
GN_HQ_CRF=18                    # CRF do encode com watermark no modo normal
GN_HQ_PRESET=medium             # Preset do encode com watermark no modo normal
GN_LM_CRF=26                    # CRF do encode com watermark no modo leve
GN_LM_PRESET=veryfast           # Preset do encode com watermark no modo leve
GN_WM_REL_WIDTH=0.19            # Aumenta/reduz a largura da logo; 0.18 = 18% da largura do vídeo
GN_WM_OPACITY=0.8               # Opacidade da logo (0.0 a 1.0)
GN_WM_MARGIN=24                 # Margem vertical da safe zone
VERTICAL_FORMAT=0               # 1=crop central 9:16 sem upscale forçado
GN_RUN_CAMERA_INTEGRATION=1     # Habilita teste real com camera sem Docker
GN_CAMERA_INTEGRATION_OUTPUT_DIR=./artifacts/camera_watermark_test  # Pasta persistente dos mp4s gerados pelo teste
```

#### Teste real sem Docker

Para gerar um `.mp4` final local usando a(s) camera(s) do `.env`, sem subir o projeto inteiro:

```bash
GN_RUN_CAMERA_INTEGRATION=1 \
GN_WM_REL_WIDTH=0.19 \
GN_CAMERA_INTEGRATION_OUTPUT_DIR=./artifacts/camera_watermark_test \
PYTHONPATH=. python -m unittest tests.test_camera_watermark_integration
```

Saida gerada:
- `./artifacts/camera_watermark_test/highlights_wm/*.mp4`
- `./artifacts/camera_watermark_test/queue_raw/`
- `./artifacts/camera_watermark_test/recorded_clips/`

#### Otimizacao de logos (opcional, recomendado)

Use o script abaixo para gerar PNGs menores em RGBA, reduzindo custo de CPU no worker:

```bash
python3 optimze_image.py
```

Por padrão ele gera:
- `files/replay_grava_nois_wm.png`
- `files/client_logo_wm.png`

O `main.py` prioriza automaticamente esses arquivos `_wm.png` quando presentes.

#### Modo Desenvolvimento

```bash
DEV=true                        # Pula chamadas de rede no ProcessingWorker
DEV_VIDEO_MODE=false            # Envia payload com "dev=true" no register de metadados
```

Com `DEV=true`:
- O processamento local do vídeo continua ativo.
- O worker não faz requisições HTTP para registro, upload e finalização.
- O sidecar é marcado como `dev_local_preserved`.
- Os artefatos locais ficam preservados para inspeção e deixam de ser reprocessados automaticamente.

#### Janela de Funcionamento

```bash
GN_TIME_ZONE=America/Sao_Paulo  # Fuso para validação de horário local
GN_START_TIME=07:00             # Início da janela (HH:MM)
GN_END_TIME=23:30               # Fim da janela (HH:MM)
```

Observações:
- Se `GN_START_TIME`/`GN_END_TIME` estiverem inválidos, o sistema faz fallback para `07:00` e `23:30`.
- Se `GN_TIME_ZONE` estiver inválido, o sistema faz fallback para `America/Sao_Paulo`.
- A comparação usa apenas hora/minuto.

#### Logging

```bash
GN_LOG_DIR=/caminho/custom/logs # Diretório de logs (fallback: <raiz-do-projeto>/logs)
```

Observações:
- `DEVICE_ID` também compõe os tópicos MQTT quando a presença está habilitada; use apenas um identificador simples sem `/`, `+`, `#` ou byte nulo.
- Se `GN_LOG_DIR` não for definido, o sistema cria e usa `logs/` na raiz do projeto (mesma pasta de `main.py`).
- O módulo MQTT usa `logs/mqtt.log` para isolar heartbeat/presença do `app.log`.
- Em falhas `401/403` nas rotas assinadas, o logger registra somente `path`, `timestamp`, `nonce`, `body_sha256` e assinatura truncada.
- `DEVICE_SECRET` nunca é escrito nos logs.
- O fallback padrão não depende de caminho absoluto de container Docker.

## 📡 Presença MQTT

### Objetivo

Fornecer visibilidade operacional de `online/offline`, heartbeat e saúde resumida do edge sem misturar essa responsabilidade com a pipeline de captura. O MQTT inicia **antes** das câmeras, e o listener Pico/LED também sobe antes das tentativas RTSP/FFmpeg. Isso garante status e sinalização local mesmo com falha total de câmera. Câmeras indisponíveis são reportadas como `camera_status=UNAVAILABLE`; o supervisor reinicia o FFmpeg quando o processo cai ou quando o buffer fica sem segmentos recentes de forma persistente. Durante a indisponibilidade, triggers são bloqueados, logs registram o motivo e o evento MQTT é publicado.

### Variáveis principais

- `GN_MQTT_ENABLED`: habilita/desabilita o serviço MQTT
- `GN_MQTT_BROKER_URL` ou `GN_MQTT_HOST` + `GN_MQTT_PORT`: broker MQTT
- `GN_MQTT_USERNAME` e `GN_MQTT_PASSWORD`: credenciais do broker
- `GN_MQTT_CLIENT_ID`: identificador MQTT do cliente; default em `DEVICE_ID`
- `GN_MQTT_KEEPALIVE`: keepalive MQTT
- `GN_MQTT_HEARTBEAT_INTERVAL_SEC`: intervalo do heartbeat
- `GN_MQTT_TOPIC_PREFIX`: prefixo base dos tópicos, default `grn`
- `GN_MQTT_QOS`: QoS padrão de publish/subscribe
- `GN_MQTT_RETAIN_PRESENCE`: mantém `presence` retido no broker
- `GN_MQTT_TLS`: força TLS quando necessário
- `GN_AGENT_VERSION`: versão publicada no payload do edge

Observação:
- O `device_id` dos tópicos vem de `DEVICE_ID`/`GN_DEVICE_ID` no serviço de presença e precisa ser um único nível de tópico MQTT. O sistema rejeita `/`, `+`, `#` e byte nulo para evitar wildcard ou hierarquia inesperada; se o valor for inválido, somente MQTT é ignorado.

### Tópicos da fase 1

- `grn/devices/{device_id}/presence`
- `grn/devices/{device_id}/heartbeat`
- `grn/devices/{device_id}/state`
- `grn/devices/{device_id}/capture/events`
- `grn/devices/{device_id}/events`
- `grn/devices/{device_id}/alerts`
- `grn/devices/{device_id}/config/desired`
- `grn/devices/{device_id}/config/reported`
- `grn/devices/{device_id}/commands/in`
- `grn/devices/{device_id}/commands/out`

### Exemplos por tópico

#### `grn/devices/{device_id}/presence`

Exemplo de tópico:
```text
grn/devices/edge-test-01/presence
```

Exemplo de payload:
```json
{
  "device_id": "edge-test-01",
  "client_id": "client-test",
  "venue_id": "venue-test",
  "status": "online",
  "agent_version": "1.0.0-edge",
  "timestamp": "2026-04-05T19:10:00+00:00",
  "last_seen": "2026-04-05T19:10:00+00:00",
  "queue_size": 0,
  "hostname": "raspberrypi",
  "health": {
    "camera_count": 2,
    "online_cameras": 2,
    "trigger_source": "pico",
    "failed_clips_count": 0,
    "upload_failed_count": 0,
    "disk_free_bytes": 12345678901,
    "disk_total_bytes": 31457280000,
    "storage_status": "OK"
  }
}
```

Exemplo de `offline` limpo:
```json
{
  "device_id": "edge-test-01",
  "client_id": "client-test",
  "venue_id": "venue-test",
  "status": "offline",
  "agent_version": "1.0.0-edge",
  "timestamp": "2026-04-05T19:20:00+00:00",
  "last_seen": "2026-04-05T19:20:00+00:00",
  "queue_size": 0,
  "hostname": "raspberrypi",
  "disconnect_reason": "clean_shutdown",
  "health": {
    "camera_count": 2,
    "online_cameras": 2,
    "trigger_source": "pico"
  }
}
```

#### `grn/devices/{device_id}/heartbeat`

Exemplo de tópico:
```text
grn/devices/edge-test-01/heartbeat
```

Exemplo de payload:
```json
{
  "device_id": "edge-test-01",
  "client_id": "client-test",
  "venue_id": "venue-test",
  "status": "online",
  "agent_version": "1.0.0-edge",
  "timestamp": "2026-04-05T19:10:30+00:00",
  "last_seen": "2026-04-05T19:10:30+00:00",
  "queue_size": 1,
  "hostname": "raspberrypi",
  "health": {
    "camera_count": 2,
    "online_cameras": 2,
    "trigger_source": "pico"
  }
}
```

#### `grn/devices/{device_id}/state`

Exemplo de tópico:
```text
grn/devices/edge-test-01/state
```

Exemplo de payload:
```json
{
  "device_id": "edge-test-01",
  "client_id": "client-test",
  "venue_id": "venue-test",
  "status": "online",
  "agent_version": "1.0.0-edge",
  "timestamp": "2026-04-05T19:10:30+00:00",
  "last_seen": "2026-04-05T19:10:30+00:00",
  "queue_size": 1,
  "health": {
    "camera_count": 2,
    "online_cameras": 2,
    "trigger_source": "pico",
    "gpio_enabled": false,
    "pico_enabled": true,
    "failed_clips_count": 0,
    "upload_failed_count": 1,
    "disk_free_bytes": 12345678901,
    "disk_total_bytes": 31457280000,
    "storage_status": "OK"
  },
  "cameras": [
    {
      "camera_id": "cam01",
      "camera_name": "Quadra 1",
      "source_type": "rtsp",
      "queue_size": 1,
      "capture_busy": false,
      "ffmpeg_alive": true,
      "camera_status": "OK",
      "last_error": "",
      "last_error_at": "",
      "restart_attempts": 0,
      "buffer_status": "FRESH",
      "buffer_fresh": true,
      "segment_age_sec": 0.5,
      "last_segment_at": "2026-04-05T19:10:29+00:00",
      "buffer_segment_count": 12
    },
    {
      "camera_id": "cam02",
      "camera_name": "Quadra 2",
      "source_type": "rtsp",
      "queue_size": 0,
      "capture_busy": false,
      "ffmpeg_alive": false,
      "camera_status": "UNAVAILABLE",
      "last_error": "Câmera RTSP não acessível após tentativas configuradas",
      "last_error_at": "2026-04-05T19:10:25+00:00",
      "restart_attempts": 3,
      "buffer_status": "STALE",
      "buffer_fresh": false,
      "segment_age_sec": 18.2,
      "last_segment_at": "2026-04-05T19:10:07+00:00",
      "buffer_segment_count": 3
    }
  ],
  "runtime": {
    "light_mode": false,
    "dev_mode": true,
    "mqtt_enabled": "1"
  }
}
```

#### `grn/devices/{device_id}/capture/events`

Evento assinado publicado quando um trigger é rejeitado por falta de câmera/buffer válido. O edge não gera clipe fantasma com segmentos antigos.

```json
{
  "type": "capture.trigger_rejected",
  "event_id": "f4b2d9c8-5f0d-4d7a-8b20-3e3f6f9f6d0a",
  "device_id": "edge-test-01",
  "client_id": "client-test",
  "venue_id": "venue-test",
  "camera_id": "cam02",
  "trigger_id": "pico-cam02",
  "trigger_source": "pico",
  "reason": "Buffer sem segmentos novos",
  "severity": "warning",
  "occurred_at": "2026-04-05T19:10:30+00:00",
  "camera_status": "UNAVAILABLE",
  "ffmpeg_alive": true,
  "buffer_status": "STALE",
  "segment_age_sec": 18.2,
  "last_segment_at": "2026-04-05T19:10:07+00:00",
  "agent_version": "1.0.0-edge",
  "signature_version": "hmac-sha256-v1",
  "signature": "<base64>"
}
```

Se MQTT estiver indisponível, o evento é salvo em `runtime_config/capture_event_outbox/` e reenviado quando o heartbeat conseguir reconectar.

#### `grn/devices/{device_id}/events`

Exemplo de tópico:
```text
grn/devices/edge-test-01/events
```

Exemplo de payload futuro:
```json
{
  "device_id": "edge-test-01",
  "event": "clip_enqueued",
  "timestamp": "2026-04-05T19:11:00+00:00",
  "details": {
    "camera_id": "cam01",
    "file_name": "highlight_cam01_20260405-191100Z.mp4"
  }
}
```

Observação:
- tópico reservado para evolução futura; a fase 1 não publica eventos operacionais nele.

#### `grn/devices/{device_id}/alerts`

Exemplo de tópico:
```text
grn/devices/edge-test-01/alerts
```

Exemplo de payload futuro:
```json
{
  "device_id": "edge-test-01",
  "severity": "warning",
  "code": "camera_offline",
  "timestamp": "2026-04-05T19:12:00+00:00",
  "message": "Camera cam02 sem segmentos recentes"
}
```

Observação:
- tópico reservado para evolução futura; a fase 1 não publica alertas dedicados nele.

#### `grn/devices/{device_id}/config/desired`

Exemplo de tópico:
```text
grn/devices/edge-test-01/config/desired
```

Exemplo de mensagem recebida:
```json
{
  "type": "config.desired",
  "device_id": "edge-test-01",
  "client_id": "client-test",
  "venue_id": "venue-test",
  "schema_version": 1,
  "config_version": 12,
  "desired_hash": "sha256:...",
  "correlation_id": "cfg-001",
  "issued_at": "2026-04-07T17:00:00+00:00",
  "expires_at": "2026-04-07T17:05:00+00:00",
  "desired_config": {},
  "signature_version": "hmac-sha256-v1",
  "signature": "base64-hmac"
}
```

Observação:
- `desired_config` deve ser uma configuração operacional completa e não sensível;
- secrets, tokens, credenciais MQTT e RTSP com `user:pass@` são rejeitados;
- o payload é validado contra hash, expiração, versão, tenant/device e assinatura HMAC com `DEVICE_SECRET`.

#### `grn/devices/{device_id}/config/reported`

Exemplo de tópico:
```text
grn/devices/edge-test-01/config/reported
```

Exemplo de resposta publicada:
```json
{
  "type": "config.reported",
  "device_id": "edge-test-01",
  "client_id": "client-test",
  "venue_id": "venue-test",
  "schema_version": 1,
  "config_version": 12,
  "correlation_id": "cfg-001",
  "status": "pending_restart",
  "requires_restart": true,
  "reported_hash": "sha256:...",
  "reported_at": "2026-04-07T17:00:03+00:00",
  "rejection_reason": null,
  "agent_version": "1.0.0-edge",
  "signature_version": "hmac-sha256-v1",
  "signature": "base64-hmac"
}
```

Estados possíveis nesta fase:
- `applied`: promovida para `config.json`;
- `pending_restart`: validada e gravada em `config.pending.json`, aguardando restart/reload controlado;
- `rejected`: rejeitada sem alterar `config.json`.

Observação:
- reports `config.reported` são assinados com HMAC-SHA256 usando `DEVICE_SECRET` para que a API aceite apenas estado reportado pelo device autenticado.

#### `grn/devices/{device_id}/config/request`

Exemplo de tópico:
```text
grn/devices/edge-test-01/config/request
```

Exemplo de mensagem recebida:
```json
{
  "type": "config.request",
  "device_id": "edge-test-01",
  "client_id": "client-test",
  "venue_id": "venue-test",
  "schema_version": 1,
  "request_id": "req-001",
  "requested_at": "2026-04-08T14:00:00+00:00",
  "signature_version": "hmac-sha256-v1",
  "signature": "base64-hmac"
}
```

Observação:
- o edge responde a esse request publicando `config.state`;
- o request só é aceito com assinatura HMAC válida usando `DEVICE_SECRET`.

#### `grn/devices/{device_id}/config/state`

Exemplo de tópico:
```text
grn/devices/edge-test-01/config/state
```

Exemplo de snapshot publicado:
```json
{
  "type": "config.state",
  "device_id": "edge-test-01",
  "client_id": "client-test",
  "venue_id": "venue-test",
  "schema_version": 1,
  "config_version": 12,
  "request_id": "req-001",
  "reported_config": {},
  "reported_hash": "sha256:...",
  "reported_at": "2026-04-08T14:00:01+00:00",
  "has_pending_restart": false,
  "pending_version": null,
  "agent_version": "1.0.0-edge",
  "signature_version": "hmac-sha256-v1",
  "signature": "base64-hmac"
}
```

Observação:
- o edge publica `config.state` no boot e em resposta a `config.request`;
- `pending_version` só é enviado como inteiro quando existe restart pendente real; sem pendência, vai `null`;
- antes de calcular `reported_hash`, o snapshot normaliza `float` inteiros (`1.0 -> 1`) para manter compatibilidade de hash com o backend Node.

#### `grn/devices/{device_id}/commands/in`

Exemplo de tópico:
```text
grn/devices/edge-test-01/commands/in
```

Exemplo de mensagem recebida:
```json
{
  "command": "restart_service",
  "request_id": "cmd-001",
  "issued_by": "admin-user"
}
```

Observação:
- a fase 1 não executa comandos remotos; qualquer mensagem recebida aqui é rejeitada.

#### `grn/devices/{device_id}/commands/out`

Exemplo de tópico:
```text
grn/devices/edge-test-01/commands/out
```

Exemplo de resposta publicada na fase 1:
```json
{
  "device_id": "edge-test-01",
  "command": "restart_service",
  "status": "rejected",
  "reason": "remote commands are not enabled in phase 1",
  "source_topic": "grn/devices/edge-test-01/commands/in"
}
```

### Payload mínimo publicado

- `device_id`
- `client_id`
- `venue_id`
- `status`
- `agent_version`
- `timestamp`
- `last_seen`
- `queue_size`
- `health`
- `health.failed_clips_count`
- `health.upload_failed_count`
- `health.disk_free_bytes`
- `health.disk_total_bytes`
- `health.storage_status`
- `cameras[].camera_status`
- `cameras[].last_error`
- `cameras[].restart_attempts`

### Garantias desta fase

- MQTT é opcional e isolado do fluxo de replay
- MQTT inicia antes das câmeras para permitir status degradado mesmo com hardware indisponível
- `presence` usa retained message e `last will`
- heartbeats não executam comandos
- qualquer comando recebido em `commands/in` é rejeitado explicitamente
- configuração remota usa `config/desired`, `config/request`, `config/reported` e `config/state`, nunca `commands/in`
- `config.json` é atualizado por escrita atômica e mantém `config.backup.json` quando promovido
- `config.json`, `config.pending.json`, `config.state.json` e `config.backup.json` precisam ficar no mesmo diretorio persistente e gravavel quando o edge roda em Docker

### Câmera V4L2 (Local)

Para usar câmera USB local em vez de RTSP, **não defina** `GN_RTSP_URL` e configure:

```bash
GN_INPUT_FRAMERATE=30
GN_VIDEO_SIZE=1280x720
```

O código usará `/dev/video0` automaticamente.

---

## 🎬 Otimização de Captura RTSP

Para câmeras RTSP, especialmente em redes WiFi instáveis, existem várias opções de tuning disponíveis. Consulte o guia completo em [`docs/RTSP_TUNING.md`](docs/RTSP_TUNING.md) para:

- **Configuração de Re-encoding vs Passthrough** (`GN_RTSP_REENCODE`)
- **Timestamps com Wallclock** (`GN_RTSP_USE_WALLCLOCK`) — útil para câmeras com DTS não-monotônicos
- **Qualidade de Compressão** (`GN_RTSP_CRF`, `GN_RTSP_PRESET`)
- **Limitação de Taxa de Frames** (`GN_RTSP_FPS`)
- **Script de Teste Automático** (`./test_wallclock_quality.sh`)

**Início rápido para qualidade máxima:**

```bash
# Evita reencode na captura; watermark final ainda reencoda para aplicar branding.
GN_RTSP_PROFILE=hq \
GN_RTSP_REENCODE=0 \
GN_RTSP_FPS= \
GN_RTSP_USE_WALLCLOCK=0 \
GN_LIGHT_MODE=0 \
GN_HQ_CRF=16 \
GN_HQ_PRESET=slow \
VERTICAL_FORMAT=0 \
python main.py
```

Para esse perfil funcionar bem, configure a própria câmera com FPS fixo e GOP/I-frame interval de 1 segundo.

**Início rápido para câmeras problemáticas:**

```bash
# Tenta wallclock para resolver stutter em câmeras WiFi
GN_RTSP_USE_WALLCLOCK=1 python main.py

# Ou reduz taxa de frames para menos CPU
GN_RTSP_FPS=15 python main.py

# Ou combina ambas
GN_RTSP_USE_WALLCLOCK=1 GN_RTSP_FPS=20 GN_RTSP_PRESET=ultrafast python main.py
```

---

## 📶 Provisionamento WiFi (Hotspot)

Quando o dispositivo chega ao cliente sem credenciais WiFi configuradas, o sistema sobe automaticamente um hotspot temporário para provisionamento local — sem necessidade de internet, aplicativo ou intervenção da equipe Grava Nóis.

### Como funciona

```
Dispositivo liga sem WiFi configurado
       ↓
Serviço grava-provisioning.service detecta ausência de rede
       ↓
Sobe hotspot "GravaNois-XXXX" (aberto, SSID único por MAC)
       ↓
Cliente conecta o celular no hotspot
       ↓
Abre http://192.168.4.1 (captive portal automático)
       ↓
Seleciona a rede WiFi do local e digita a senha
       ↓
Dispositivo testa, salva no Netplan e derruba o hotspot
       ↓
Docker e containers sobem normalmente com WiFi ativo
```

### Guia para o cliente (instalação inicial)

1. Ligue o dispositivo Grava Nóis.
2. Aguarde ~60 segundos até o hotspot aparecer nas redes WiFi do seu celular.
3. Conecte ao hotspot **GravaNois-XXXX** (sem senha).
4. A página de configuração abre automaticamente. Se não abrir, acesse `http://192.168.4.1`.
5. Selecione a rede WiFi do local na lista e informe a senha.
6. Aguarde a confirmação "Conectado!" — o hotspot desaparecerá automaticamente.
7. A partir deste ponto, o dispositivo se conectará automaticamente a essa rede em todo boot.

> **Senha incorreta:** a página exibe erro e o hotspot permanece ativo para nova tentativa.
> **Troca de WiFi futura:** será feita via painel web → Configurações do dispositivo → Alterar WiFi (funcionalidade separada).

### Componentes

Os scripts de provisionamento ficam no repositório `grava_nois_config/provisioning/`
e são instalados em `/opt/.grn/provisioning/` (root:root, 700) durante a preparação
do dispositivo pela equipe Grava Nóis.

| Arquivo | Responsabilidade |
|---------|-----------------|
| `wifi_check.sh` | Detecta se há WiFi ativo no boot |
| `hotspot_up.sh` | Sobe hostapd + dnsmasq |
| `hotspot_down.sh` | Derruba hotspot e reconecta ao WiFi salvo |
| `provisioning_server.py` | Servidor Flask local (porta 80) |
| `netplan_writer.py` | Persiste credenciais no Netplan |
| `templates/provisioning.html` | Página HTML offline (mobile-first) |
| `grava-provisioning.service` | Serviço systemd que orquestra o fluxo no boot |
| `install_provisioning.sh` | Instala dependências e copia scripts para `/opt/.grn/provisioning/` |

### Detalhes técnicos

- **SSID:** `GravaNois-XXXX` — últimos 4 chars do MAC address da interface WiFi
- **IP do dispositivo no hotspot:** `192.168.4.1`
- **DHCP para clientes:** `192.168.4.10` a `192.168.4.50`
- **DNS captive portal:** todo tráfego DNS aponta para `192.168.4.1`
- **Rede:** modo `g` (2.4 GHz, máxima compatibilidade com celulares)
- **Segurança:** hotspot aberto, mas isolado — sem internet exposta; senha do cliente nunca é logada nem enviada à API

### Dependências de sistema

`hostapd`, `dnsmasq`, `python3-flask`, `wireless-tools` — instalados via `grava_nois_config/provisioning/install_provisioning.sh`.

### Instalação

Feita pela equipe via `grava_nois_config`:

```bash
ENABLE_PROVISIONING=1 sudo bash grava_nois_config/.setup_ubuntu_server.sh
```

---

## 🔌 GPIO (Botão Físico)

### Requisitos

- Raspberry Pi com pinos GPIO
- Daemon `pigpiod` em execução
- Biblioteca Python `pigpio` instalada

### Instalação

```bash
sudo apt install -y pigpio
sudo systemctl enable --now pigpiod
```

### Fiação

- **Pino BCM 17** → Um lado do botão
- **GND** → Outro lado do botão
- Pull-up interno habilitado automaticamente

### Configuração

```bash
export GN_GPIO_PIN=17
export GN_GPIO_COOLDOWN_SEC=120  # Ignora cliques por 2min após disparo
python3 main.py
```

### Comportamento

- **Borda detectada:** FALLING (pressionar)
- **Debounce:** 300ms (configurável)
- **Cooldown:** 120s entre disparos válidos (evita cliques acidentais)
- **Fallback:** Se GPIO não disponível, funciona apenas com ENTER

---

## 🔌 Pico via USB Serial (Docker/Linux)

Para ambientes Linux (especialmente Docker), prefira mapear o device por `by-id`:

```yaml
services:
  grava_nois_system:
    devices:
      - /dev/serial/by-id/usb-Raspberry_Pi_Pico_XXXXXXXXXXXXXXXX-if00:/dev/serial/by-id/usb-Raspberry_Pi_Pico_XXXXXXXXXXXXXXXX-if00
```

Exemplo de `.env`:

```bash
GN_PICO_PORT=/dev/serial/by-id/usb-Raspberry_Pi_Pico_XXXXXXXXXXXXXXXX-if00
```

Por que `by-id` é melhor que `/dev/ttyACM0`:
- `ttyACM0` pode variar entre boots/reconexões (`ttyACM1`, `ttyACM2`, ...).
- `by-id` é estável por identificador USB do dispositivo, reduzindo falhas em produção.
- Em container, o mapeamento explícito por `by-id` evita dependência da ordem de enumeração do host.

---

## 🪶 Modo Leve (Light Mode)

**Recomendado para Raspberry Pi 3B ou inferior.**

### Ativação

```bash
export GN_LIGHT_MODE=1
python3 main.py
```

### Diferenças

| Recurso | Modo Normal | Modo Leve |
|---------|-------------|-----------|
| Marca d'água local | ✅ Sim | ✅ Sim |
| Encode do watermark | `GN_HQ_CRF` + `GN_HQ_PRESET` | `GN_LM_CRF` + `GN_LM_PRESET` |
| Perfil RTSP inferido | `hq` | `compatible` |
| Transformação vertical | ✅ Configurável | ✅ Configurável |
| Helper de thumbnail no pipeline ativo | ❌ Não | ❌ Não |
| Cálculo SHA-256 | enqueue + upload | upload |
| Artefato local principal | `highlights_wm/` | `highlights_wm/` |

### Quando Usar

- ✅ Hardware limitado (Pi 3B, 1GB RAM)
- ✅ Múltiplos cliques em sequência
- ✅ Quer priorizar robustez/CPU em vez de qualidade máxima
- ❌ Quer passthrough RTSP e encode final de maior qualidade

---

## 🔧 Troubleshooting

### 1. Câmera RTSP Não Conecta

**Sintomas:**
- "Câmera não acessível após 10 tentativas"
- `camera_status=UNAVAILABLE` no payload MQTT
- triggers para essa câmera são ignorados até o supervisor restabelecer FFmpeg

**Diagnóstico:**

```bash
# Verificar conectividade TCP
nc -zv 192.168.1.100 554

# Testar RTSP diretamente
ffplay rtsp://user:pass@192.168.1.100:554/cam/realmonitor

# Ver logs do FFmpeg por câmera
tail -f logs/ffmpeg_cam01.log

# Ver logs da aplicação
tail -f logs/app.log
```

**Soluções:**

```bash
# 1. Aumentar tentativas/timeout
export GN_RTSP_MAX_RETRIES=20
export GN_RTSP_TIMEOUT=10

# 2. Verificar URL
export GN_RTSP_URL=rtsp://admin:senha@192.168.1.100:554/cam/realmonitor

# 3. Verificar firewall
sudo ufw allow 554/tcp
```

Comportamento esperado: a falha de câmera não derruba o edge. O processo principal permanece vivo, MQTT continua publicando presença/state quando o broker está acessível e o supervisor tenta reiniciar FFmpeg com backoff exponencial.

### 2. GPIO Não Funciona

**Sintomas:**
- "pigpiod não está acessível"
- Botão não dispara

**Soluções:**

```bash
# Iniciar daemon
sudo systemctl start pigpiod
sudo systemctl status pigpiod

# Verificar pino
gpio readall  # Lista todos os pinos

# Testar manualmente
pigs r 17  # Lê estado do pino 17
```

### 3. Disco Cheio

**Sintomas:**
- "No space left on device"
- Sistema travando

**Soluções:**

```bash
# Limpar buffer manualmente
rm -f /dev/shm/grn_buffer/*.ts

# Limpar vídeos processados
rm -f queue_raw/*.mp4
rm -f highlights_wm/*.mp4

# Ajustar buffer máximo
export GN_BUFFER_DIR=/home/pi/buffer  # Usar disco em vez de RAM
```

### 4. Upload Falhando

**Sintomas:**
- Vídeos acumulando em `failed_clips/upload_failed/`
- "Erro de rede ao POST"

**Diagnóstico:**

```bash
# Verificar logs
grep "upload falhou" logs/app.log

# Verificar conectividade
curl -I https://api.gravanois.com

# Testar token
curl -H "Authorization: Bearer $GN_API_TOKEN" https://api.gravanois.com/health
```

**Soluções:**

```bash
# Reprocessamento automático ativado por padrão
# Vídeos em failed_clips/upload_failed/ são retentados a cada 2 minutos

# Forçar reprocessamento manual (mover de volta para fila)
mv failed_clips/upload_failed/*.mp4 queue_raw/
mv failed_clips/upload_failed/*.json queue_raw/
```

### 5. Upload Rejeitado por Horário Comercial (HTTP 403)

**Sintomas:**
- Log com `Upload rejeitado por horário. Arquivo será excluído: ...`
- O arquivo não aparece em `failed_clips/upload_failed/`

**Comportamento esperado:**
- O worker entende essa resposta como rejeição de regra de negócio (não erro transitório).
- O vídeo e o sidecar JSON são removidos localmente.
- Não entra em `max_attempts` e não passa por retry.

### 6. Erros HMAC/Autenticação do Device

Quando o backend retorna erro no formato:

```json
{
  "success": false,
  "data": null,
  "message": "signature_mismatch",
  "error": { "code": "UNAUTHORIZED", "details": null },
  "requestId": "..."
}
```

o device aplica uma política local (em `src/services/api_error_policy.py`) para decidir exclusão imediata ou retry.

**Apaga registro local (vídeo + sidecar):**
- `missing_headers`
- `invalid_timestamp`
- `invalid_nonce`
- `invalid_body_hash`
- `invalid_signature_format`
- `device_not_found`
- `device_revoked`
- `client_mismatch`
- `missing_raw_body`
- `integrity_failed`
- `signature_mismatch`
- `device_not_authenticated`
- `Forbidden - video does not belong to device client`

**Mantém em retry/backoff:**
- `timestamp_out_of_range` (normalmente resolvido com timestamp novo/sincronismo de relógio)
- `replay_detected` (nonce já usado; próximo envio gera nonce novo)
- `replay_store_unavailable`
- `device_hmac_verification_failed`
- outros erros transitórios de rede/infra

### 7. Conflito de Reupload (HTTP 409)

**Sintomas:**
- Log com `Upload bloqueado pelo backend (reupload não permitido).`
- O item é excluído localmente em vez de voltar para retry

**Comportamento esperado:**
- O worker trata `HTTP 409` com mensagem de transição inválida para reupload como erro não-retriável.
- O vídeo e o sidecar JSON são removidos localmente.
- O item não deve voltar para `failed_clips/upload_failed/`.

### 8. CPU/Memória Alta

**Soluções:**

```bash
# 1. Ativar modo leve
export GN_LIGHT_MODE=1

# 2. Reduzir resolução da câmera
# Configurar na câmera para 720p em vez de 1080p

# 3. Usar buffer em disco
export GN_BUFFER_DIR=/home/pi/buffer

# 4. Aumentar intervalo de varredura do worker
# (Editar main.py: scan_interval=3 em vez de 1)
```

### 9. Modo DEV Não Está Isolando Rede

**Sintomas:**
- Mesmo com `.env` configurado, ainda aparecem logs de chamada HTTP.

**Checklist:**

```bash
# O valor deve ser true/1/yes (case-insensitive)
grep '^DEV=' .env

# Exemplo válido
DEV=true
```

**Observação:**
- Em `DEV=true`, o worker deve logar:
  `Modo DEV ativado. Pulando comunicação com a API e upload para a nuvem.`

### 10. Logs Muito Grandes

```bash
# Logs rotativos estão ativados por padrão
# Máximo: 10MB por arquivo, 5 backups = 50MB total

# Limpar manualmente se necessário
rm logs/app.log.*
> logs/app.log
```

---

## 📚 Sistema de Logging

### Níveis de Log

- **Console:** INFO e acima (mensagens importantes)
- **Arquivo `logs/app.log`:** DEBUG e acima (tudo)
- **Arquivo `logs/mqtt.log`:** lifecycle MQTT, heartbeat e presença
- **Arquivo `logs/ffmpeg_<camera_id>.log`:** saída do FFmpeg por câmera (`stdout` + `stderr`)

### Formato

```
[2026-02-13 10:35:05] [INFO    ] [grava_nois:setup_logger:72] Logger configurado
[2026-02-13 10:35:10] [WARNING ] [grava_nois:_process_one:355] API não configurada
[2026-02-13 10:35:15] [ERROR   ] [grava_nois:upload:401] Upload falhou: timeout
```

### Uso no Código

```python
from src.utils.logger import logger

logger.info("Informação geral")
logger.warning("Alerta")
logger.error("Erro")
logger.exception("Erro com stack trace completo")
```

---

## 🌐 Cliente de API

### Uso

```python
from src.services.api_client import GravaNoisAPIClient

# Instanciar (lê configurações do .env)
api_client = GravaNoisAPIClient()

# Verificar se está configurado
if not api_client.is_configured():
    logger.warning("API não configurada")
    return

# Registrar metadados
response = api_client.register_clip_metadados({
    "venue_id": api_client.venue_id,
    "duration_sec": 9.0,
    "captured_at": "2026-02-13T10:00:00Z",
    "meta": {...},
    "sha256": "abc123..."
})

# Upload para URL assinada
status_code, reason, headers = api_client.upload_file_to_signed_url(
    upload_url=response["upload_url"],
    file_path=Path("video.mp4")
)

# Finalizar upload
api_client.finalize_clip_uploaded(
    clip_id=response["clip_id"],
    size_bytes=1234567,
    sha256="abc123...",
    etag=headers.get("etag")
)
```

### Endpoints

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| POST | `/api/videos/metadados/client/{id}/venue/{id}` | Registra metadados |
| PUT | `{upload_url}` | Upload para storage (S3/Supabase) |
| POST | `/api/videos/{clip_id}/uploaded` | Confirma upload |

---

## 🗺️ Roadmap

### Implementado ✅

- ✅ Captura contínua com buffer circular
- ✅ Highlights sob demanda (ENTER/GPIO/Pico serial)
- ✅ Multi-câmera (worker e pipeline isolado por câmera)
- ✅ Pico serial com roteamento multi-botão por câmera (`pico_trigger_token`)
- ✅ Cooldown por câmera (triggers físicos independentes entre câmeras)
- ✅ Worker de processamento com retry
- ✅ Marca d'água local no modo normal
- ✅ Upload via URL assinada
- ✅ Startup de câmera não-fatal com supervisor/retry de FFmpeg
- ✅ MQTT antes das câmeras para status degradado de hardware
- ✅ Pico/LED antes das câmeras para sinalização local mesmo sem RTSP
- ✅ Modo leve para hardware limitado
- ✅ Sistema de logging estruturado
- ✅ Cliente de API centralizado
- ✅ Reprocessamento automático de falhas
- ✅ Assinatura HMAC por device nas rotas protegidas

### Próximos Passos 🚧

- [ ] Logs estruturados em JSON
- [ ] Watchdog com inotify (substituir varredura)
- [ ] Métricas Prometheus/Grafana
- [ ] Dashboard web para monitoramento
- [ ] Compressão de vídeos antigos
- [ ] Upload paralelo (múltiplos vídeos)
- [ ] Detecção de movimento (trigger automático)
- [ ] Suporte a `gpio_pin` por câmera (análogo ao `pico_trigger_token`)

---

## 📖 Documentação Adicional

- **[Specs técnicas](docs/specs/DESIGN_SPEC.md)** — Índice de lookup das specs especializadas
- **[Fluxo Funcional Detalhado](docs/fluxo-funcional.md)** — Diagrama completo do sistema

---

## 📝 Licença

MVP interno do projeto **Grava Nóis**. Uso restrito ao time até formalização de licença.

---

## 🤝 Contribuindo

Para contribuir com o projeto:

1. Leia a documentação completa em `docs/`
2. Teste em ambiente local antes de deploy
3. Siga as convenções de logging estabelecidas
4. Atualize toda documentação impactada no mesmo change (`README.md`, `.env.example`, specs e `AGENTS.md` quando aplicável)

---

## 📞 Suporte

Em caso de problemas:

1. Verifique os logs em `logs/app.log` e `logs/ffmpeg_<camera_id>.log`
2. Consulte a seção [Troubleshooting](#troubleshooting)
3. Entre em contato com a equipe de desenvolvimento

---

**Última atualização:** 2026-03-31
**Versão:** 2.4.0 (multi-botão Pico por câmera + cooldown por câmera)
