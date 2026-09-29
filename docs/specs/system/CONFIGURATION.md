# CONFIGURATION.md — Modelo de configuração do grava_nois_system

Contrato administrativo vigente: [DEVICE_RELIABILITY.md](./DEVICE_RELIABILITY.md). `.env` usa controle v2 integralmente assinado, sem fallback v1; comandos usam IPC durável v2 e ACK aplicativo assinado. Registros históricos de fase 1 não descrevem o dispatcher ativo.


## Contrato diferido v3 (opt-in)

Novos campos: `processing.deferredEnabled=false` (`GN_DEFERRED_PROCESSING_ENABLED`, restart) e `processing.additionalWindows=[]` (`GN_PROCESSING_WINDOWS_JSON`, hot reload). Dias ISO 1–7, múltiplos intervalos HH:MM incluindo meia-noite; madrugada obrigatória não é configurável. Reutiliza `operationWindow.timeZone`. Versões menores são rejeitadas, duplicatas iguais só reportam resultado e conflitos de hash são rejeitados. Journal `config.transaction.json` recupera persistência interrompida. Configuração remota continua snapshot completo com HMAC e correlação.

Detalhes e dependências de liberação: [DEFERRED_PROCESSING.md](DEFERRED_PROCESSING.md).

## Seleção fixed/rental

- `GN_DEVICE_MODE=fixed` (padrão): `GN_CLIENT_ID` e `GN_VENUE_ID` obrigatórios.
- `GN_DEVICE_MODE=rental`: `GN_CLIENT_ID` e `GN_VENUE_ID` devem ficar vazios; `DEVICE_ID` e `DEVICE_SECRET` formam a identidade técnica estável.
- Em configuração remota rental, `client_id` e `venue_id` permanecem presentes nos envelopes com valor JSON `null`.
- Se `GN_API_BASE`/`API_BASE_URL` estiver definido, `DEVICE_ID`/`GN_DEVICE_ID` e `DEVICE_SECRET`/`GN_DEVICE_SECRET` são obrigatórios; `GN_CLIENT_ID` é exigido apenas em `fixed` e deve ficar vazio em `rental`.

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
| Captura / segmentação | `capture.segmentSeconds`, `capture.preSegments`, `capture.postSegments`, `capture.bufferSeconds` | Sim |
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
| `GN_PICO_DOCKER_ACTIONS_ENABLED` | — | Habilita intents host-only de restart/pull via Pico |
| `GN_REMOTE_DEVICE_COMMANDS_ENABLED` | `0` | Aceita comandos administrativos HMAC em commands/in |
| `GN_HOST_ACTION_SECRET_DIR` | `/usr/src/app/host_actions` | Volume tmpfs para segredo transitorio de Wi-Fi |
| `GN_PICO_HOST_SHUTDOWN_ENABLED` | — | Opt-in para poweroff confirmado via Pico; default `0` |
| `GN_PICO_HOST_SHUTDOWN_TOKEN` | — | Token serial de poweroff; default `SHUTDOWN_HOST` |
| `DEV` | — | Flag de desenvolvimento |
| `DEV_USE_CAMERA` | — | Usa câmeras em DEV; padrão `true`, ignorado fora de DEV e exige restart |
| `DEV_VIDEO_MODE` | — | Flag de teste |
| `GN_HMAC_DRY_RUN` | `HMAC_DRY_RUN` | Flag de auditoria/debug |
| `GN_FORCE_RASPBERRY_PI` | — | Override de plataforma (teste) |
| `GN_AGENT_VERSION` | — | Versão de deploy (imagem/build) |
| `GN_RUN_CAMERA_INTEGRATION` | — | Teste de integração manual |
| `GN_CAMERA_INTEGRATION_OUTPUT_DIR` | — | Diretório de artefatos de teste |
| `GN_CLIENT_WATERMARK_ENABLED` | — | Exibe as logos superior e inferior do cliente; padrão `1` e exige restart |

`GN_MQTT_BROKER_URL` aceita apenas `mqtt://` ou `mqtts://`. No `config.json`,
`mqtt.broker.host` contém somente o hostname; protocolo e TLS são representados
separadamente por `mqtt.broker.tls`.

`GN_CLIENT_WATERMARK_ENABLED` não participa de `config.json` nem da configuração remota MQTT. Com `0`, ambas as logos do cliente são omitidas; a watermark Grava Nóis continua obrigatória. A flag ausente equivale a `1` e a mudança vale após reiniciar o edge, para novos processamentos.

