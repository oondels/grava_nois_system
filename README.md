# Grava Nóis — Capture MVP

> Objetivo: capturar replays com pré/pós-buffer, gerar um highlight, enfileirar para tratamento (marca d’água + thumbnail + metadados) para rodar em Raspberry Pi.

- **Arquivos principais**: `video_core.py` (núcleo) e `capture_service.py` (serviço + worker)
- **Dependências**: Python 3.10+, FFmpeg/ffprobe
- **Fluxo**: _Captura contínua_ → _Highlight on-demand_ → _Fila_ → _Watermark + Thumbnail_ → _Registro no backend (URL assinada)_

---

## GPIO usado + Execução rápida

- GPIO (BCM): 17 — conforme `.env` (`GN_GPIO_PIN=17`).
- Fiação: botão entre `BCM 17` e `GND` (pull-up interno habilitado).
- Daemon: `pigpiod` deve estar ativo para o disparo físico.

Como rodar no Raspberry Pi (TL;DR):

```
sudo apt update && sudo apt install -y ffmpeg pigpio
sudo systemctl enable --now pigpiod   # ou rode apenas: pigpiod

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
# (MoviePy não é necessário: watermark/thumbnail implementados com ffmpeg)

export GN_GPIO_PIN=17                 # ou mantenha no .env
python3 capture_service.py            # ENTER ou botão no BCM 17 dispara
```

Detalhes completos estão nas seções “Como rodar” e “GPIO (botão físico)” abaixo.

Modo leve (recomendado no Raspberry Pi 3B/1GB):

```
export GN_LIGHT_MODE=1               # pular watermark/thumbnail, upload direto
python3 capture_service.py
```

## 1) Visão Geral do Fluxo

```
[ Câmera /dev/video0 ]
       │
       ▼
FFmpeg (segmentos de 1s)  →  /tmp/recorded_videos/buffer%06d.mp4
       │       ▲
       │       └── Thread indexadora (SegmentBuffer)
       │
ENTER/GPIO → build_highlight() concatena os últimos (pré+pos) → recorded_clips/highlight_*.mp4
       │
       └── enqueue_clip() move p/ fila + sidecar JSON → queue_raw/
                               │
                               ▼
                     Worker varredura (ProcessingWorker)
                               │
        add_image_watermark()  │  generate_thumbnail()
             ▼                 │         ▼
       20_highlights_wm/  ◄────┴─────────┘
             │
             ├── POST /api/videos/metadados → URL assinada
             └── (próx.) uploader usa URL → storage
```

**Diretórios (por padrão):**

- `buffer_dir`: `/tmp/recorded_videos` — segmentos de 1s do FFmpeg.
- `clips_dir`: `./recorded_clips` — _highlights_ gerados na hora do clique.
- `queue_dir`: `./queue_raw` — fila de processamento (entrada do worker).
- `20_highlights_wm/` — saída final do worker (com watermark + thumbnail).
- `90_failed/` — entrada que falhou após retentativas.

### 1.1) Fonte de vídeo (RTSP x V4L2)

O código atual está configurado para fonte **RTSP (câmera dedicada)** dentro de `start_ffmpeg()` em `video_core.py`.

- Para RTSP, edite a URL dentro do array `ffmpeg_cmd` (já ativo no código):

  - Linha com `-i` → `rtsp://<user>:<pass>@<host>:<port>/cam/realmonitor?channel=1&subtype=0`
  - Ajuste credenciais/host/porta conforme sua câmera.

- Para usar a câmera local (V4L2, ex.: `/dev/video0`), use o bloco comentado “Old -> Camera do notebook” no mesmo método, e comente o bloco RTSP. O campo `CaptureConfig.device` é respeitado nesse modo.

Observação: `stdout`/`stderr` do FFmpeg estão direcionados para `DEVNULL` para reduzir ruído; habilite logs se necessário.

---

## 2) Como rodar

1. **Instale FFmpeg** (inclui `ffprobe`). Em Debian/Ubuntu/Raspberry Pi OS:

   ```
   sudo apt update && sudo apt install -y ffmpeg
   ```

