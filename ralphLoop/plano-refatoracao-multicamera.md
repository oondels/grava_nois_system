# Plano de Refatoração para Suporte a Múltiplas Câmeras RTSP (N câmeras)

## 1) Diagnóstico do estado atual (single-camera)

- O fluxo está acoplado a uma única fonte em `main.py`:
  - Lê apenas `GN_RTSP_URL` (`main.py:53`).
  - Cria apenas 1 `CaptureConfig`, 1 processo FFmpeg e 1 `SegmentBuffer` (`main.py:65-97`).
  - Trigger chama `build_highlight()` e `enqueue_clip()` de forma serial para essa única fonte (`main.py:245-251`).
- `start_ffmpeg()` em `src/video/capture.py` também lê URL global única (`GN_RTSP_URL`) em vez de receber por câmera (`capture.py:88`).
- `CaptureConfig` não possui identidade de câmera (`camera_id`) nem metadados da fonte (`settings.py:8-31`).
- Arquivos e locks usam diretórios globais únicos (`queue_raw`, `failed_clips`, `recorded_clips`, `highlights_wm`), com nomes por timestamp em segundos (`processor.py:67-90`), o que abre risco de colisão em multi-camera.

---

## 2) Gestão de Configuração (ponto 1 solicitado)

### Recomendação de formato

- **Primário (recomendado): `GN_CAMERAS_JSON`** com lista de objetos por câmera.
- **Compatibilidade retroativa**:
  - Se `GN_CAMERAS_JSON` não existir, usar `GN_RTSP_URLS` (CSV).
  - Se `GN_RTSP_URLS` não existir, usar legado `GN_RTSP_URL` (single-camera).

### Exemplo recomendado (`GN_CAMERAS_JSON`)

```json
[
  {
    "id": "cam01",
    "name": "quadra_norte",
    "rtsp_url": "rtsp://user:pass@192.168.1.101:554/stream1",
    "enabled": true,
    "pre_segments": 6,
    "post_segments": 3,
    "seg_time": 1,
    "max_buffer_seconds": 40
  },
  {,
    "id": "cam02",
    "name": "quadra_sul",
    "rtsp_url": "rtsp://user:pass@192.168.1.102:554/stream1",
    "enabled": true
  }
]
```

### Adaptação da `CaptureConfig`

- Evoluir `CaptureConfig` para representar **uma câmera** explicitamente:
  - `camera_id: str` (obrigatório e único)
  - `camera_name: str | None`
  - `rtsp_url: str | None`
  - `source_type: Literal["rtsp","v4l2"]` (preparar extensões futuras)
  - `device: str | None` (quando não RTSP)
  - manter parâmetros já existentes (`seg_time`, `pre/post_seconds`, `pre/post_segments`, etc.)
  - manter diretórios, mas agora resolvidos por câmera (`buffer_dir`, `clips_dir`, `queue_dir`, `failed_dir_highlight`)
- Validar no bootstrap:
  - IDs únicos
  - URL presente para `source_type=rtsp`
  - sem diretórios duplicados entre câmeras
  - `seg_time > 0`, `pre/post` válidos

### Novas variáveis globais úteis

- `GN_BUFFER_BASE_DIR` (ex.: `/dev/shm/grn_buffer`)
- `GN_QUEUE_BASE_DIR` (ex.: `./queue_raw`)
- `GN_CLIPS_BASE_DIR` (ex.: `./recorded_clips`)
- `GN_FAILED_BASE_DIR` (ex.: `./failed_clips`)
- `GN_WM_BASE_DIR` (ex.: `./highlights_wm`)
- `GN_TRIGGER_MAX_WORKERS` (limite de concorrência no fan-out do trigger)

---

## 3) Orquestração de Processos/Threads

### Estratégia arquitetural

- Introduzir uma estrutura de runtime por câmera (ex.: `CameraRuntime`):
  - `cfg: CaptureConfig`
  - `ffmpeg_proc: subprocess.Popen`
  - `segbuf: SegmentBuffer`
  - `capture_lock: threading.Lock` (evita 2 highlights simultâneos na mesma câmera)
  - estado (`running`, `last_error`, `last_trigger_ts`)
- Em `main.py`, manter um `dict[camera_id, CameraRuntime]`.

### Bootstrap e shutdown

