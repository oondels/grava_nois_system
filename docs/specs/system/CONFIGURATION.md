# CONFIGURATION.md — Modelo de configuração do grava_nois_system

## Visão geral

O `grava_nois_system` suporta configuração operacional por arquivo persistente (`config.json`), preparando o sistema para futura edição via app (frontend) sem depender de redeploy.

### Política de precedência (parâmetros operacionais)

```
defaults hardcoded
    ↓  (menor prioridade)
variáveis de ambiente (fallback legado — preserva compatibilidade)
    ↓
config.json (vence quando presente — configuração gerenciada)
    ↑  (maior prioridade)
```

Segredos, identidade de device e flags de desenvolvimento **nunca** participam desta cadeia — permanecem exclusivamente em variáveis de ambiente (ver seção abaixo).

---

## O que vai em `config.json`

| Domínio | Campos | Exige restart? |
|---|---|---|
| Captura / segmentação | `capture.segmentSeconds`, `capture.preSegments`, `capture.postSegments` | Sim |
| Tuning RTSP | `capture.rtsp.*` (maxRetries, timeout, reencode, gop, preset, crf, fps, useWallclockTimestamps) | Sim |
| Câmera V4L2 | `capture.v4l2.*` (device, framerate, videoSize) | Sim |
| Estrutura de câmeras | `cameras[]` (id, name, enabled, sourceType, rtspUrl, picoTriggerToken, pre/postSegments) | Sim |
| Fonte de trigger | `triggers.source` (auto/gpio/pico/both) | Sim |
| Concorrência | `triggers.maxWorkers` | Sim |
| Pico serial | `triggers.pico.globalToken` | Sim |
| GPIO | `triggers.gpio.pin` | Sim |
| GPIO cooldown/debounce | `triggers.gpio.cooldownSeconds`, `triggers.gpio.debounceMs` | Futuro: hot-reload |
| Processamento | `processing.lightMode`, `processing.maxAttempts`, `processing.mobileFormat`, `processing.verticalFormat` | Sim (worker) |
| Watermark | `processing.watermark.*` (preset, relativeWidth, opacity, margin) | Futuro: hot-reload |
| Janela operacional | `operationWindow.*` (timeZone, start, end) | Futuro: hot-reload |
| MQTT (não sensível) | `mqtt.enabled`, `mqtt.broker.host`, `mqtt.broker.port`, `mqtt.broker.tls`, `mqtt.keepaliveSeconds`, `mqtt.heartbeatIntervalSeconds`, `mqtt.topicPrefix`, `mqtt.qos`, `mqtt.retainPresence` | Sim |

---

## O que permanece em env/secret

| Variável canônica | Alias legado | Motivo |
|---|---|---|
| `DEVICE_SECRET` | `GN_DEVICE_SECRET` | Segredo HMAC — crítico |
| `GN_API_TOKEN` | `API_TOKEN` | Token de autenticação |
| `GN_MQTT_PASSWORD` | — | Segredo MQTT |
| `GN_MQTT_USERNAME` | — | Credencial MQTT |
| `DEVICE_ID` | `GN_DEVICE_ID` | Identidade de device/provisionamento |
| `GN_CLIENT_ID` | `CLIENT_ID` | Identidade de cliente |
| `GN_VENUE_ID` | `VENUE_ID` | Identidade de venue |
| `GN_API_BASE` | `API_BASE_URL` | Endpoint de backend (infra) |
| `GN_BUFFER_DIR` | — | Path de volume/container |
| `GN_LOG_DIR` | — | Path de logs de container |
| `GN_PICO_PORT` | — | Path de device serial no host |
| `DEV` | — | Flag de desenvolvimento |
| `DEV_VIDEO_MODE` | — | Flag de teste |
| `GN_HMAC_DRY_RUN` | `HMAC_DRY_RUN` | Flag de auditoria/debug |
| `GN_FORCE_RASPBERRY_PI` | — | Override de plataforma (teste) |
| `GN_AGENT_VERSION` | — | Versão de deploy (imagem/build) |
| `GN_RUN_CAMERA_INTEGRATION` | — | Teste de integração manual |
| `GN_CAMERA_INTEGRATION_OUTPUT_DIR` | — | Diretório de artefatos de teste |

