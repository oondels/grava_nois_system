# Changelog

## 2026-08-13

### Added
- `feat(config)`: configuração operacional MQTT passa a persistir atomicamente também no `.env` gerenciado, preservando segredos e permitindo restart remoto assinado após confirmação.
- `feat(rental)`: fila persistente `rental_clips_generated`, manifesto de agenda assinado e inventário/upload manual via MQTT para clipes gerados offline.
- `feat(rental)`: probe CLI sem efeitos colaterais expõe versão do agente e confirma suporte ao contrato rental tenantless.
- `feat(pico)`: firmware operacional V2 opt-in adiciona LEDs de estado, gestos administrativos, diagnóstico local, manutenção temporária e watchdog visual.
- `feat(pico)`: controlador serial único publica câmera/MQTT/upload, mantém heartbeat e preserva os tokens V1 de restart/pull e botões dedicados.
- `feat(device)`: poweroff confirmado via Pico usa intent host-only e permanece desabilitado por padrão.
- `docs(pico)`: guia canônico separa firmware V1 legado e V2 operacional, incluindo pinagem, gestos, LEDs e protocolo.

### Fixed
- `fix(config)`: backup do `.env` operacional recebe permissão `0600`; falha de persistência restaura `.env` e estado pendente antes de reportar rejeição.
- `fix(config)`: `cameras[]` gerenciado passa a ser autoritativo inclusive vazio, e referências `env:` ausentes falham sem expor credenciais.
- `fix(mqtt)`: URL do broker aceita somente `mqtt://` e `mqtts://`, rejeitando WebSocket e aliases incompatíveis com o transporte TCP/TLS atual.
- `fix(rental)`: rejeições definitivas identificadas pelo `error.code` também saem do retry e geram feedback visual no Pico V2.
- `test(rental)`: fixtures MQTT e de presença passam a representar rental com `client_id=null` e `venue_id=null`.

## 2026-08-12

### Changed
- `fix(config)`: pull e restart regeneram `config.json` a partir do `.env` antes do recreate; falha no conversor cancela a ação.
- `refactor(rental)`: identidade técnica rental deixa cliente e venue vazios, omite `X-Client-Id` nas chamadas HMAC e envia ambos como `null` no MQTT.

### Fixed
- `docs(rental)`: requisitos de startup passam a distinguir `GN_CLIENT_ID` obrigatório em fixed e proibido em rental.

## 2026-08-08

### Added
- `feat(rental)`: modo `GN_DEVICE_MODE=rental` para captura móvel sem `GN_VENUE_ID` e registro pela rota HMAC de locações.

### Changed
- `feat(edge)`: eventos MQTT aceitam venue ausente no modo rental e erros definitivos de período/locação são descartados sem retry.
- `fix(rental)`: inicialização reforça as invariantes fixed/rental e configuração MQTT preserva `venue_id=null` de ponta a ponta.

## Unreleased

### Added
- `feat(edge)`: `GN_CLIENT_WATERMARK_ENABLED` controla a exibição da logo do cliente, mantendo a marca Grava Nóis e compatibilidade com instalações existentes.

### Fixed
- `fix(rental)`: worker e retry normalizam o envelope oficial `{ data: { clip } }` do registro, preservando compatibilidade com o formato legado.
- `fix(edge)`: inicialização com API configurada falha cedo sem client/device/segredo HMAC, e erros definitivos de finalize rental saem do loop de retry.
- `fix(architecture)`: identidade da clean architecture agora representa `fixed`/`rental` com as mesmas invariantes do runtime legado.
- `fix(delivery)`: checkpoints deixam de persistir URL/headers assinados e carregam tamanho, SHA-256 e ETag até o finalize.
- `fix(sidecar)`: sidecars legados com `remote_finalize.status=ok` são reconhecidos como terminais durante a migração.

### Added
- Fixture versionada dos contratos legados usada pelos testes de caracterização da refatoração.
- Serviço `DeviceDiagnosticEventService` para publicar eventos MQTT assinados em `diagnostics/events`, incluindo boot, shutdown limpo, conexão e desconexão do broker.
- `boot_id`, sequência de heartbeat e contadores de reconexão MQTT no payload de presença/state para auditoria remota.
- `config.reported` e `config.state` passam a expor `last_applied_version` e `pending_version` para reconciliação remota de versão.

### Changed
- Cliente MQTT registra motivo/timestamp da última desconexão e notifica listeners internos sem interromper o pipeline de captura.
- Troubleshooting MQTT documenta diagnostico separado de DNS, TCP e handshake TLS; certificado expirado deve ser corrigido no broker sem desabilitar `CERT_REQUIRED` no edge.
- Rejeição de configuração por `config_version antiga ou já aplicada` agora é reportada via MQTT com `correlation_id`, versão local aplicada e snapshot efetivo quando disponível.

## 2026-05-02

