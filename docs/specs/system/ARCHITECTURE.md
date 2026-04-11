# Edge Architecture

## Overview

`grava_nois_system` roda no edge para capturar segmentos contínuos, montar highlights sob trigger, enfileirar processamento local e integrar com o backend via URL assinada.

Responsabilidades centrais:

- capturar vídeo de RTSP ou V4L2 com FFmpeg;
- manter buffer circular local;
- disparar highlights por ENTER, GPIO ou Pico serial;
- persistir sidecar JSON local;
- processar watermark (sempre) e reframe vertical opcional;
- registrar/upload/finalize com o backend;
- reprocessar falhas locais conforme política.

Fora do escopo:

- autenticação de usuários web;
- billing, contratos e RBAC do backend;
- emissão de URL assinada;
- frontend ou painel administrativo.

## Runtime structure

Bootstrap em [`main.py`](../../../main.py):

1. carrega `.env`;
2. resolve configuração operacional efetiva (`config.json` -> env legado -> defaults);
3. cria `CaptureConfig` por câmera;
4. **inicia MQTT (presence, dispatcher, config service) antes das câmeras**;
5. tenta iniciar FFmpeg por câmera (não-fatal: falha marca `camera_status=UNAVAILABLE`);
6. inicia supervisor por câmera em background (retry com backoff exponencial);
7. inicia `ProcessingWorker` por câmera;
8. resolve trigger source e listeners;
9. orquestra trigger fan-out até shutdown.

### Resiliência de startup

- Falha de câmera não aborta o processo; MQTT publica estado degradado.
- Supervisor monitora `proc.poll()` e reinicia FFmpeg com backoff (5s..300s).
- Heartbeat MQTT protegido contra exceções no snapshot provider.
- Trigger fan-out verifica disponibilidade da câmera antes de disparar.

## Main internal modules

### Config

- [`src/config/config_loader.py`](../../../src/config/config_loader.py)
- [`src/config/settings.py`](../../../src/config/settings.py)
- resolve configuração operacional, single camera, multi-camera via JSON e RTSP legacy

### Video

- `capture.py`: comando FFmpeg
- `buffer.py`: buffer circular e indexação
- `processor.py`: concat highlight, ffprobe, watermark e enqueue

### Workers

- [`src/workers/processing_worker.py`](../../../src/workers/processing_worker.py)
- consome fila por filesystem e integra com backend

### Security

- [`src/security/hmac.py`](../../../src/security/hmac.py)
- [`src/security/request_signer.py`](../../../src/security/request_signer.py)

### Service integration

- [`src/services/api_client.py`](../../../src/services/api_client.py)
- [`src/services/api_error_policy.py`](../../../src/services/api_error_policy.py)
- [`src/services/retry_upload.py`](../../../src/services/retry_upload.py)
- `src/services/mqtt/mqtt_client.py`
- `src/services/mqtt/device_presence_service.py`
- `src/services/mqtt/device_config_service.py`
- `src/services/mqtt/command_dispatcher.py`

### Utilities

- `logger.py`
- `pico.py`
- `device.py`
- `time_utils.py`

## Camera runtime model

`CameraRuntime` em `main.py` encapsula:

- `cfg` (`CaptureConfig`, inclui `pico_trigger_token`)
- processo FFmpeg opcional (`None` quando a câmera está indisponível)
- `SegmentBuffer` opcional (`None` enquanto FFmpeg não está rodando)
- `capture_lock` — evita sobreposição de build para a mesma câmera
- `_cooldown_until: float` — timestamp até o qual novos triggers físicos são ignorados para esta câmera (cooldown por câmera)
- `camera_status` — `STARTING`, `OK`, `UNAVAILABLE` ou `ERROR`
- `last_error`, `last_error_at` e `restart_attempts` para diagnóstico remoto via MQTT

Consequência:

- cada câmera tem pipeline isolado;
- o trigger global (ENTER/GPIO/token Pico global) faz fan-out para câmeras sem token dedicado;
- câmeras com `pico_trigger_token` só disparam quando o token dedicado é recebido;
- o lock evita sobreposição de build; o cooldown evita cliques acidentais em sequência.
- câmera indisponível não aborta o edge; o trigger é ignorado para ela e o supervisor continua tentando restabelecer FFmpeg.

## Queue and filesystem model

Diretórios principais:

- buffer: `/dev/shm/grn_buffer` ou `GN_BUFFER_DIR`
- `recorded_clips/`
- `queue_raw/`
- `highlights_wm/`
- `failed_clips/`
- `logs/`

O sistema usa o filesystem como fila, lock e trilha de auditoria local.

## MQTT presence layer

Camada opcional e isolada do pipeline principal:

- cliente MQTT com reconexão controlada e `last will`;
- publicação de `presence`, `heartbeat` e `state`;
- assinatura de `config/desired` em serviço dedicado para configuração operacional remota segura;
- publicação de `config/reported` com resultado `applied`, `pending_restart` ou `rejected`;
- logger dedicado em `mqtt.log`;
- estrutura de `commands/in` e `commands/out` preparada para a fase futura;
- política explícita que bloqueia execução remota na fase 1.

Ponto de integração:

- bootstrap em `main.py` antes de iniciar FFmpeg, para publicar estado mesmo quando a câmera falha;
- `DeviceConfigService` fica separado de `CommandDispatcher` para não transformar config remota em command/control arbitrário;
- falhas do broker não derrubam captura, trigger nem worker;
- payload é derivado de snapshot barato do runtime, sem dependência circular com a fila.

## Operating modes

### Normal mode (light_mode=false)

- aplica watermark sempre, com encode de alta qualidade (`hqCrf` + `hqPreset`, padrão CRF 18 + medium);
- crop 9:16 quando `VERTICAL_FORMAT=1` (reframe sem scale forçado);
- register/upload/finalize.

### Light mode (light_mode=true)

- aplica watermark sempre, com encode leve para hardware fraco (`lmCrf` + `lmPreset`, padrão CRF 26 + veryfast);
- crop 9:16 quando `VERTICAL_FORMAT=1` (mesmo reframe do modo normal);
- register/upload/finalize (idêntico ao modo normal após o encode).

### DEV mode

- processa localmente;
- não chama API externa;
- marca o item como `dev_local_preserved`;
- preserva artefatos locais úteis para inspeção sem reprocessamento automático.

## Architectural constraints

- trigger global pode gerar highlight para múltiplas câmeras simultaneamente (fan-out concorrente);
- câmeras com `pico_trigger_token` dedicado só disparam no token correspondente, não no fan-out global;
- o cooldown por câmera (`_cooldown_until`) é independente entre câmeras;
- a fila é baseada em polling com arquivos `.lock`;
- o edge depende de FFmpeg/ffprobe no ambiente;
- integrações com backend devem respeitar assinatura HMAC nas rotas protegidas;
- o pipeline precisa tolerar conectividade intermitente sem corromper a fila local.
