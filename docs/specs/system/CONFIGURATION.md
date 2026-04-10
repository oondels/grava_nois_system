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
| Tuning RTSP | `capture.rtsp.*` (maxRetries, timeout, profile, reencode, gop, preset, crf, fps, useWallclockTimestamps, lowLatencyInput, lowDelayCodecFlags) | Sim |
| Câmera V4L2 | `capture.v4l2.*` (device, framerate, videoSize) | Sim |
| Estrutura de câmeras | `cameras[]` (id, name, enabled, sourceType, rtspUrl, picoTriggerToken, pre/postSegments) | Sim |
| Fonte de trigger | `triggers.source` (auto/gpio/pico/both) | Sim |
| Concorrência | `triggers.maxWorkers` | Sim |
| Pico serial | `triggers.pico.globalToken` | Sim |
| GPIO | `triggers.gpio.pin` | Sim |
| GPIO cooldown/debounce | `triggers.gpio.cooldownSeconds`, `triggers.gpio.debounceMs` | Futuro: hot-reload |
| Processamento | `processing.lightMode`, `processing.maxAttempts`, `processing.verticalFormat`, `processing.hqCrf`, `processing.hqPreset`, `processing.lmCrf`, `processing.lmPreset` | Sim (worker) |
| Watermark | `processing.watermark.*` (relativeWidth, opacity, margin) | Futuro: hot-reload |
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

## Ownership e identidade operacional

- o `grava_nois_system` continua executando como **um device logico por processo/host provisionado**;
- `GN_CLIENT_ID` e `GN_VENUE_ID` definem o contexto do cliente e da venue daquele host;
- uma mesma venue pode ter varios devices no backend, entao esses dois valores podem se repetir em hosts diferentes;
- `DEVICE_ID` e `DEVICE_SECRET` precisam permanecer exclusivos por host/device.

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
- `processing.lightMode`, `maxAttempts`, `hqCrf`, `hqPreset`, `lmCrf`, `lmPreset` — qualidade e comportamento do worker

Para forçar recarga da config em memória (ex: testes), chame `reset_config_cache()` de `src.config.config_loader`.

---

## Configuração remota via MQTT

O edge possui `DeviceConfigService` em `src/services/mqtt/device_config_service.py` para receber configuração operacional remota de forma separada do `CommandDispatcher`.

Tópicos:

- entrada: `grn/devices/{device_id}/config/desired`
- saída: `grn/devices/{device_id}/config/reported`

Contrato de entrada:

- `type`: `config.desired`
- `device_id`, `client_id`, `venue_id`
- `schema_version`: `1`
- `config_version`: inteiro monotônico
- `desired_hash`: `sha256:<hex>` calculado sobre o JSON canônico do `desired_config` preparado com `version` e `updatedAt`
- `correlation_id`
- `issued_at`, `expires_at`
- `desired_config`: configuração operacional completa e não sensível
- `signature`: HMAC-SHA256 base64 do envelope canônico
- `signature_version`: opcional, padrão `hmac-sha256-v1`

Canonical string da assinatura:

```text
v1:CONFIG_DESIRED:{device_id}:{config_version}:{correlation_id}:{issued_at}:{expires_at}:{desired_hash}
```

Contrato de saída:

- `type`: `config.reported`
- `device_id`, `client_id`, `venue_id`
- `schema_version`: `1`
- `config_version`: versão recebida em `config.desired`
- `status`: `applied`, `pending_restart` ou `rejected`
- `requires_restart`
- `reported_hash`: hash aplicado ou pendente, quando houver
- `reported_at`
- `rejection_reason`: motivo sanitizado, quando houver rejeição
- `agent_version`
- `signature`: HMAC-SHA256 base64 do envelope reportado
- `signature_version`: `hmac-sha256-v1`

Canonical string da assinatura do report:

```text
v1:CONFIG_REPORTED:{device_id}:{config_version}:{correlation_id}:{reported_at}:{status}:{reported_hash}
```

Snapshot de sincronização (`config.state`):

- entrada opcional: `grn/devices/{device_id}/config/request`
- saída: `grn/devices/{device_id}/config/state`
- o edge publica `config.state` no boot e em resposta a `config.request`
- `reported_config` deve refletir a configuração operacional efetiva sanitizada
- `has_pending_restart=false` implica `pending_version=null`
- `has_pending_restart=true` implica `pending_version>=1`
- antes do hash, o snapshot normaliza `float` inteiros (`1.0`, `300.0`, `120.0`) para `int`, preservando floats reais como `0.8`

Persistência local:

- `config.pending.json`: versão validada aguardando restart/reload controlado;
- `config.backup.json`: cópia da última `config.json` antes de promoção;
- `config.state.json`: metadata local de versão/hash/status;
- `config.json`: só é sobrescrito por escrita atômica após validação completa e quando a mudança não exige restart.

Estados reportados:

- `applied`: configuração promovida para `config.json`;
- `pending_restart`: configuração validada e gravada em `config.pending.json`, mas exige restart/reload controlado;
- `rejected`: payload rejeitado por schema, hash, expiração, assinatura, tenant/device divergente, versão antiga ou campo sensível.

O backend continua sendo a fonte de verdade futura para desired/applied config. MQTT é apenas canal de entrega e reporte.

---

## Migração de instalações existentes

Instalações sem `config.json` continuam funcionando sem alteração — todas as variáveis de ambiente continuam sendo lidas como fallback.

Para migrar:

1. Copie `config.example.json` para `config.json` na raiz do projeto.
2. Ajuste os campos desejados.
3. Para câmeras com credenciais, use `"rtspUrl": "env:GN_CAM01_RTSP_URL"` e mantenha a URL no env.
4. Reinicie o serviço.

Alternativa para devices legados com `.env` já preenchido:

```bash
./env_to_config.sh .env config.json --dry-run
./env_to_config.sh .env config.json
```

Em hosts provisionados pelo `grava_nois_config`, informe os paths explicitamente:

```bash
sudo ./env_to_config.sh /opt/.grn/config/.env /opt/.grn/config/config.json
```

O script converte apenas parâmetros operacionais não sensíveis. Segredos, identidade,
tokens, credenciais MQTT e URLs RTSP com `user:pass@` permanecem no `.env`; nesses
casos o `config.json` usa referência `env:VAR_NAME`.

Não é necessário remover as variáveis de ambiente existentes — elas continuam válidas como fallback.

---

## Exemplo completo

Veja `config.example.json` na raiz do projeto para um exemplo completo com todos os domínios funcionais.
