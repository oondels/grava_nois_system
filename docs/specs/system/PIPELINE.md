# Edge Pipeline

## 1. Capture bootstrap

O bootstrap de captura é resiliente e não deve bloquear MQTT/presença nem o handshake Pico/LED. Para cada `CaptureConfig`:

1. limpa buffer antigo;
2. cria diretórios necessários;
3. cria `CameraRuntime` com `camera_status=STARTING`, sem abrir RTSP/FFmpeg no thread principal;
4. inicia listeners de trigger, incluindo Pico serial e handshake `GRN_STARTED` -> `ACK_GRN_STARTED`;
5. inicia um supervisor por câmera em background;
6. o supervisor faz a primeira tentativa de FFmpeg, cria `SegmentBuffer` e marca `camera_status=OK`;
7. se FFmpeg falhar, marca `camera_status=UNAVAILABLE`, mantém o edge vivo e tenta novamente com backoff.

Entradas possíveis:

- `GN_CAMERAS_JSON`
- `GN_RTSP_URLS`
- `GN_RTSP_URL`
- fallback para `v4l2` local

## 2. Continuous capture

FFmpeg gera segmentos contínuos de 1s ou `GN_SEG_TIME`.

Perfis de captura RTSP (`capture.rtsp.profile`):

- `hq` (padrão quando `lightMode=false`): passthrough `-c:v copy`, preserva qualidade original da câmera. Adequado para câmeras com timestamps estáveis.
- `compatible` (padrão quando `lightMode=true`): reencode libx264 com `fps_mode=vfr` e `force_key_frames`. Tolerante a DTS/PTS ruins e perda de pacotes.
- Profile explícito (`hq`/`compatible`) sempre tem precedência sobre inferência por `lightMode`.
- `capture.rtsp.reencode` (null/true/false): override explícito do reencode, independente do profile.

O `SegmentBuffer`:

- indexa segmentos disponíveis;
- mantém janela deslizante;
- remove arquivos excedentes do buffer.

Quando a câmera está indisponível (`proc=None`, processo encerrado ou `segbuf=None`) ou quando o último segmento está velho demais, triggers para aquela câmera são ignorados com warning. Isso evita build com segmentos antigos e mantém os demais módulos operando. A queda real do processo FFmpeg continua sendo tratada pelo supervisor; buffer stale por si só não reinicia o container nem força restart do sistema.

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
3. o runtime da câmera é validado: FFmpeg precisa estar vivo e `SegmentBuffer.diagnostics()` precisa retornar `buffer_fresh=true`;
4. se a câmera não estiver pronta, o edge publica `capture.trigger_rejected` em `grn/devices/{device_id}/capture/events` e não chama `build_highlight()`;
5. o evento é roteado conforme o tipo:
   - **ACK Pico** (`ACK_GRN_STARTED`): confirmação do handshake iniciado pelo edge; interrompe novos reenvios de `GRN_STARTED` e é ignorado no fluxo de câmera/Docker;
   - **Token Pico dedicado** (`pico_trigger_token` da câmera): dispara apenas a câmera correspondente;
   - **Token Pico global** (`GN_PICO_TRIGGER_TOKEN`) ou ENTER/GPIO: fan-out para câmeras sem token dedicado (fallback: todas, se todas tiverem token);
   - **Token desconhecido**: ignorado com `warning`, listener não interrompe.

## 4. Build highlight

Em `build_highlight()`:

1. espera o pós-buffer;
2. calcula segmentos necessários;
3. lê snapshot do buffer;
4. cria manifesto de concat;
5. concatena diretamente para `.mp4` temporário com `ffmpeg -f concat -c copy`;
6. promove o `.tmp.mp4` para o arquivo final;
7. salva em `recorded_clips/` com nome `highlight_{camera_id}_{timestamp}.mp4`.

Se falhar:

- move saídas parciais para `failed_clips/build_failed`;
- grava `.error.txt`.

## 5. Enqueue

`enqueue_clip()`:

1. calcula tamanho;
2. calcula SHA256 (omitido em light mode para reduzir CPU);
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
- `pre_segments`
- `post_segments`
- `seg_time`
- `status`

## 6. Worker main path

`ProcessingWorker._scan_once()`:

1. lista `*.mp4` na fila;
2. garante sidecar JSON;
3. cria lock `.lock` por arquivo;
4. chama `_process_one()`.

## 7. Worker processing path

### Normal mode (light_mode=false)

1. aplica watermark com `hqCrf` + `hqPreset` (alta qualidade);
2. aplica crop 9:16 quando `VERTICAL_FORMAT=1` (reframe sem scale forçado);
3. salva resultado em `highlights_wm/`;
4. atualiza sidecar com `meta_wm`, `wm_path` e `wm_encode`;
5. registra metadados no backend;
6. recebe `upload_url`;
7. faz `PUT` do arquivo final;
8. chama finalize;
9. remove artefatos locais no sucesso.

### Light mode (light_mode=true)

Modo para hardware fraco — watermark é sempre aplicada, mas com encode mais leve:

1. aplica watermark com `lmCrf` + `lmPreset` (menor custo de CPU);
2. aplica crop 9:16 quando `VERTICAL_FORMAT=1` (reframe sem scale forçado);
3. salva resultado em `highlights_wm/`;
4. atualiza sidecar com `meta_wm`, `wm_path` e `wm_encode`;
5. registra no backend, faz upload e chama finalize (igual ao modo normal).

### DEV mode

1. não chama backend;
2. marca `remote_registration` como `skipped`;
3. marca o sidecar como `dev_local_preserved`;
4. preserva artefatos locais úteis para inspeção;
5. interrompe antes da limpeza final de sucesso.

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
- `build_highlight()` só deve ser chamado para câmera com FFmpeg vivo e `SegmentBuffer` ativo;
- a fila continua sendo filesystem-based, não DB-based.