Em ambos os formatos, a logo `files/client_logo_wm.png` (alternativa: `client_logo.png`) fica no canto inferior esquerdo. A logo `files/client_logo_top_wm.png` (alternativa: `client_logo_top.png`) fica no canto superior esquerdo e é omitida quando ambos os arquivos estão ausentes. A largura base vem de `processing.watermark.relativeWidth`: a logo superior usa fator 0.70 e a inferior 1.05, preservando a proporção (base de 20% resulta em 14% e 21% da largura final). Ambas seguem `opacity` e `margin`; não há novas variáveis de configuração. Para afastamento de 5 px das quinas, use `processing.watermark.margin=5` (ou `GN_WM_MARGIN=5` quando o JSON não definir essa margem). A marca Grava Nóis permanece centralizada no rodapé horizontal ou no topo vertical dentro da safe zone.

---

## Ownership e identidade operacional

- o `grava_nois_system` continua executando como **um device logico por processo/host provisionado**;
- em `fixed`, `GN_CLIENT_ID` e `GN_VENUE_ID` definem o contexto permanente daquele host;
- em `rental`, cliente e local pertencem à locação temporal e ambos permanecem vazios no host;
- `GN_RENTAL_CLIPS_DIR` define a fila persistente offline (default `/usr/src/app/rental_clips_generated`) e `GN_RENTAL_QUARANTINE_TTL_HOURS` limita itens sem manifesto (default `48`).
- uma mesma venue pode ter varios devices no backend, entao esses dois valores podem se repetir em hosts diferentes;
- `DEVICE_ID` e `DEVICE_SECRET` precisam permanecer exclusivos por host/device.

O configurador pode verificar a compatibilidade da imagem sem iniciar o pipeline:

```bash
python -m src.cli.rental_compat_probe
```

O comando retorna JSON com `compatible`, `probe_schema_version`,
`rental_identity_contract=tenantless-v1` e `agent_version` derivado de
`GN_AGENT_VERSION`. Ele não abre câmera nem conexão MQTT/API.

---

## Câmeras com credenciais RTSP

URLs RTSP que embutes credenciais (`rtsp://user:pass@host`) **não devem ser gravadas em texto plano** no `config.json`.

Duas opções:

1. **`env:VAR_NAME`** no campo `rtspUrl` da câmera:
   ```json
   { "id": "cam01", "rtspUrl": "env:GN_CAM01_RTSP_URL", ... }
   ```
   O loader resolve `env:GN_CAM01_RTSP_URL` → `os.getenv("GN_CAM01_RTSP_URL")`.

2. **Legado via env**: somente quando o campo `cameras` estiver ausente do `config.json`, o sistema lê `GN_CAMERAS_JSON` / `GN_RTSP_URLS` / `GN_RTSP_URL`.

O campo `cameras` é autoritativo quando presente. Um array vazio ou composto apenas
por entradas `enabled=false` desabilita todas as câmeras e nunca aciona fallback.
Com captura habilitada, uma câmera RTSP com `env:VAR_NAME` ausente causa falha explícita no startup;
a mensagem contém apenas câmera e nome da variável, nunca seu valor.

### Execução DEV sem câmera

`DEV=true` com `DEV_USE_CAMERA=false` desativa todas as câmeras antes da resolução
de fontes e credenciais, inclusive as gerenciadas em `config.json`. Não há fallback
RTSP/V4L2, captura FFmpeg, buffers, supervisores ou workers por câmera. MQTT e
listeners de gatilhos permanecem conforme a configuração; a presença reporta zero
câmeras e gatilhos não preservam clipes. O log de startup informa que o serviço está
ativo sem câmeras. Filas e artefatos existentes são preservados; a recuperação
deferred, quando aplicável, continua em modo DEV.

`DEV_USE_CAMERA` permanece exclusivamente no env e exige restart. O padrão é
`true`, mantendo as fontes configuradas e respeitando `enabled=false`/`cameras: []`.
Fora de DEV, a flag é ignorada. Como os demais booleanos de settings, valores
`1`, `true`, `yes`, `y` e `on` habilitam a flag, sem distinção de maiúsculas e com
espaços externos removidos; demais valores, incluindo `0` e `false`, desabilitam.

---

## Localização e override do arquivo

- **Padrão local**: `config.json` na raiz do projeto (mesmo diretório de `main.py`).
- **Override**: defina `GN_CONFIG_PATH=/caminho/para/config.json` no env.
- **Docker provisionado**: monte o diretorio persistente de config como volume gravavel e defina `GN_CONFIG_PATH=/usr/src/app/runtime_config/config.json`. O `.env` deve continuar montado separadamente como somente leitura.
- **Gerenciamento admin de `.env`**: defina `GN_HOST_ENV_PATH` apontando para o `.env` real montado em volume gravável dentro do container. No compose gerenciado, o padrão é `/usr/src/app/host_config/.env`.