### Fixed
- Supervisor de camera passa a reiniciar apenas o FFmpeg da camera quando o buffer fica `STALE`/`MISSING`/`UNKNOWN` de forma persistente, cobrindo o caso de processo vivo sem novos segmentos apos queda/religamento RTSP.
- Healthcheck Docker deixa de depender de `pgrep`/`procps` e passa a verificar o processo principal via Python e `/proc/1/cmdline`, evitando falso `unhealthy` em imagens slim.

## 2026-04-17

### Changed
- `DeviceEnvService`: `restart_after_apply=true` passa a solicitar `restart_container` ao runner Docker do host (`source=admin_env`), permitindo que o host regenere `config.json` antes do recreate.
- `env_to_config.sh` e `.env.example`: `VERTICAL_FORMAT` passa a default `0`, alinhado ao loader e ao `config.example.json`, evitando crop 9:16 acidental em configs geradas.
- `.env.example`: `GN_RTSP_FPS` passa a default vazio para preservar a cadencia original quando nao houver necessidade explicita de filtro `fps`.
- Documentacao de RTSP/qualidade atualizada para diferenciar `hq` passthrough, `compatible` com reencode, watermark final e tradeoffs de CRF/preset.

## 2026-04-16

### Changed
- Documentado que o token `RESTART_DOCKER` deve recriar o container via compose no host, sem pull de imagem, para reler `env_file` e aplicar alterações remotas no `.env`.
- Documentado que o token `PULL_DOCKER` executa pull e recriação por compose, preservando `runtime_config` como volume persistente.

## 2026-04-13

### Added
- Serviço MQTT `DeviceEnvService` para gerenciamento remoto de .env admin-only.
- Topicos MQTT: `env/request`, `env/desired`, `env/reported` com envelope criptografado AES-256-GCM.
- Helper `env_envelope.py` para criptografia/descriptografia de envelope de .env com HKDF-SHA256.
- Backup atomico de .env antes de sobrescrever (`.env.bak.grn.<timestamp>`).
- Escrita atomica com `chmod 600` para proteger .env.
- Suporte a `restart_after_apply` para agendar restart do container apos aplicar .env.
- Auditoria dedicada em `env_audit.log` sem valores sensiveis.
- Testes unitarios completos e testes cruzados de compatibilidade TS/Python.

### Changed
- `requirements.txt`: adicionada dependencia `cryptography==44.0.3` para AES-256-GCM e HKDF.
- `main.py`: inicializa `DeviceEnvService` junto com os demais servicos MQTT.

## 2026-04-11

### Added
- Supervisor de câmera em background com retry/backoff exponencial (5s..300s) e restart automático de FFmpeg.
- Campos `camera_status`, `last_error`, `last_error_at` e `restart_attempts` no snapshot MQTT por câmera.
- Métricas expandidas no payload MQTT: `failed_clips_count`, `upload_failed_count`, `disk_free_bytes`, `disk_total_bytes`, `storage_status`.
- Thread dedicada para processamento de mensagens MQTT (handlers desacoplados do loop Paho).
- Reconexão MQTT explícita com `reconnect_delay_set(min_delay=1, max_delay=120)`.
- Sanitização de credenciais RTSP nos logs de comando FFmpeg (`ffmpeg_*.log`).

### Changed
- **MQTT inicia antes das câmeras**: presença e heartbeat publicam status mesmo com falha total de hardware.
- Configuração remota passa a aplicar payload válido mesmo quando `config_version` é antiga, já aplicada ou menor que a pendente.
- Startup de câmera não-fatal: falha em `start_ffmpeg()` marca câmera como `UNAVAILABLE` sem abortar o processo.
- Healthcheck Docker mede liveness do processo Python em vez de FFmpeg.
- `CameraRuntime.proc` e `.segbuf` agora são opcionais (`None` quando câmera indisponível).
- Heartbeat MQTT protegido com try/except permanente; snapshot provider com fallback seguro.
- Shutdown gracioso verifica `proc` e `segbuf` antes de chamar `terminate()`/`stop()`.

### Fixed
- `env_to_config.sh`: conversor local passa a gerar o contrato atual de `config.json`, removendo campos legados `processing.mobileFormat` e `processing.watermark.preset`.
- `DeviceConfigService`: removido resíduo `processing.mobileFormat` da lista de paths que exigem restart.
- `.dockerignore`: artefatos reais de configuração runtime (`config.json`, pending/state/backup e backup local) deixam de entrar no contexto de build.
- `provisioning_server.py`: corrigida ordem de definição de `_detect_wifi_interface()` (NameError no import).

### Changed
- Documentacao passa a recomendar `GN_CONFIG_PATH=/usr/src/app/runtime_config/config.json` com diretorio de config persistente e gravavel em Docker.

## 2026-04-07

