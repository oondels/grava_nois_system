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

### Utilities

- `logger.py`
- `pico.py`
- `device.py`
- `time_utils.py`

## Camera runtime model

`CameraRuntime` em `main.py` encapsula:

- `cfg`
- processo FFmpeg
- `SegmentBuffer`
- `capture_lock`

Consequência:

- cada câmera tem pipeline isolado;
- o trigger é disparado em fan-out concorrente;
- o lock evita sobreposição de build para a mesma câmera.

## Queue and filesystem model

Diretórios principais:

- buffer: `/dev/shm/grn_buffer` ou `GN_BUFFER_DIR`
- `recorded_clips/`
- `queue_raw/`
- `highlights_wm/`
- `failed_clips/`
- `logs/`

O sistema usa o filesystem como fila, lock e trilha de auditoria local.

## Operating modes

### Normal mode

- watermark;
- thumbnail;
- register/upload/finalize.

### Light mode

- sem watermark e sem thumbnail;
- upload direto do raw highlight.

### DEV mode

- processa localmente;
- não chama API externa;
- limpa `queue_raw`;
- preserva saída local útil para inspeção.

## Architectural constraints

- trigger válido pode gerar highlight para múltiplas câmeras simultaneamente;
- a fila é baseada em polling com arquivos `.lock`;
- o edge depende de FFmpeg/ffprobe no ambiente;
- integrações com backend devem respeitar assinatura HMAC nas rotas protegidas;
- o pipeline precisa tolerar conectividade intermitente sem corromper a fila local.
