# Edge Pipeline

## 1. Capture bootstrap

Para cada `CaptureConfig`:

1. limpa buffer antigo;
2. cria diretórios necessários;
3. inicia FFmpeg;
4. inicia `SegmentBuffer`.

Entradas possíveis:

- `GN_CAMERAS_JSON`
- `GN_RTSP_URLS`
- `GN_RTSP_URL`
- fallback para `v4l2` local

## 2. Continuous capture

FFmpeg gera segmentos contínuos de 1s ou `GN_SEG_TIME`.

O `SegmentBuffer`:

- indexa segmentos disponíveis;
- mantém janela deslizante;
- remove arquivos excedentes do buffer.

## 3. Trigger flow

Origens suportadas:

- ENTER
- GPIO
- Pico serial (global ou por câmera)

Resolução de origem:

- `auto`
- `gpio`
- `pico`
- `both`

Antes do build:

1. o trigger pode passar por janela horária local;
2. GPIO/Pico respeitam cooldown por câmera (`_cooldown_until`);
3. o evento é roteado conforme o tipo:
   - **Token Pico dedicado** (`pico_trigger_token` da câmera): dispara apenas a câmera correspondente;
   - **Token Pico global** (`GN_PICO_TRIGGER_TOKEN`) ou ENTER/GPIO: fan-out para câmeras sem token dedicado (fallback: todas, se todas tiverem token);
   - **Token desconhecido**: ignorado com `warning`, listener não interrompe.

## 4. Build highlight

Em `build_highlight()`:

1. espera o pós-buffer;
2. calcula segmentos necessários;
3. lê snapshot do buffer;
4. cria manifesto de concat;
5. concatena para `.ts`;
6. remuxa para `.mp4`;
7. salva em `recorded_clips/`.

Se falhar:

- move saídas parciais para `failed_clips/build_failed`;
- grava `.error.txt`.

## 5. Enqueue

`enqueue_clip()`:

1. calcula tamanho;
2. calcula SHA256, exceto em light mode;
3. extrai metadados com `ffprobe`;
4. cria sidecar JSON;
5. move o vídeo para `queue_raw/`.

Campos típicos do sidecar:

- `type`
- `created_at`
- `file_name`
- `size_bytes`
- `sha256`
- `meta`
- `pre_seconds`
- `post_seconds`
- `seg_time`
- `status`

## 6. Worker main path

`ProcessingWorker._scan_once()`:

1. lista `*.mp4` na fila;
2. garante sidecar JSON;
3. cria lock `.lock` por arquivo;
4. chama `_process_one()`.

## 7. Worker processing path

### Normal mode

1. gera watermark em `highlights_wm/`;
2. atualiza sidecar com `meta_wm` e `wm_path`;
3. registra metadados no backend;
4. recebe `upload_url`;
5. faz `PUT` do arquivo final;
6. chama finalize;
7. remove raw local da fila no sucesso.

### Light mode

1. marca sidecar como `ready_for_upload`;
2. registra no backend;
3. faz upload do arquivo original da fila;
4. chama finalize.

### DEV mode

1. não chama backend;
2. marca `remote_registration` como `skipped`;
3. limpa fila local;
4. preserva artefatos locais úteis.

## 8. Retry and failed paths

Se `_process_one()` falhar:

- incrementa `attempts`;
- aplica política de falha;
- refileira ou move para `failed_clips`.

`_scan_retry_failed()`:

- revisita `failed_clips/upload_failed`;
- respeita `max_attempts`;
- respeita idade mínima e backoff;
- só reprocessa estados elegíveis.

## 9. API interaction points

Chamadas principais:

- `register_clip_metadados`
- `upload_file_to_signed_url`
- `finalize_clip_uploaded`

As rotas protegidas por HMAC são:

- `POST /api/videos/metadados/client/:clientId/venue/:venueId`
- `POST /api/videos/:clipId/uploaded`

## 10. Local invariants agents must preserve

- sidecar e vídeo devem permanecer coerentes;
- locks `.lock` devem ser removidos no `finally`;
- `attempts` deve refletir o estado real de retry;
- `build_highlight()` não deve correr em paralelo para a mesma câmera;
- a fila continua sendo filesystem-based, não DB-based.
