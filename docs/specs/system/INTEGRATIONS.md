# Edge Integrations

## Environment and settings

Config principal em [`src/config/settings.py`](../../../src/config/settings.py) e leitura complementar em `main.py`.

Variáveis importantes:

### Capture

- `GN_CAMERAS_JSON`
- `GN_RTSP_URLS`
- `GN_RTSP_URL`
- `GN_SEG_TIME`
- `GN_RTSP_PRE_SEGMENTS`
- `GN_RTSP_POST_SEGMENTS`
- `GN_BUFFER_DIR`

### API

- `GN_API_BASE`
- `GN_API_TOKEN`
- `GN_CLIENT_ID`
- `GN_VENUE_ID`
- `DEVICE_ID`
- `DEVICE_SECRET`
- `GN_HMAC_DRY_RUN`

### Trigger

- `GN_TRIGGER_SOURCE`
- `GN_GPIO_PIN`
- `GN_GPIO_COOLDOWN_SEC`
- `GN_GPIO_DEBOUNCE_MS`
- `GN_PICO_PORT`
- `GN_PICO_TRIGGER_TOKEN`

### Runtime

- `GN_LIGHT_MODE`
- `GN_MAX_ATTEMPTS`
- `DEV`
- `GN_TRIGGER_MAX_WORKERS`

### Time window

- `GN_TIME_ZONE`
- `GN_START_TIME`
- `GN_END_TIME`

## FFmpeg / ffprobe

Dependências obrigatórias:

- `ffmpeg`
- `ffprobe`

Uso:

- segmentação contínua;
- concat/remux;
- watermark;
- thumbnail;
- inspeção de metadados.

## Backend API

Cliente: [`src/services/api_client.py`](../../../src/services/api_client.py)

Chamadas principais:

- registro de metadados
- upload para signed URL
- finalize

Observações:

- o cliente ainda aceita `Authorization: Bearer`, mas as rotas protegidas dependem de HMAC;
- `GN_HMAC_DRY_RUN` permite validar canonical string e headers sem chamar backend.

## Request signing

Implementação em [`src/security/request_signer.py`](../../../src/security/request_signer.py)

Headers assinados:

- `X-Device-Id`
- `X-Client-Id`
- `X-Timestamp`
- `X-Nonce`
- `X-Body-SHA256`
- `X-Signature`

Canonical string:

- `v1:{METHOD}:{PATH}:{timestamp}:{nonce}:{bodySha256}`

## Hardware integrations

### GPIO

- usa `pigpio` quando disponível;
- requer `pigpiod` acessível;
- botão esperado entre pino BCM e GND com pull-up interno.

### Pico serial

- descoberta automática por `/dev/serial/by-id`, `/dev/ttyACM*`, `/dev/ttyUSB*`;
- token textual configurável por `GN_PICO_TRIGGER_TOKEN`.

## Local filesystem

Integrações locais relevantes:

- `/dev/shm/grn_buffer` ou diretório configurado
- `recorded_clips/`
- `queue_raw/`
- `highlights_wm/`
- `failed_clips/`
- `logs/`

O filesystem é parte do contrato operacional do sistema.

## Multi-camera support

`GN_CAMERAS_JSON` e `GN_RTSP_URLS` permitem múltiplas câmeras.

Consequências:

- diretórios podem ser isolados por `camera_id`;
- há um worker por câmera;
- o trigger local faz fan-out para todas as câmeras ativas.