Se o arquivo não existir, o sistema opera com valores de env e defaults — sem erro.

Para o painel admin conseguir visualizar/editar `.env`, o path de `GN_HOST_ENV_PATH` precisa existir no container. Em teste local com `docker-compose.yml`, mantenha `.env` na raiz do repo e o volume `.:/usr/src/app/host_config:rw`. Em device provisionado pelo `grava_nois_config`, o setup monta `/opt/.grn/config:/usr/src/app/host_config:rw`.

---

## Gerenciamento remoto de `.env` via admin

O `DeviceEnvService` permite que admins visualizem e editem remotamente o `.env` do host pelo app, usando MQTT e SSE:

- `env/request`: backend solicita snapshot do `.env`;
- `env/desired`: backend envia o novo conteúdo criptografado;
- `env/reported`: edge responde snapshot, aplicação ou rejeição.

O conteúdo nunca trafega em texto claro no broker. API e edge usam envelope AES-256-GCM com chave derivada de `DEVICE_SECRET`/`GN_DEVICE_SECRET`. O edge lê e escreve somente o arquivo apontado por `GN_HOST_ENV_PATH`, cria backup `.env.bak.grn.<timestamp>` antes de aplicar alterações e publica `rejected` quando o arquivo não existe ou a assinatura falha.

Quando o admin salva `.env` com `restart_after_apply=true`, o edge solicita ao runner Docker do host a ação `restart_container` em vez de executar Docker dentro do container. Antes do recreate, o runner executa o conversor persistente e regenera atomicamente `config.json` a partir do `.env`; falha na conversao interrompe a acao. Identidade e segredos permanecem somente no `.env`.

O `config.desired` operacional aceito é convertido para as variáveis equivalentes e gravado atomicamente no `.env` gerenciado antes do report MQTT. A escrita preserva identidade, segredos e campos não gerenciados, cria backup `0600` e faz rollback se a promoção do JSON falhar. Assim, `.env` e JSON permanecem reconciliados durante `restart_container` e `pull_and_recreate`.

Quando `restart_after_apply=true` está presente no envelope HMAC, o edge só agenda `restart_container` depois de persistir o `.env` e publicar o report de sucesso. O campo participa da assinatura; alterá-lo em trânsito invalida o comando.

`applied_requires_restart` confirma a gravação do arquivo, não uma mudança no runtime. O status é conservador: também é emitido se a edição modificar apenas comentários ou outra linha sem efeito operacional. `restartStatus=not_requested|queued|rejected|uncertain` informa somente o encaminhamento do pedido; `queued` não confirma que o runner concluiu o recreate. Um `rejected` ou `uncertain` após a escrita não restaura o `.env` anterior. Reinício manual, pedido admin ou `RESTART_DOCKER` do Pico só ficam comprovados quando o novo processo publica outro `boot_id` em presence/heartbeat MQTT; a aba `.env` não atualiza retroativamente o resultado da escrita.