- Startup:
  - Parse e validação das câmeras.
  - Para cada câmera: `clear_buffer(cfg)` -> `cfg.ensure_dirs()` -> `start_ffmpeg(cfg)` -> `SegmentBuffer.start()`.
  - Se uma câmera falhar no start: registrar erro por câmera sem derrubar necessariamente todas (policy configurável: fail-fast x degradação parcial).
- Shutdown:
  - Sinal global de parada.
  - Encerrar listeners de trigger.
  - Para cada runtime: `segbuf.stop()` e `ffmpeg_proc.terminate()` com timeout e fallback seguro.
  - Evitar `join` bloqueante indefinido.

### Evitar deadlocks

- Não compartilhar lock global entre câmeras para build/enqueue.
- Lock por câmera (`capture_lock`) apenas no trecho de highlight daquela câmera.
- Executor dedicado para tarefas de trigger com `max_workers` limitado (backpressure controlado).
- Separar claramente:
  - thread/listener de entrada de trigger
  - loop principal de despacho
  - workers de build por câmera

---

## 4) Trigger GPIO/STDIN para todas as câmeras

### Comportamento proposto

- Trigger continua único (ENTER/GPIO), mas vira **fan-out** para todas as câmeras ativas.
- Ao receber trigger:
  - validar janela de horário uma vez;
  - gerar `trigger_id` único;
  - despachar tarefas concorrentes para cada câmera ativa.

### Concorrência eficiente

- Usar `ThreadPoolExecutor` (ou pool de threads equivalente) para `build_highlight + enqueue_clip`.
- Cada tarefa:
  - tenta adquirir `capture_lock` da câmera;
  - se lock ocupado, registrar `"camera busy"` e pular ou enfileirar retry curto (definir política);
  - executa `build_highlight(cfg, segbuf)` e `enqueue_clip(cfg, out)`;
  - registra métricas/resultado por câmera.

### Observabilidade do trigger multi-camera

- Logar sumário por trigger:
  - `trigger_id`, origem (`gpio`/`enter`), quantidade de câmeras alvo, sucesso/falha por câmera.
- Sidecar deve receber:
  - `camera_id`, `camera_name`, `trigger_id`, `trigger_source`, `trigger_at`.

---

## 5) Isolamento de arquivos e locks por câmera (ponto 4 solicitado)

### Estrutura de diretórios recomendada

```text
/dev/shm/grn_buffer/<camera_id>/buffer%06d.ts
recorded_clips/<camera_id>/highlight_*.mp4
queue_raw/<camera_id>/highlight_*.mp4 + .json + .lock
highlights_wm/<camera_id>/*.mp4
failed_clips/<camera_id>/{build_failed,enqueue_failed,upload_failed,...}
logs/ffmpeg_<camera_id>.log
```

### Regras para evitar sobrescrita

- Nunca usar diretório global compartilhado para artefatos temporários de build.
- Nome de arquivo deve ser monotônico e único:
  - incluir `camera_id` + timestamp de alta resolução (`%Y%m%d-%H%M%S.%fZ`) ou `uuid4`.
- `concat_list` temporário deve ser por câmera e por trigger.
- Locks `.lock` permanecem por stem no mesmo diretório da mídia, que agora já é isolado por câmera.

### Impacto no worker

- `ProcessingWorker` atual trabalha com um `queue_dir`.
- Plano preferencial:
  - manter 1 worker por câmera (`queue_raw/<camera_id>`) para isolamento simples;
  - alternativa futura: worker global recursivo com fila interna e chave por câmera.
- Em ambos os casos, sidecar precisa carregar `camera_id`.

---

## 6) Análise de impacto por arquivo

## `main.py`

- Maior impacto.
- Trocar fluxo single-camera por supervisor multi-camera:
  - parse de múltiplas câmeras;
  - loop de start/stop por câmera;
  - fan-out concorrente no trigger;
  - logging agregado por trigger e por câmera;
  - política de tolerância a falhas de câmera (start/restart).

## `src/config/settings.py`

- Evoluir `CaptureConfig` com identidade e origem da câmera.
- Adicionar parser/validador de configuração multi-camera (pode virar `AppConfig` + `CameraConfig`, mantendo `CaptureConfig` para runtime por câmera).
- `ensure_dirs()` passa a garantir árvores por câmera.

## `src/video/capture.py`