---

## Câmeras com credenciais RTSP

URLs RTSP que embutes credenciais (`rtsp://user:pass@host`) **não devem ser gravadas em texto plano** no `config.json`.

Duas opções:

1. **`env:VAR_NAME`** no campo `rtspUrl` da câmera:
   ```json
   { "id": "cam01", "rtspUrl": "env:GN_CAM01_RTSP_URL", ... }
   ```
   O loader resolve `env:GN_CAM01_RTSP_URL` → `os.getenv("GN_CAM01_RTSP_URL")`.

2. **Legado via `GN_CAMERAS_JSON`**: se o array `cameras` em `config.json` estiver vazio ou ausente, o sistema continua lendo `GN_CAMERAS_JSON` / `GN_RTSP_URLS` / `GN_RTSP_URL` do env, preservando total compatibilidade.

---

## Localização e override do arquivo

- **Padrão**: `config.json` na raiz do projeto (mesmo diretório de `main.py`).
- **Override**: defina `GN_CONFIG_PATH=/caminho/para/config.json` no env.

Se o arquivo não existir, o sistema opera com valores de env e defaults — sem erro.

---

## Formato e campos especiais

```json
{
  "version": 1,
  "updatedAt": "2026-04-07T12:00:00Z",
  ...
}
```

- `version`: inteiro >= 1; reservado para migrações futuras de schema.
- `updatedAt`: timestamp ISO-8601 preenchido pelo app ao gravar remotamente; usado para auditoria local.

---

## Validação

Ao carregar `config.json`, o loader valida:

- **Tipos**: booleanos, inteiros, floats, strings no lugar correto.
- **Ranges**: CRF (0–51), GOP (1–300), QoS (0–2), opacidade (0–1), largura relativa (0–1), pino GPIO (0–40 BCM), timeouts, retries, ports.
- **Enums**: `triggers.source` ∈ {auto, gpio, pico, both}; `sourceType` ∈ {rtsp, v4l2}; `preset` ∈ presets x264 válidos.
- **Formato**: `operationWindow.start/end` em HH:MM.

Se houver erros de validação, o `config.json` é **rejeitado completamente** e o sistema cai para env/defaults, logando todos os erros. Isso evita aplicar configuração parcialmente inválida.

---

## Hot-reload vs. restart

A separação entre parâmetros que suportarão hot-reload e os que exigem restart é intencional e documentada no código, embora hot-reload completo não esteja implementado nesta fase.

### Suportarão hot-reload futuro (sem restart do pipeline)
- `operationWindow.*` — fuso, início e fim da janela
- `triggers.gpio.cooldownSeconds` e `debounceMs`
- `mqtt.heartbeatIntervalSeconds`
- `processing.watermark.*`

### Exigem restart/reload controlado
- `cameras` — estrutura de câmeras e source RTSP
- `capture.segmentSeconds` — tamanho do segmento FFmpeg
- `capture.rtsp.*` — parâmetros do stream RTSP
- `triggers.source` — fonte de trigger físico
- `triggers.gpio.pin` — pino BCM
- `processing.lightMode` e `maxAttempts` — comportamento do worker

Para forçar recarga da config em memória (ex: testes), chame `reset_config_cache()` de `src.config.config_loader`.

---

## Migração de instalações existentes

Instalações sem `config.json` continuam funcionando sem alteração — todas as variáveis de ambiente continuam sendo lidas como fallback.

Para migrar:

1. Copie `config.example.json` para `config.json` na raiz do projeto.
2. Ajuste os campos desejados.
3. Para câmeras com credenciais, use `"rtspUrl": "env:GN_CAM01_RTSP_URL"` e mantenha a URL no env.
4. Reinicie o serviço.

Não é necessário remover as variáveis de ambiente existentes — elas continuam válidas como fallback.

---

## Exemplo completo

Veja `config.example.json` na raiz do projeto para um exemplo completo com todos os domínios funcionais.
