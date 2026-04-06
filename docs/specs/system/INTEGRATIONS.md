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
- `GN_AGENT_VERSION`

### MQTT

- `GN_MQTT_ENABLED`
- `GN_MQTT_BROKER_URL`
- `GN_MQTT_HOST`
- `GN_MQTT_PORT`
- `GN_MQTT_USERNAME`
- `GN_MQTT_PASSWORD`
- `GN_MQTT_CLIENT_ID`
- `GN_MQTT_KEEPALIVE`
- `GN_MQTT_HEARTBEAT_INTERVAL_SEC`
- `GN_MQTT_TOPIC_PREFIX`
- `GN_MQTT_QOS`
- `GN_MQTT_RETAIN_PRESENCE`
- `GN_MQTT_TLS`

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

## MQTT broker

Cliente: `src/services/mqtt/mqtt_client.py`

Publicações da fase 1:

- `grn/devices/{device_id}/presence` (retained)
- `grn/devices/{device_id}/heartbeat`
- `grn/devices/{device_id}/state`

Tópicos reservados para evolução futura:

- `grn/devices/{device_id}/events`
- `grn/devices/{device_id}/alerts`
- `grn/devices/{device_id}/commands/in`
- `grn/devices/{device_id}/commands/out`

Observações:

- `last will` marca `offline` quando a conexão cai abruptamente;
- o edge continua operando sem broker;
- a fase 1 não executa comandos remotos mesmo que receba mensagens em `commands/in`.

Exemplos rápidos por tópico:

- `presence`
  - tópico: `grn/devices/edge-test-01/presence`
  - payload típico: `status=online|offline`, `last_seen`, `queue_size`, `health`
- `heartbeat`
  - tópico: `grn/devices/edge-test-01/heartbeat`
  - payload típico: mesmo envelope base de presença com `status=online`
- `state`
  - tópico: `grn/devices/edge-test-01/state`
  - payload típico: envelope expandido com `cameras[]` e `runtime`
- `events`
  - tópico reservado para eventos operacionais futuros; fase 1 não publica nele
- `alerts`
  - tópico reservado para alertas futuros; fase 1 não publica nele
- `commands/in`
  - aceita mensagens de comando para evolução futura, mas a fase 1 não executa nada
- `commands/out`
  - publica resposta de rejeição: `status=rejected`, `reason=remote commands are not enabled in phase 1`

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
- token global configurável por `GN_PICO_TRIGGER_TOKEN` (fan-out para câmeras sem token dedicado);
- cada câmera em `GN_CAMERAS_JSON` pode declarar `pico_trigger_token` próprio — quando recebido, dispara apenas aquela câmera sem acionar as demais;
- token desconhecido é logado como `warning` e ignorado; o listener não é interrompido.

## WiFi Provisioning (hotspot local)

Scripts em `grava_nois_config/provisioning/`, instalados em `/opt/.grn/provisioning/` (root:root 700) durante preparação do device.
Dependências de sistema instaladas via `grava_nois_config/provisioning/install_provisioning.sh`:

| Pacote | Uso |
|--------|-----|
| `hostapd` | Criação do ponto de acesso WiFi (AP mode) |
| `dnsmasq` | DHCP para clientes do hotspot + DNS captive portal |
| `python3-flask` | Servidor web local de provisionamento (porta 80) |
| `wireless-tools` | `iwlist` para scan de redes WiFi disponíveis |
| `netplan.io` | Já presente no Ubuntu Server; usado para persistir credenciais |

Interfaces de integração:

- **`hostapd`**: configuração gerada dinamicamente em `/tmp/hostapd.conf`; SSID derivado do MAC da interface WiFi detectada (`iw dev`).
- **`dnsmasq`**: configuração gerada em `/tmp/dnsmasq-hotspot.conf`; DHCP `192.168.4.10–50`, DNS aponta tudo para `192.168.4.1`.
- **Flask** (`provisioning/provisioning_server.py`): endpoints `GET /`, `GET /scan`, `POST /configure`, `GET /status`; roda somente durante o modo hotspot.
- **Netplan** (`provisioning/netplan_writer.py`): lê, faz backup e reescreve `/etc/netplan/50-cloud-init.yaml`; executa `sudo netplan apply`; nunca loga a senha em texto claro.
- **sudoers** (`/etc/sudoers.d/gravanois-provisioning`): permite execução sem senha de `netplan apply`, `hostapd`, `dnsmasq` e `ip` pelo usuário do sistema.
- **systemd** (`systemd/grava-provisioning.service`): orquestra `wifi_check.sh → hotspot_up.sh → provisioning_server.py`; configurado para rodar **antes** do `docker.service`.

PIDs de `hostapd` e `dnsmasq` são salvos em `/tmp/hotspot.pid` para derrubada controlada via `hotspot_down.sh`.

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
- trigger global faz fan-out para câmeras sem `pico_trigger_token` dedicado;
- `pico_trigger_token` por câmera em `GN_CAMERAS_JSON` habilita roteamento direto de botão → câmera.

Exemplo com token dedicado por câmera:

```json
[
  {
    "id": "cam_quadra1",
    "name": "Quadra 1",
    "rtsp_url": "rtsp://user:pass@192.168.1.101:554/stream",
    "enabled": true,
    "pico_trigger_token": "BTN_Q1"
  },
  {
    "id": "cam_quadra2",
    "name": "Quadra 2",
    "rtsp_url": "rtsp://user:pass@192.168.1.102:554/stream",
    "enabled": true,
    "pico_trigger_token": "BTN_Q2"
  }
]
```

Câmeras sem `pico_trigger_token` participam do fan-out global (`GN_PICO_TRIGGER_TOKEN`, ENTER, GPIO).