2. **Crie venv**:

   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   # dependências estão em requirements.txt
   ```

3. **Estruture os arquivos** no mesmo diretório:
   - `video_core.py`
   - `capture_service.py`
   - `files/grava-nois.png` (logo para a marca d’água)
4. **Execute**:

   ```
   python3 capture_service.py
   ```

5. **Gerar highlight**: pressione **ENTER/GPIO**. O sistema concatena **40s antes** + **10s depois** (ajustável) e enfileira para o worker.

> Dica: para produção via systemd, defina WorkingDirectory, direcione stdout/stderr para logs e garanta que o serviço finalize com SIGTERM (o worker e a thread indexadora fazem shutdown limpo).

### 2.1) Fluxo concreto (passo a passo)

1. Recorder (FFmpeg) grava segmentos `buffer%06d.mp4` em `buffer_dir` a cada `seg_time` segundo(s).
2. `SegmentBuffer` (thread) indexa e mantém apenas os últimos `max_buffer_seconds / seg_time` segmentos, removendo excedentes do disco.
3. Ao pressionar ENTER, `build_highlight()` aguarda `post_seconds`, seleciona `pre_seconds + post_seconds` de segmentos, cria `to_concat_*.txt` e concatena com `ffmpeg -f concat -c copy` para `clips_dir/highlight_*.mp4`.
4. `enqueue_clip()` move o highlight para `queue_dir` e grava sidecar JSON com metadados de `ffprobe` e hash `sha256`.
5. `ProcessingWorker` varre `queue_dir` periodicamente, faz lock com `.lock`, e para cada item:
   - Aplica watermark com ffmpeg para um `*.wm_tmp.mp4` e faz `replace()` atômico para `20_highlights_wm/highlight_*.mp4`.
   - Gera `20_highlights_wm/highlight_*.jpg` (thumbnail).
   - Atualiza o JSON na fila com `status="watermarked"`, caminhos de saída e `meta_wm` (ffprobe do arquivo final).
   - Envia POST `GN_API_BASE/api/videos/metadados` com metadados do arquivo final; salva a resposta no sidecar em `remote_registration`.
   - Remove o `.mp4` original da fila (o `.json` permanece como registro de processamento).
6. Em caso de erro, incrementa `attempts`; com `attempts >= max_attempts`, move `.mp4` e `.json` para `90_failed/` e grava `*.error.txt`; caso contrário, mantém na fila com `status="queued_retry"` e backoff linear.

---

## 3) Sidecar JSON (metadados)

Ao enfileirar (`enqueue_clip`), é criado um `highlight_*.json` na mesma pasta da fila:

```
{
  "type": "highlight_raw",
  "status": "queued",
  "created_at": "2025-08-17T19:50:00Z",
  "file_name": "highlight_20250817-195000Z.mp4",
  "size_bytes": 1234567,
  "sha256": "...",
  "meta": { "codec": "h264", "width": 1280, "height": 720, "fps": 30, "duration_sec": 50.0 },
  "pre_seconds": 40,
  "post_seconds": 10,
  "seg_time": 1
}
```

Após o worker, o JSON recebe:

```
{
  "status": "watermarked",
  "updated_at": "2025-08-17T19:51:10Z",
  "wm_path": "/.../20_highlights_wm/highlight_...mp4",
  "thumbnail_path": "/.../20_highlights_wm/highlight_....jpg",
  "meta_wm": { "codec": "h264", "width": 1280, "height": 720, "fps": 30, "duration_sec": 50.0 }
}
```

---

## 4) Detalhes de Concorrência e Robustez

- **SegmentBuffer (thread daemon)**: indexa periodicamente os segmentos, mantém buffer circular e apaga excedentes no disco.
- **ProcessingWorker (thread daemon)**: varre `queue_raw/`, faz _claim_ com arquivo `.lock`, processa com idempotência e retentativas.
- **Shutdown limpo**: eventos de parada + `join()` com _timeout_ na thread indexadora e no worker.
- **Atômico**: escrita de saídas via `.wm_tmp.mp4` seguida de `Path.replace()`.

---

## 5) Documentação de Código — `video_core.py`

### `CaptureConfig` (dataclass)

- **Campos**:
  - `buffer_dir: Path` — onde ficam os segmentos `buffer%06d.mp4`.
  - `clips_dir: Path` — onde nasce o highlight após concat.
  - `queue_dir: Path` — fila para processamento (entrada do worker).
  - `device: str = "/dev/video0"` — dispositivo V4L2.
  - `seg_time: int = 1` — segundos por segmento.
  - `pre_seconds: int = 40`, `post_seconds: int = 10` — janela do highlight.
  - `scan_interval: float = 0.5` — período da indexação.
  - `max_buffer_seconds: int = 80` — retenção máxima no disco.
- **Propriedades/Métodos**:
  - `max_segments` — `max(1, max_buffer_seconds/seg_time)`.
  - `ensure_dirs()` — cria `buffer_dir`, `clips_dir`, `queue_dir`.

### `_calc_start_number(buffer_dir: Path) -> int`

Calcula o próximo número inicial para `-segment_start_number` com base nos arquivos existentes.

### `start_ffmpeg(cfg: CaptureConfig) -> subprocess.Popen`

Inicia o FFmpeg em modo segmentado com:

- `f v4l2 -i /dev/video0`
- H.264 `ultrafast` + `zerolatency`
- `force_key_frames expr:gte(t,n_forced*seg_time)`
- `f segment -segment_time seg_time -reset_timestamps 1`

**Retorno**: `Popen` do processo do FFmpeg.

### `SegmentBuffer`

Mantém uma visão ordenada dos segmentos recentes.

- `start()` — inicia a thread indexadora (daemon).
- `stop(join_timeout=2.0)` — sinaliza parada e `join()`.
- `snapshot_last(n)` — devolve os últimos `n` caminhos (cópia segura com lock).
- `_index_loop()` — varre diretório, apaga excedentes e atualiza deque sob lock.

### `build_highlight(cfg, segbuf) -> Path | None`

Aguarda o pós-buffer, gera arquivo `to_concat.txt` e concatena sem recodificar usando `ffmpeg -f concat -c copy`. Retorna o caminho do highlight salvo em `clips_dir`.

### `_sha256_file(path, chunk=1MiB) -> str`

Calcula SHA-256 em _streaming_.

### `ffprobe_metadata(path) -> dict`

Executa `ffprobe` para extrair `codec`, `width`, `height`, `fps`, `duration_sec`.

### `enqueue_clip(cfg, clip_path) -> Path`

Move o highlight para `queue_dir` e cria sidecar JSON com metadados e `status="queued"`. Em modo leve (`GN_LIGHT_MODE=1`), não calcula SHA-256 aqui (para poupar CPU); o hash é calculado apenas no momento do upload. Retorna o destino na fila.

### `add_image_watermark(input_path, watermark_path, output_path, margin=24, opacity=0.6, rel_width=0.2, codec="libx264", crf=20, preset="medium")`

Aplica marca d’água usando ffmpeg no **canto inferior direito**:

- `rel_width` escala a logo para `rel_width * largura_do_vídeo`.
- Posição `(x, y) = (W - w - margin, H - h - margin)`.
- Usa `overlay` com ajuste de opacidade e reencode H.264 (`crf` e `preset`).

### `generate_thumbnail(input_path, output_path, at_sec=None)`

Gera uma imagem `.jpg` (meio do vídeo ou tempo específico) usando ffmpeg (`-ss ... -frames:v 1`).

---

## 6) Documentação de Código — `capture_service.py`

### `ProcessingWorker`

Worker de varredura de diretório para aplicar watermark e gerar thumbnail. Em modo leve, faz upload direto do vídeo sem pós-processamento.

**`__init__(queue_dir, out_wm_dir, failed_dir, watermark_path, scan_interval=1.5, max_attempts=3, wm_margin=24, wm_opacity=0.6, wm_rel_width=0.2, *, light_mode=False)`**

- **queue_dir**: pasta de entrada (`queue_raw/`).
- **out_wm_dir**: pasta de saída com watermark (`20_highlights_wm/`). Ignorada quando `light_mode=True`.
- **failed_dir**: pasta para falhas definitivas (`90_failed/`).
- **watermark_path**: caminho do PNG da logo.
- **scan_interval**: período da varredura.
- **max_attempts**: número máximo de tentativas por item.
- **wm_margin/opacidade/rel_width**: parâmetros da marca d’água.

**`start()` / `stop(timeout=2.0)`**: controla a thread interna do worker (daemon).

**`_loop()`**: laço contínuo com `scan_interval`, trata exceções e respeita _stop event_.

**`_scan_once()`**:

- Varre `queue_dir` por `.mp4`.
- Garante sidecar JSON (cria mínimo se ausente).
- Faz _claim_ por `.lock` (`os.O_CREAT | os.O_EXCL`).
- Chama `_process_one()` e libera o lock.

**`_process_one(mp4, meta_path)`**:

- Modo normal: aplica watermark (canto inferior direito) e gera thumbnail; atualiza sidecar (`status="watermarked"`, caminhos e `meta_wm`).
- Modo leve: atualiza sidecar para `status="ready_for_upload"` e faz upload diretamente do `mp4` da fila (sem reencode/thumbnail). Após sucesso, notifica o backend e remove o arquivo da fila.

**`_handle_failure(mp4, meta_path, err)`**:

- Incrementa `attempts`, atualiza `status` e `last_error`.
- Se `attempts >= max_attempts` move mp4/json para `failed_dir` e registra `.error.txt`.
- Caso contrário, mantém na fila com `status="queued_retry"` e aplica _backoff_ simples.

### `main() -> int`

- Monta `CaptureConfig` e cria diretórios.
- Inicia FFmpeg + `SegmentBuffer`.
- Inicia `ProcessingWorker`.
- Loop principal: **ENTER** → `build_highlight()` → `enqueue_clip()`.
- Finalização: `stop()`/`terminate()` com `try/finally`.

---

## 7) Parâmetros importantes (tuning)

- `pre_seconds` / `post_seconds`: janela do highlight. Aumentar `pre_seconds` consome mais disco/tempo para concat.
- `seg_time`: segmento menor → mais precisão no corte; maior → menos arquivos.
- `max_buffer_seconds`: retenção no disco para evitar encher `/tmp`.
- Watermark: ajuste `rel_width` e `margin` conforme resolução do vídeo.
- Encoder preset: use `ultrafast` no Raspberry Pi; `crf 20–23` costuma equilibrar qualidade/tamanho.

### 7.1) GPIO (botão físico)

O disparo por botão físico no Raspberry Pi está disponível e usa exclusivamente a biblioteca `pigpio` e o daemon `pigpiod` (sem `RPi.GPIO`).

- Defina o pino BCM via variável de ambiente: `GN_GPIO_PIN` (ou `GPIO_PIN`).
- Conexão: habilite pull-up interno; conecte o botão entre o pino e GND.
- Borda considerada: FALLING (pressionado). Debounce padrão de 300 ms.
- Cooldown: após um disparo válido via GPIO, novos pressionamentos são ignorados por 120s
  (configurável via `GN_GPIO_COOLDOWN_SEC`).
- Requisito: daemon `pigpiod` em execução. Se não estiver ativo, rode `pigpiod` em outro terminal/serviço antes de iniciar o script.

Exemplo (bash):

```
pigpiod                        # inicia o daemon (sem sudo)
export GN_GPIO_PIN=17          # pino BCM
export GN_GPIO_DEBOUNCE_MS=300 # opcional
export GN_GPIO_COOLDOWN_SEC=120 # opcional; padrão 120
python3 capture_service.py
```

Se `pigpio`/`pigpiod` não estiver disponível, o serviço continua funcionando apenas com ENTER.

---

## 8) Roadmap (próximos passos)

- **Uploader**: implementar worker de upload com URL pré-assinada, seguindo o fluxo já definido no backend.
- **GPIO**: substituir ENTER por botão físico (GPIO) chamando `build_highlight()`.
- **Logs** e métricas: logging estruturado (JSON) e contadores de sucesso/falha.
- **Watchdog**: substituir varredura por eventos de filesystem para reduzir latência.

---

## 9) Troubleshooting

- **Sem vídeo/permite acesso**: verifique permissões de `/dev/video0` e módulos V4L2.
- **FFmpeg ausente**: instale `ffmpeg` (inclui `ffprobe`).
- **CPU alta** no Pi: evite rodar múltiplas marca d’água em paralelo; mantenha `preset=ultrafast`.
- **Arquivos não aparecem**: confirme caminhos (`buffer_dir`, `clips_dir`, `queue_dir`) e se o processo tem permissões de escrita.

---

## 10) Licença e créditos

MVP interno do projeto **Grava Nóis**. Uso restrito ao time até formalização de licença.

---

## 11) Integração com Backend (registro de metadados)

Após a geração de watermark e thumbnail, o worker executa uma requisição para registrar a intenção de criar o clipe (sem fazer upload do vídeo).

- Endpoint: `POST {API_BASE}/api/videos/metadados`
- Requisição: JSON com metadados principais do arquivo final (`file_name`, `size_bytes`, `sha256`, `mime_type`, `meta` com `{codec,width,height,fps,duration_sec}`, além de `pre_seconds`/`post_seconds`).
- Resposta esperada:

```
{
  "clip_id": "nanoid-clip-id",
  "contract_type": "per_video",
  "storage_path": "temp/uuid-client/uuid-venue/nanoid-clip-id.mp4",
  "upload_url": "https://...signed-url...",
  "expires_hint_hours": 12
}
```

- Persistência: o retorno é salvo em `queue_raw/<stem>.json` sob `remote_registration.response`, com `status` e timestamps.

### Configuração

Defina variáveis de ambiente antes de rodar o serviço:

- `GN_API_BASE` (ou `API_BASE_URL`): base do backend (ex.: `http://localhost:3000`).
- `GN_API_TOKEN` (ou `API_TOKEN`): token JWT/Bearer se a rota exigir autenticação.

Se `GN_API_BASE` não for definido, o passo de registro é ignorado e anotado como `remote_registration.status = "skipped"` no sidecar.
