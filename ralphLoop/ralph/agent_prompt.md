PRD: Refatoração para Suporte a Múltiplas Câmeras RTSP (N câmeras)

ROLE:
Você é um code agent responsável por refatorar o sistema atual (single-camera)
para suportar múltiplas câmeras RTSP com isolamento, concorrência segura
e compatibilidade retroativa.

OBJETIVO PRINCIPAL:
Evoluir a arquitetura para suportar N câmeras simultâneas, garantindo:
- isolamento por câmera
- concorrência segura no trigger
- paths sem colisão
- compatibilidade com configuração legada

INTRODUÇÃO:
Este planejamento define a migração do Grava Nóis de um fluxo single-camera
para uma arquitetura multi-camera RTSP, com foco em previsibilidade operacional,
isolamento de falhas e manutenção da compatibilidade com o ambiente atual.

A execução proposta é incremental e orientada por risco: primeiro estabiliza
configuração e modelo de domínio por câmera, depois evolui runtime e concorrência
de trigger, e por fim consolida isolamento de arquivos, observabilidade e documentação.

O objetivo é permitir crescimento para N câmeras sem regressões de comportamento
no modo legado, mantendo deploy gradual e validação contínua por fase.

---------------------------------------------------------------------
SEÇÃO 1 — DIAGNÓSTICO DO ESTADO ATUAL
---------------------------------------------------------------------

O sistema atual é single-camera e apresenta os seguintes acoplamentos:

1. main.py
   - Detecta modo RTSP via GN_RTSP_URL global (sem seleção por câmera)
   - Cria apenas 1 CaptureConfig, 1 processo FFmpeg, 1 SegmentBuffer e 1 ProcessingWorker
   - Trigger loop executa build_highlight() de forma síncrona (inclui espera de pós-buffer), serializando capturas
   - Cooldown de trigger usa estado único (last_gpio_ok_ts), sem escopo por câmera

2. capture.py
   - start_ffmpeg(cfg) ainda lê GN_RTSP_URL global, em vez de cfg.rtsp_url
   - Configurações de conectividade RTSP e log de FFmpeg são globais (ex.: logs/ffmpeg.log)

3. CaptureConfig
   - Não possui camera_id
   - Não possui rtsp_url, camera_name ou source_type
   - Não representa identidade de câmera nem origem da captura

4. processor.py
   - build_highlight() e artefatos temporários usam timestamp em segundos (concat/highlight), sem entropia extra
   - enqueue_clip() gera sidecar sem camera_id/trigger_id
   - Em multi-camera ou múltiplos triggers no mesmo segundo, há risco real de colisão