No notebook executando `main.py` sem runner, pare o processo e inicie-o novamente. Para parâmetros que também existem em `config.json`, regenere antes o JSON no caminho de `GN_CONFIG_PATH` (por exemplo `bash env_to_config.sh .env runtime_config/config.json`); variáveis já exportadas no shell prevalecem sobre `load_dotenv()` sem `override`. No host provisionado, o runner faz a conversão e o recreate automaticamente. Procedimento e verificação: [README de grava_nois_config](https://github.com/oondels/grava_nois_config/blob/main/README.md#aplicar-alteracoes-de-ambiente-e-configuracao-admin).

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
- `capture.bufferSeconds` — capacidade do buffer circular. Se omitido, usa
  `max(40, (preSegments + postSegments + 2) × segmentSeconds)`. O fallback
  legado é `GN_MAX_BUFFER_SECONDS`; overrides menores que a janela necessária
  são rejeitados.
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
- `device_id`, `client_id`, `venue_id` (os dois últimos são `null` no modo rental)
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
- `device_id`, `client_id`, `venue_id` (os dois últimos são `null` no modo rental)
- `schema_version`: `1`
- `config_version`: versão recebida em `config.desired`
- `status`: `applied`, `pending_restart` ou `rejected`
- `requires_restart`
- `reported_hash`: hash aplicado ou pendente, quando houver
- `reported_at`
- `rejection_reason`: motivo sanitizado, quando houver rejeição
- `last_applied_version`: última versão local aplicada em `config.state.json`, quando conhecida
- `pending_version`: versão pendente local, quando houver `config.pending.json`
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
- `last_applied_version` expõe a última versão local aplicada para o backend recuperar divergência de versionamento
- `has_pending_restart=false` implica `pending_version=null`
- `has_pending_restart=true` implica `pending_version>=1`
- antes do hash, o snapshot normaliza `float` inteiros (`1.0`, `300.0`, `120.0`) para `int`, preservando floats reais como `0.8`

Persistência local:

- `config.pending.json`: versão validada aguardando restart/reload controlado;
- `config.backup.json`: cópia da última `config.json` antes de promoção;
- `config.state.json`: metadata local de versão/hash/status;
- `config.json`: só é sobrescrito por escrita atômica após validação completa e quando a mudança não exige restart.

Em Docker, os quatro arquivos acima devem compartilhar o mesmo diretorio persistente e gravavel. Montar apenas `config.json` como arquivo `:ro` quebra a promoção de configurações remotas e impede a persistência correta de pending/state/backup.

O compose local e o compose gerenciado devem apontar `GN_CONFIG_PATH` para `/usr/src/app/runtime_config/config.json` e montar o diretório `runtime_config` como volume gravável. Esse mesmo diretório recebe `device-actions/requests/<uuid>.json` quando Pico/MQTT solicita manutenção ao host. O caminho legado `GN_DOCKER_ACTION_REQUEST_PATH` ancora o IPC v2 em seu diretório pai; solicitações legadas pendentes continuam sendo detectadas e reconciliadas.

Estados reportados:

- `applied`: configuração promovida para `config.json`;
- `pending_restart`: configuração validada e gravada em `config.pending.json`, mas exige restart/reload controlado;
- `rejected`: payload rejeitado por schema, hash, expiração, assinatura, tenant/device divergente ou campo sensível.

O backend continua sendo a fonte de verdade futura para desired/applied config. MQTT é apenas canal de entrega e reporte.

### Sanitização de dados publicados

Antes de publicar `config.state` ou `config.reported` via MQTT, o edge sanitiza URLs RTSP que contenham credenciais inline (`rtsp://user:pass@host`), substituindo por `[CREDENTIALS_IN_ENV]`. Isso protege contra configs legadas que tenham sido configuradas manualmente com credenciais em texto plano no `config.json`.

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

O primeiro argumento posicional é sempre a fonte e o segundo é o destino, inclusive quando escritos literalmente como `.env` e `config.json`. `--dry-run` é aceito antes, entre ou depois deles e não grava arquivos nem backups. Mais de dois paths são erro. Sem paths, usa `.env` e `config.json` locais; somente nesse modo, se o `.env` local estiver ausente, considera o caminho legado `/opt/.grn/config/.env`. Paths explícitos nunca são redirecionados para a instalação legada.

Na execução local, `bash env_to_config.sh .env runtime_config/config.json` deve exibir `.env` em **Fonte** e `runtime_config/config.json` em **Saída**. Se o destino existir, preserva seu conteúdo anterior no backup `.json.bak`; outros arquivos de configuração não são alterados. Configure `GN_CONFIG_PATH` para o mesmo destino antes de iniciar o edge.

Uma webcam precisa estar declarada como câmera gerenciada para sobreviver à conversão:

```dotenv
GN_CAMERAS_JSON=[{"id":"notebook","name":"Webcam notebook","enabled":true,"sourceType":"v4l2"}]
GN_INPUT_FRAMERATE=30
GN_VIDEO_SIZE=640x480
```

O dispositivo V4L2 padrão do conversor é `/dev/video0` (`GN_V4L2_DEVICE` permite escolher outro durante a conversão). Sem fontes configuradas, `cameras: []` desativa captura; o aviso do conversor não promete fallback. `tests.test_env_to_config_cli` valida argumentos, webcam, destino, backup, paths com espaços e dry-run com arquivos sintéticos, sem câmera ou serviços.

Em hosts provisionados pelo `grava_nois_config`, informe os paths explicitamente:

```bash
sudo ./env_to_config.sh /opt/.grn/config/.env /opt/.grn/config/runtime/config.json
```

O script converte apenas parâmetros operacionais não sensíveis. Segredos, identidade,
tokens, credenciais MQTT e URLs RTSP com `user:pass@` permanecem no `.env`; nesses
casos o `config.json` usa referência `env:VAR_NAME`.

Não é necessário remover as variáveis de ambiente existentes. As fontes legadas de
câmera só funcionam como fallback enquanto o campo `cameras` estiver ausente.

---

## Exemplo completo

Veja `config.example.json` na raiz do projeto para um exemplo completo com todos os domínios funcionais.
