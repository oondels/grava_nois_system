# Edge Architecture

## Overview

`grava_nois_system` roda no edge para capturar segmentos contínuos, montar highlights sob trigger, enfileirar processamento local e integrar com o backend via URL assinada.

Responsabilidades centrais:

- capturar vídeo de RTSP ou V4L2 com FFmpeg;
- manter buffer circular local;
- disparar highlights por ENTER, GPIO ou Pico serial;
- persistir sidecar JSON local;
- processar watermark/thumbnail quando aplicável;
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
2. resolve `GN_LIGHT_MODE`, `GN_MAX_ATTEMPTS` e segment size;
3. cria `CaptureConfig` por câmera;
4. limpa buffer e inicia FFmpeg por câmera;
5. inicia `SegmentBuffer` por câmera;
6. inicia `ProcessingWorker` por câmera;
7. resolve trigger source e listeners;
8. orquestra trigger fan-out até shutdown.

## Main internal modules

### Config

- [`src/config/settings.py`](../../../src/config/settings.py)
- resolve single camera, multi-camera via JSON e RTSP legacy

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
- `src/services/mqtt/command_dispatcher.py`

### Utilities

- `logger.py`
- `pico.py`
- `device.py`
- `time_utils.py`

## Camera runtime model

`CameraRuntime` em `main.py` encapsula:

- `cfg` (`CaptureConfig`, inclui `pico_trigger_token`)
- processo FFmpeg
- `SegmentBuffer`
- `capture_lock` — evita sobreposição de build para a mesma câmera
- `_cooldown_until: float` — timestamp até o qual novos triggers físicos são ignorados para esta câmera (cooldown por câmera)

Consequência:

- cada câmera tem pipeline isolado;
- o trigger global (ENTER/GPIO/token Pico global) faz fan-out para câmeras sem token dedicado;
- câmeras com `pico_trigger_token` só disparam quando o token dedicado é recebido;
- o lock evita sobreposição de build; o cooldown evita cliques acidentais em sequência.

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

- bootstrap em `main.py` após iniciar câmeras e workers;
- `DeviceConfigService` fica separado de `CommandDispatcher` para não transformar config remota em command/control arbitrário;
- falhas do broker não derrubam captura, trigger nem worker;
- payload é derivado de snapshot barato do runtime, sem dependência circular com a fila.

## Operating modes

### Normal mode

- crop `9:16` e escala `1080x1920`;
- watermark com safe zone e tamanho relativo configuravel por `GN_WM_REL_WIDTH`;
- thumbnail;
- register/upload/finalize.

### Light mode

- sem watermark e sem thumbnail;
- transforma para vertical quando `VERTICAL_FORMAT=1`;
- upload direto do highlight transformado.

### DEV mode

- processa localmente;
- não chama API externa;
- limpa `queue_raw`;
- preserva saída local útil para inspeção.

## Architectural constraints

- trigger global pode gerar highlight para múltiplas câmeras simultaneamente (fan-out concorrente);
- câmeras com `pico_trigger_token` dedicado só disparam no token correspondente, não no fan-out global;
- o cooldown por câmera (`_cooldown_until`) é independente entre câmeras;
- a fila é baseada em polling com arquivos `.lock`;
- o edge depende de FFmpeg/ffprobe no ambiente;
- integrações com backend devem respeitar assinatura HMAC nas rotas protegidas;
- o pipeline precisa tolerar conectividade intermitente sem corromper a fila local.