5. Paths e worker globais
   - Diretórios atuais são únicos: queue_raw, failed_clips, recorded_clips, highlights_wm
   - ProcessingWorker varre apenas queue_dir/*.mp4 (sem namespace por câmera)

6. Buffer compartilhado por padrão
   - GN_BUFFER_DIR padrão aponta para /dev/shm/grn_buffer único
   - Segmentos buffer%06d.ts no mesmo diretório não isolam streams simultâneos

---------------------------------------------------------------------
SEÇÃO 2 — GESTÃO DE CONFIGURAÇÃO
---------------------------------------------------------------------

### Fonte principal

Adotar:

GN_CAMERAS_JSON

Formato:

[
  {
    "id": "cam01",
    "name": "quadra_norte",
    "rtsp_url": "...",
    "enabled": true,
    "pre_segments": 6,
    "post_segments": 3,
    "seg_time": 1,
    "max_buffer_seconds": 40
  }
]

### Compatibilidade retroativa

Ordem de fallback:

1. GN_CAMERAS_JSON
2. GN_RTSP_URLS (CSV)
3. GN_RTSP_URL (legado)

---------------------------------------------------------------------
SEÇÃO 3 — NOVO MODELO DE CONFIGURAÇÃO
---------------------------------------------------------------------

Evoluir CaptureConfig para representar uma câmera (utilize tipagem e calsses):

Campos obrigatórios:
- camera_id: str
- rtsp_url: str

Campos opcionais:
- camera_name
- source_type: "rtsp" | "v4l2"
- device
- seg_time
- pre_segments
- post_segments
- max_buffer_seconds

Paths por câmera:
- buffer_dir
- clips_dir
- queue_dir
- failed_dir_highlight

Validações obrigatórias:
- IDs únicos
- rtsp_url presente se source_type=rtsp
- seg_time > 0
- pre/post válidos

---------------------------------------------------------------------
SEÇÃO 4 — VARIÁVEIS GLOBAIS
---------------------------------------------------------------------

Adicionar suporte a:

GN_BUFFER_BASE_DIR
GN_QUEUE_BASE_DIR
GN_CLIPS_BASE_DIR
GN_FAILED_BASE_DIR
GN_WM_BASE_DIR
GN_TRIGGER_MAX_WORKERS

---------------------------------------------------------------------
SEÇÃO 5 — RUNTIME MULTI-CAMERA
---------------------------------------------------------------------

Criar estrutura CameraRuntime:

CameraRuntime:
  cfg: CaptureConfig
  ffmpeg_proc
  segbuf
  capture_lock
  running
  last_error
  last_trigger_ts

Em main.py:
dict[camera_id, CameraRuntime]

Startup:
- validar configs
- iniciar ffmpeg por câmera
- iniciar SegmentBuffer por câmera

Shutdown:
- parar segbuf
- terminar ffmpeg com timeout
- evitar join indefinido

---------------------------------------------------------------------
SEÇÃO 6 — TRIGGER FAN-OUT
---------------------------------------------------------------------

Trigger único → fan-out para todas as câmeras ativas.

Fluxo:
1. gerar trigger_id
2. validar janela de horário
3. despachar tarefas concorrentes por câmera

Concorrência:
- ThreadPoolExecutor
- max_workers configurável

Por câmera:
- adquirir capture_lock
- se ocupado → registrar busy e pular
- executar build_highlight
- executar enqueue_clip
- logar resultado

Logs agregados:
trigger_id
origem
sucesso/falha por câmera

Sidecar deve incluir:
camera_id
camera_name
trigger_id
trigger_source
trigger_at

---------------------------------------------------------------------
SEÇÃO 7 — ISOLAMENTO DE ARQUIVOS
---------------------------------------------------------------------

Estrutura obrigatória:

/dev/shm/grn_buffer/<camera_id>/buffer%06d.ts
recorded_clips/<camera_id>/*.mp4
queue_raw/<camera_id>/*.mp4 + .json + .lock
highlights_wm/<camera_id>/*.mp4
failed_clips/<camera_id>/*
logs/ffmpeg_<camera_id>.log

Regras:
- nunca usar diretórios globais compartilhados
- nomes devem ser únicos:
  incluir camera_id + timestamp alta resolução ou uuid4
- concat_list temporário por câmera

---------------------------------------------------------------------
SEÇÃO 8 — IMPACTO POR ARQUIVO
---------------------------------------------------------------------

main.py
- supervisor multi-camera
- start/stop por câmera
- trigger concorrente

settings.py
- evoluir CaptureConfig
- parser multi-camera

capture.py
- start_ffmpeg(cfg.rtsp_url)
- logs por câmera

buffer.py
- logs com camera_id

processor.py
- nomes únicos
- sidecar com camera_id

processing_worker.py
- worker por câmera OU suporte recursivo
- logs com camera_id

.env.example
- incluir GN_CAMERAS_JSON

docker-compose.yml
- revisar healthcheck multi-ffmpeg

docs/*
- documentar arquitetura multi-camera

---------------------------------------------------------------------
SEÇÃO 9 — FASES DE IMPLEMENTAÇÃO
---------------------------------------------------------------------

FASE A — Configuração
- parser multi-camera
- validação
- compatibilidade legado

FASE B — Runtime
- supervisor multi-camera
- ffmpeg + segbuf por câmera

FASE C — Trigger concorrente
- fan-out
- lock por câmera
- trigger_id

FASE D — Isolamento de paths
- diretórios por câmera
- sidecar com camera_id

FASE E — Compatibilidade e docs
- atualizar env, scripts e docs
- criar testes

---------------------------------------------------------------------
SEÇÃO 10 — CRITÉRIOS DE ACEITAÇÃO
---------------------------------------------------------------------

O sistema será considerado correto se:

1. N câmeras RTSP funcionarem simultaneamente
2. Trigger gerar highlights independentes por câmera
3. Não houver colisões de arquivos
4. Falha em uma câmera não derrubar as demais
5. Configuração legada continuar funcional
6. Logs e sidecars incluírem camera_id

---------------------------------------------------------------------
FIM DO PROMPT
---------------------------------------------------------------------