### Added
- Camada central de configuração persistente em `src/config/config_loader.py` com dataclasses tipados para todos os parâmetros operacionais não sensíveis.
- Validação de `config.json` em `src/config/config_schema.py`: tipos, ranges, enums e consistência de campos.
- `config.example.json` na raiz do projeto com todos os domínios funcionais documentados.
- `env_to_config.sh` para converter `.env` legado em `config.json` sem migrar segredos ou identidade de device.
- `DeviceConfigService` para receber `config/desired`, validar assinatura/hash/schema, persistir `config.pending.json` e reportar `config/reported`.
- `docs/specs/system/CONFIGURATION.md` descrevendo modelo de precedência, o que vai em `config.json`, o que fica em env, hot-reload vs. restart e guia de migração.
- Suporte a referência `env:VAR_NAME` no campo `rtspUrl` de câmeras para evitar credenciais RTSP em texto plano no `config.json`.
- Override de path via `GN_CONFIG_PATH` para localização customizada do `config.json`.

### Changed
- `src/config/settings.py`: `load_mqtt_config()` e `load_capture_configs()` passam a consumir o loader central; parâmetros operacionais (host MQTT, pre/post segmentos, tuning RTSP/V4L2) vêm de `config.json` → env → defaults.
- `src/video/capture.py`: parâmetros RTSP (`reencode`, `gop`, `preset`, `crf`, `fps`, `useWallclock`, `maxRetries`, `timeout`, `startupCheckSec`) e V4L2 (`framerate`, `videoSize`) lidos via loader central.
- `src/utils/time_utils.py`: `is_within_business_hours()` lê janela operacional via loader central.
- `src/utils/pico.py`: `resolve_trigger_source()` lê `triggers.source` via loader central.
- `src/workers/processing_worker.py`: `GN_WM_PRESET`, `MOBILE_FORMAT` e `VERTICAL_FORMAT` lidos via loader central.
- `main.py`: `light_mode`, `seg_time`, `max_attempts`, `max_workers`, `wm_*`, `gpio_cooldown_sec`, `debounce_ms`, `pico_trigger_token` e `gpio_pin` lidos via loader central; `DEV` permanece em env.

### Compatibility
- Instalações sem `config.json` continuam operando via env/defaults sem alteração de comportamento.
- Aliases legados (`GPIO_PIN`, `GN_RTSP_URL`, `GN_CAMERAS_JSON`, `GN_RTSP_URLS`) preservados como fallback de leitura.

## 2026-04-06

### Changed
- Sanitizacao de respostas persistidas pelo retry de upload para remover URLs assinadas de upload dos sidecars locais.
- Validacao do `device_id` usado em topicos MQTT para rejeitar separadores de nivel e wildcards sem derrubar captura/worker.
- Contagem de fila no payload MQTT passou a usar iterador sem materializar lista de arquivos.

## 2026-04-05

### Added
- Camada MQTT dedicada em `src/services/mqtt/` com cliente, presença do device e placeholders de command/control.
- Publicação opcional de `presence`, `heartbeat` e `state` para o namespace `grn/devices/{device_id}/...`.
- Novo arquivo de log dedicado `mqtt.log`.
- Novos testes `tests/test_mqtt_settings.py`, `tests/test_device_presence_service.py` e `tests/test_mqtt_commands.py`.

### Changed
- `main.py` passou a integrar o lifecycle do serviço MQTT sem acoplar na pipeline principal de captura/upload.
- `src/config/settings.py` passou a centralizar configuração MQTT e `GN_AGENT_VERSION`.
- `README.md`, specs do edge e `.env.example` foram atualizados para documentar a presença MQTT da fase 1 e o bloqueio explícito de comandos remotos.

## 2026-04-04

### Added
- Variavel `GN_WM_REL_WIDTH` para ajustar o tamanho relativo das logos no watermark sem editar codigo.
- Novo teste `tests/test_camera_watermark_integration.py` para validar captura real, highlight, crop vertical e geracao do mp4 final com watermark em modo `DEV`.

### Changed
- Fluxo vertical consolidado em `1080x1920`, com crop `9:16` antes do branding.
- Testes de mobile/vertical atualizados para refletir a nova saida final do pipeline.
- README e specs do edge atualizados com os novos controles de watermark e o fluxo de integracao real com camera.
- Teste de integracao com camera passou a gravar artefatos em pasta persistente configuravel por `GN_CAMERA_INTEGRATION_OUTPUT_DIR`.

## 2026-03-05

### Added
- Suporte a watermark duplo no worker: logo principal + `client_logo`.
- Novo script `optimze_image.py` para gerar logos otimizadas em PNG RGBA.
- Novos assets otimizados:
  - `files/replay_grava_nois_wm.png`
  - `files/client_logo_wm.png`
- Novo teste `tests/test_dual_watermark_command.py` para validar o comando ffmpeg com 2 logos.

### Changed
- Fluxo voltou a priorizar latencia do trigger: `build_highlight` permanece rapido e sem watermark.
- Watermark segue assíncrono no `ProcessingWorker` com preset mais rapido por padrao (`GN_WM_PRESET=veryfast`).
- Posicionamento das 2 logos ajustado para centro (empilhadas).
- `main.py` passou a priorizar automaticamente arquivos `_wm.png` quando existirem.
- README atualizado com o comportamento atual do watermark e uso do script de otimizacao.
