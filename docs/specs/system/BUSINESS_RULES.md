# Edge Business Rules

## Trigger and time-window rules

- trigger físico/local só gera highlight dentro da janela horária configurada;
- se estiver fora da janela, o build deve ser ignorado localmente;
- `GN_START_TIME`, `GN_END_TIME` e `GN_TIME_ZONE` controlam a janela;
- em modo `auto`, Raspberry usa GPIO e outros hosts usam Pico serial.

## Trigger source resolution

- `GN_TRIGGER_SOURCE` aceita `auto`, `gpio`, `pico`, `both`;
- `gpio` sem GPIO válido tenta fallback para Pico;
- Pico só é habilitado com porta válida;
- a detecção de Pico prioriza `/dev/serial/by-id`.

## Concurrency rules

- trigger global (ENTER, GPIO, token Pico global) faz fan-out para câmeras sem `pico_trigger_token` dedicado;
- se todas as câmeras tiverem token dedicado, o fan-out global continua disparando todas (modo de debug/fallback);
- token Pico dedicado dispara apenas a câmera correspondente, não as demais;
- cada câmera possui `capture_lock` próprio — um highlight novo não sobrepõe outro em construção da mesma câmera;
- cooldown de trigger físico (GPIO/Pico) é por câmera via `_cooldown_until`; câmeras em cooldown são ignoradas individualmente sem bloquear as demais.

## Queue and retry rules

- `queue_raw` é a fila de entrada do worker;
- cada item possui vídeo + sidecar JSON;
- lock por `.lock` evita processamento duplicado;
- `GN_MAX_ATTEMPTS` limita retries;
- reprocessamento considera idade mínima e backoff;
- sidecar sem estado elegível não deve ser reprocessado arbitrariamente.

## File lifecycle rules

- highlights brutos nascem em `recorded_clips/`;
- após enqueue, o raw vai para `queue_raw/`;
- arquivos processados em modo normal geram artefato em `highlights_wm/`;
- em `DEV=true`, o sidecar pode permanecer em `queue_raw/` com status `dev_local_preserved`;
- falhas de build vão para `failed_clips/build_failed`;
- falhas de upload/retry podem ir para `failed_clips/upload_failed`.

## Upload and finalize rules

- o edge não envia binário via backend, usa signed URL;
- finalize deve enviar `size_bytes`, `sha256` e opcionalmente `etag`;
- HMAC é obrigatório nas rotas protegidas;
- `DEVICE_SECRET` nunca deve aparecer em log.

## API error policy rules

Erros que devem levar à exclusão do registro local:

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

Também devem excluir localmente mensagens com snippet:

- `forbidden - video does not belong to device client`

Também devem excluir localmente conflitos de negócio não-retriáveis:

- `HTTP 409` com mensagem de transição inválida para reupload

## Business-hours rejection rule

- se a API rejeitar o registro com `request_outside_allowed_time_window`, o worker não deve fazer retry;
- esse caso não deve alimentar `failed_clips`;
- o registro local deve ser descartado.

## Mode rules

### Light mode

- aplica watermark local com encode mais leve (`lmCrf` + `lmPreset`);
- se `capture.rtsp.profile` não estiver explícito, infere perfil RTSP `compatible`;
- `verticalFormat` continua sendo apenas crop/reframe 9:16;
- preserva upload e finalize.

### DEV mode

- não chama API externa;
- não gera retry remoto;
- marca o item como `dev_local_preserved`;
- preserva artefatos locais para inspeção e bloqueia reprocessamento automático.

## Logging rules

- logs devem truncar assinatura HMAC;
- logs devem evitar segredos, tokens e credenciais;
- sidecars de retry não devem persistir URLs assinadas de upload em claro;
- erros operacionais devem preservar contexto suficiente para auditoria local.

## MQTT presence rules

- MQTT deve poder ser desligado integralmente por configuração;
- indisponibilidade do broker não pode interromper captura, trigger, worker ou retry local;
- falha de câmera, FFmpeg, backend, S3 ou internet não pode impedir publicação de status MQTT quando o broker estiver acessível;
- MQTT deve iniciar antes das câmeras para reportar `UNAVAILABLE`/`ERROR` em vez de deixar o device invisível;
- `presence` deve distinguir `online`, `offline` limpo e `offline` por queda abrupta via `last will`;
- `heartbeat` deve atualizar `last_seen` sem gerar ruído excessivo de log;
- `heartbeat` deve sobreviver a exceções de snapshot e publicar fallback seguro quando necessário;
- `state` deve expor saúde por câmera (`camera_status`, `ffmpeg_alive`, `restart_attempts`, `buffer_status`, `buffer_fresh`, `segment_age_sec`, `last_segment_at`) e métricas de fila/storage;
- trigger para câmera sem FFmpeg vivo, sem `SegmentBuffer` ou com buffer sem segmentos recentes não deve gerar clipe; deve registrar warning e publicar `capture.trigger_rejected` em `grn/devices/{device_id}/capture/events`;
- buffer sem segmentos recentes muda o diagnóstico da câmera para `UNAVAILABLE`, bloqueia clipes fantasmas e, se persistente, faz o supervisor reiniciar apenas o FFmpeg daquela câmera, sem reiniciar o container;
- eventos `capture.trigger_rejected` devem ser assinados com `DEVICE_SECRET`/`GN_DEVICE_SECRET`; se MQTT estiver indisponível, devem ficar em outbox local para reenvio;
- `mqtt.log` deve ser separado do `app.log`;
- credenciais MQTT e `DEVICE_SECRET` nunca podem aparecer em logs;
- `device_id` usado em tópicos MQTT deve rejeitar separadores de nível e wildcards (`/`, `+`, `#`);
- `commands/in` e `commands/out` podem existir, mas nenhum comando remoto pode ser executado na fase 1.

## Remote config rules

- configuração remota usa `config/desired`, `config/request`, `config/reported` e `config/state`, nunca `commands/in`;
- `desired_config` deve ser um objeto completo de configuração operacional não sensível;
- o edge valida `device_id`, `client_id`, `venue_id`, `schema_version`, `config_version`, `desired_hash`, expiração e assinatura HMAC;
- a assinatura usa `DEVICE_SECRET`/`GN_DEVICE_SECRET`; sem esse segredo, o payload é rejeitado;
- o report `config.reported` também é assinado com `DEVICE_SECRET`/`GN_DEVICE_SECRET` antes de ser enviado à API;
- `config.request` válido deve gerar `config.state` assinado com snapshot sanitizado da configuração efetiva;
- o edge publica `config.state` no boot para permitir bootstrap do `reportedConfig` persistido no backend;
- `pending_version` no snapshot só pode ser inteiro quando houver pendência real; sem pendência, deve ser `null`;
- o cálculo de `reported_hash` do snapshot deve normalizar `float` inteiros para manter compatibilidade de hash com o backend;
- secrets, credenciais MQTT, tokens, `DEVICE_SECRET` e RTSP com `user:pass@` são rejeitados;
- campos que exigem restart são gravados em `config.pending.json` e reportados como `pending_restart`;
- mudanças em domínios hot-reload-safe podem ser promovidas atomicamente para `config.json`;
- rejeição nunca sobrescreve `config.json` nem apaga a configuração aplicada atual.