- `start_ffmpeg(cfg)` deve usar `cfg.rtsp_url` (não `GN_RTSP_URL` global).
- `check_rtsp_connectivity()` já é reaproveitável por câmera.
- Logs do FFmpeg devem ser por câmera (`ffmpeg_<camera_id>.log`).
- Manter `_calc_start_number(buffer_dir)` por buffer isolado.

## `src/video/buffer.py`

- Sem mudança estrutural grande; já funciona por instância.
- Ajustes:
  - logs com `camera_id`;
  - garantir robustez em diretórios por câmera.

## `src/video/processor.py`

- Ajustar naming para unicidade forte em multi-camera (evitar colisão por segundo).
- `build_highlight()` deve incluir contexto da câmera em logs e arquivos temporários.
- `enqueue_clip()` deve gravar metadados de câmera/trigger no sidecar.
- Revisar uso de `cfg.clips_dir`/`cfg.queue_dir` assumindo isolamento por câmera.

## `src/workers/processing_worker.py`

- Adaptar para operar por câmera (instância por `queue_dir` de câmera) ou suportar varredura recursiva.
- Incluir `camera_id` nos logs e em sidecars mínimos criados pelo worker.
- Confirmar que `upload_failed`, `.lock` e movimentos de falha usem diretório da câmera.

## `video_core.py` e `src/video/__init__.py`

- Exportar novos tipos/funções de configuração multi-camera.
- Manter compatibilidade pública para chamadas antigas quando possível.

## `.env.example`

- Incluir exemplo de `GN_CAMERAS_JSON` e/ou `GN_RTSP_URLS`.
- Manter `GN_RTSP_URL` como fallback legado documentado.
- Incluir variáveis de base dirs e concorrência de trigger.

## `docker-compose.yml`

- Volumes podem permanecer os mesmos, mas healthcheck atual (`pgrep -f ffmpeg`) precisa revisar:
  - em multi-camera, validar se **todos** os ffmpeg esperados estão ativos (ou ao menos 1, conforme policy).

## `gn_start.sh`

- Template `.env` deve refletir novo formato de câmeras.
- Criação de estrutura de diretórios deve considerar subpastas por câmera (ou deixar bootstrap da app criar dinamicamente).

## `README.md`, `INSTRUCTIONS.md`, `docs/*`

- Atualizar fluxo arquitetural para N câmeras.
- Documentar convenções de `camera_id`, organização de arquivos e comportamento do trigger fan-out.

## `tests/*` (novos testes necessários)

- Config parser:
  - JSON válido/inválido, IDs duplicados, fallback CSV/legado.
- Isolamento de paths:
  - geração por câmera sem colisões.
- Trigger fan-out:
  - 1 trigger dispara N tarefas, respeitando lock por câmera.
- Worker:
  - sidecar com `camera_id` e lock isolation.

---

## 7) Plano de execução em fases (para aprovação)

1. **Fase A - Configuração e modelos**
- Introduzir parser multi-camera + validação.
- Evoluir `CaptureConfig` (ou separar `AppConfig`/`CameraConfig`).
- Garantir backward compatibility com `GN_RTSP_URL`.

2. **Fase B - Runtime multi-camera**
- Refatorar `main.py` para supervisor com múltiplos runtimes.
- Subir/parar FFmpeg+SegmentBuffer por câmera.
- Logs e health por câmera.

3. **Fase C - Trigger fan-out concorrente**
- Implementar despacho concorrente por câmera com pool limitado.
- Adicionar lock por câmera e `trigger_id`.

4. **Fase D - Isolamento de artefatos**
- Migrar paths para estrutura por câmera.
- Atualizar `build_highlight`/`enqueue_clip`/worker para sidecar com `camera_id`.
- Ajustar retries e locks em `failed_clips/<camera_id>`.

5. **Fase E - Compatibilidade e documentação**
- Atualizar `.env.example`, scripts, docs, docker healthcheck.
- Criar testes de regressão e multi-camera.

---

## 8) Decisões recomendadas para aprovação

- Adotar `GN_CAMERAS_JSON` como fonte principal de configuração.
- Manter fallback legado (`GN_RTSP_URL`) para migração gradual.
- Isolamento por diretório de câmera como regra mandatória.
- Trigger com fan-out concorrente e lock por câmera para estabilidade.
