# Diagrama de Fluxo Funcional — Grava Nóis System

Este documento apresenta o fluxo funcional completo do sistema de captura e processamento de vídeos do Grava Nóis.

## Visão Geral

O sistema captura vídeo continuamente de uma câmera RTSP ou V4L2, mantém um buffer circular de segmentos, e ao receber um trigger (botão GPIO ou ENTER), constrói um clipe highlight concatenando segmentos pré e pós-clique. O clipe é então enfileirado para processamento (opcional: watermark), upload para storage cloud via URL assinada, e registro no backend.

---

## Fluxo Principal (Mermaid)

```mermaid
flowchart TD
    Start([Inicialização do Sistema]) --> HealthCheck{Health Check<br/>RTSP?}

    HealthCheck -->|Câmera RTSP| CheckConn[Verifica Conectividade<br/>TCP com Câmera]
    CheckConn -->|Sucesso| StartFFmpeg[Inicia FFmpeg]
    CheckConn -->|Falha após<br/>10 tentativas| Error([Erro: Câmera<br/>Inacessível])

    HealthCheck -->|Câmera V4L2| StartFFmpeg

    StartFFmpeg --> BufferLoop[Buffer Circular<br/>Segmentos .ts em<br/>/dev/shm]

    BufferLoop --> IndexThread[Thread Indexadora<br/>SegmentBuffer]
    IndexThread -->|Periodicamente| CleanOld[Remove Segmentos<br/>Excedentes]
    CleanOld --> IndexThread

    BufferLoop --> WaitTrigger{Aguarda<br/>Trigger}

    WaitTrigger -->|ENTER| TriggerEvent[Evento de<br/>Captura]
    WaitTrigger -->|GPIO Botão| CooldownCheck{Cooldown<br/>Ativo?}

    CooldownCheck -->|Sim| WaitTrigger
    CooldownCheck -->|Não| TriggerEvent

    TriggerEvent --> WaitPost[Aguarda Pós-Buffer<br/>post_seconds]
    WaitPost --> SelectSegs[Seleciona N Segmentos<br/>pre_seg + post_seg]
    SelectSegs --> Validate{Segmentos<br/>Válidos?}

    Validate -->|Não| ErrorBuild[Move para<br/>failed_clips/build_failed]
    ErrorBuild --> WaitTrigger

    Validate -->|Sim| ConcatTS[Concatena .ts<br/>com genpts]
    ConcatTS --> RemuxMP4[Remux para .mp4<br/>com faststart]
    RemuxMP4 --> SaveClip[Salva em<br/>recorded_clips/]

    SaveClip --> Enqueue[Move para queue_raw/<br/>+ Cria Sidecar JSON]

    Enqueue --> WorkerScan[ProcessingWorker<br/>Varre Fila]

    WorkerScan --> LockFile{Acquire<br/>Lock?}
    LockFile -->|Falha| WorkerScan
    LockFile -->|Sucesso| LightMode{Light<br/>Mode?}

    LightMode -->|Não| ApplyWM[Aplica Watermark<br/>ffmpeg overlay]
    ApplyWM --> SaveWM[Salva em<br/>highlights_wm/]
    SaveWM --> RegisterMeta[POST /api/videos/metadados]

    LightMode -->|Sim| RegisterMeta

    RegisterMeta -->|Sem API_BASE| SkipRemote[Status: skipped<br/>Move para failed_clips/upload_failed]
    SkipRemote --> WorkerScan

    RegisterMeta -->|Erro| FailReg[Status: registration_failed<br/>Move para failed_clips/upload_failed]
    FailReg --> RetryLogic{Attempts <<br/>Max?}

    RegisterMeta -->|Sucesso| GetURL{Upload URL<br/>Presente?}

    GetURL -->|Não| SkipUpload[Status: no_upload_url<br/>Move para failed_clips/upload_failed]
    SkipUpload --> WorkerScan

    GetURL -->|Sim| CalcHash[Calcula SHA256<br/>do Vídeo]
    CalcHash --> Upload[HTTP PUT para<br/>URL Assinada Supabase]

    Upload -->|Erro| FailUpload[Status: upload_failed<br/>Move para failed_clips/upload_failed]
    FailUpload --> RetryLogic

    Upload -->|HTTP 2xx| Finalize[POST /api/videos/:id/uploaded<br/>Valida Integridade]

    Finalize -->|Erro| FailFinalize[Status: finalize_failed<br/>Move para failed_clips/upload_failed]
    FailFinalize --> RetryLogic

    Finalize -->|Sucesso| Cleanup[Remove Artefatos<br/>da Fila]
    Cleanup --> WorkerScan

    RetryLogic -->|Não| Requeue[Requeue com Backoff]
    Requeue --> WorkerScan

    RetryLogic -->|Sim| MoveFailed[Move para<br/>failed_clips/<br/>+ .error.txt]
    MoveFailed --> RetryScanner[Retry Scanner<br/>failed_clips/upload_failed]

    RetryScanner -->|Age > Min<br/>Attempts < Max| LockFile
    RetryScanner --> RetryScanner

    WaitTrigger -.->|Ctrl+C| Shutdown([Shutdown Limpo])

    style Start fill:#4a90e2
    style Error fill:#e74c3c
    style Shutdown fill:#95a5a6
    style TriggerEvent fill:#f39c12
    style Cleanup fill:#27ae60
    style HealthCheck fill:#9b59b6
    style LightMode fill:#16a085
```

---

## Componentes Principais

### 1. Inicialização e Health Check

```mermaid
sequenceDiagram
    participant Main as main()
    participant Core as video_core
    participant Camera as Câmera RTSP/V4L2
    participant FFmpeg as Processo FFmpeg

    Main->>Core: start_ffmpeg(cfg)
    Core->>Core: check_rtsp_connectivity(url)

    loop Até 10 tentativas
        Core->>Camera: TCP connect (porta 554)
        alt Sucesso
            Camera-->>Core: Connected
            Core->>FFmpeg: Inicia captura segmentada
            FFmpeg-->>Main: Popen handle
        else Falha
            Camera-->>Core: Connection refused
            Core->>Core: sleep(5s)
        end
    end

    alt Todas tentativas falharam
        Core-->>Main: RuntimeError
    end
```

### 2. Buffer Circular (SegmentBuffer)

```mermaid
stateDiagram-v2
    [*] --> Indexing: start()

    Indexing --> Scanning: A cada scan_interval
    Scanning --> Listing: glob buffer*.ts
    Listing --> Sorting: Ordena por número
    Sorting --> Trimming: Remove excedentes
    Trimming --> Updating: Atualiza deque
    Updating --> Scanning

    Scanning --> Stopped: stop() chamado
    Stopped --> [*]

    note right of Indexing
        Thread daemon que mantém
        índice atualizado dos
        últimos max_segments
    end note

    note right of Trimming
        Apaga do disco segmentos
        além de max_buffer_seconds
    end note
```

### 3. Construção de Highlight

```mermaid
flowchart LR
    Trigger[Trigger Recebido] --> Wait[Aguarda post_seconds]
    Wait --> Snapshot[snapshot_last N]
    Snapshot --> Validate{Segmentos<br/>Válidos?}

    Validate -->|Não| Fail[Retorna None]
    Validate -->|Sim| CreateList[Cria concat_list.txt]

    CreateList --> ConcatTS[ffmpeg concat<br/>-fflags +genpts+igndts]
    ConcatTS --> RemuxMP4[ffmpeg remux<br/>-avoid_negative_ts make_zero]
    RemuxMP4 --> Success[Retorna Path do .mp4]

    style Success fill:#27ae60
    style Fail fill:#e74c3c
```

### 4. ProcessingWorker Pipeline

```mermaid
flowchart TD
    Scan[Varre queue_raw/] --> FindMP4[Lista *.mp4]
    FindMP4 --> CheckJSON{Sidecar<br/>Existe?}

    CheckJSON -->|Não| CreateJSON[Cria JSON mínimo<br/>com ffprobe]
    CheckJSON -->|Sim| Lock{Acquire<br/>.lock?}
    CreateJSON --> Lock

    Lock -->|Falha| Scan
    Lock -->|Sucesso| Process[_process_one]

    Process --> Mode{light_mode?}

    Mode -->|Não| WM[add_image_watermark<br/>canto inferior direito]
    WM --> SaveWM[Atomic replace para<br/>highlights_wm/]
    SaveWM --> UpdateJSON1[Atualiza JSON:<br/>status=watermarked<br/>wm_path, meta_wm]

    Mode -->|Sim| UpdateJSON2[Atualiza JSON:<br/>status=ready_for_upload<br/>meta_raw]

    UpdateJSON1 --> Register
    UpdateJSON2 --> Register

    Register[POST /api/videos/metadados] --> RegResult{Sucesso?}

    RegResult -->|Não| HandleFail[_handle_failure]
    RegResult -->|Sim| UploadURL{upload_url<br/>presente?}

    UploadURL -->|Não| PendUpload[Move para<br/>upload_failed]
    UploadURL -->|Sim| CalcSHA[_sha256_file]

    CalcSHA --> PutReq[HTTP PUT para<br/>signed URL]
    PutReq --> PutResult{HTTP 2xx?}

    PutResult -->|Não| HandleFail
    PutResult -->|Sim| Finalize[POST /api/videos/:id/uploaded]

    Finalize --> FinResult{Sucesso?}

    FinResult -->|Não| HandleFail
    FinResult -->|Sim| CleanupQueue[Remove da fila]

    CleanupQueue --> ReleaseLock[Remove .lock]
    HandleFail --> RetryDecision{attempts <<br/>max?}

    RetryDecision -->|Sim| MoveFail[Move para failed_clips/<br/>+ .error.txt]
    RetryDecision -->|Não| Backoff[Backoff + Requeue]

    MoveFail --> ReleaseLock
    Backoff --> ReleaseLock
    PendUpload --> ReleaseLock

    ReleaseLock --> Scan

    style CleanupQueue fill:#27ae60
    style HandleFail fill:#e74c3c
    style Register fill:#3498db
```

### 5. Retry Scanner (Falhas de Upload)

```mermaid
flowchart TD
    Start[Retry Scanner] --> Check{retry_failed<br/>enabled?}
    Check -->|Não| End([Skip])
    Check -->|Sim| ScanFailed[Varre failed_clips/upload_failed]

    ScanFailed --> ListVids[Lista *.mp4 + *.ts]
    ListVids --> ForEach[Para cada vídeo]

    ForEach --> LockRetry{Acquire<br/>.lock?}
    LockRetry -->|Falha| ForEach
    LockRetry -->|Sucesso| CheckMeta{JSON<br/>existe?}

    CheckMeta -->|Não| CreateMin[Cria JSON mínimo]
    CheckMeta -->|Sim| LoadMeta[Carrega JSON]
    CreateMin --> LoadMeta

    LoadMeta --> CheckAttempts{attempts <<br/>max?}
    CheckAttempts -->|Não| CheckAPI{API_BASE<br/>configurada?}
    CheckAttempts -->|Sim| Skip1[Skip]

    CheckAPI -->|Não| Skip2[Skip<br/>Log esporádico]
    CheckAPI -->|Sim| CheckAge{Age ><br/>backoff?}

    CheckAge -->|Não| Skip3[Skip<br/>Aguarda]
    CheckAge -->|Sim| CheckStatus{Status<br/>elegível?}

    CheckStatus -->|Não| Skip4[Skip]
    CheckStatus -->|Sim| IncrAttempt[Incrementa attempts<br/>Status = queued_retry]

    IncrAttempt --> Reprocess[_process_one]
    Reprocess --> Result{Sucesso?}

    Result -->|Sim| RemoveFromFailed[Remove de failed_clips/]
    Result -->|Não| StayFailed[Permanece em failed_clips/]

    RemoveFromFailed --> ReleaseLock
    StayFailed --> ReleaseLock
    Skip1 --> ReleaseLock
    Skip2 --> ReleaseLock
    Skip3 --> ReleaseLock
    Skip4 --> ReleaseLock

    ReleaseLock --> ForEach

    style IncrAttempt fill:#f39c12
    style RemoveFromFailed fill:#27ae60
```

---

## Estrutura de Diretórios

```
grava_nois_system/
├── /dev/shm/grn_buffer/          # Buffer de segmentos (volátil)
│   └── buffer000001.ts ... bufferNNNNNN.ts
│
├── recorded_clips/               # Highlights recém-construídos
│   └── highlight_YYYYMMDD-HHMMSSZ.mp4
│
├── queue_raw/                    # Fila de processamento
│   ├── highlight_*.mp4           # Vídeo aguardando processamento
│   ├── highlight_*.json          # Sidecar com metadados
│   └── highlight_*.lock          # Lock para concorrência
│
├── highlights_wm/                # Vídeos com watermark (modo completo)
│   └── highlight_*.mp4
│
├── failed_clips/                 # Falhas organizadas por tipo
│   ├── build_failed/             # Erros na concatenação
│   ├── enqueue_failed/           # Erros ao enfileirar
│   └── upload_failed/            # Erros de upload (elegível para retry)
│       ├── highlight_*.mp4
│       ├── highlight_*.json
│       └── highlight_*.error.txt
│
├── logs/                         # Logs do sistema
│   └── ffmpeg.log                # Output do FFmpeg
│
└── files/                        # Assets
    └── replay_grava_nois.png     # Watermark
```

---

## Sidecar JSON (Evolução de Estados)

### Estado Inicial (Enqueue)
```json
{
  "type": "highlight_raw",
  "status": "queued",
  "created_at": "2025-10-14T12:30:00Z",
  "file_name": "highlight_20251014-123000Z.mp4",
  "size_bytes": 5242880,
  "sha256": null,
  "meta": {
    "codec": "h264",
    "width": 1280,
    "height": 720,
    "fps": 30.0,
    "duration_sec": 35.0
  },
  "pre_seconds": 25,
  "post_seconds": 10,
  "seg_time": 1,
  "attempts": 0
}
```

### Após Watermark (Modo Completo)
```json
{
  "status": "watermarked",
  "updated_at": "2025-10-14T12:30:15Z",
  "wm_path": "/path/highlights_wm/highlight_*.mp4",
  "meta_wm": { /* ffprobe do arquivo com watermark */ },
  "attempts": 0
}
```

### Após Registro no Backend
```json
{
  "remote_registration": {
    "status": "registered",
    "registered_at": "2025-10-14T12:30:20Z",
    "response": {
      "clip_id": "abc123xyz",
      "contract_type": "per_video",
      "storage_path": "temp/client-uuid/venue-uuid/abc123xyz.mp4",
      "upload_url": "https://storage.provider.com/signed-url?token=...",
      "expires_hint_hours": 12
    }
  }
}
```

### Após Upload Bem-Sucedido
```json
{
  "remote_upload": {
    "status": "uploaded",
    "http_status": 200,
    "reason": "OK",
    "attempted_at": "2025-10-14T12:31:00Z",
    "duration_ms": 8500,
    "file_size": 5242880
  }
}
```

### Após Finalização
```json
{
  "remote_finalize": {
    "status": "ok",
    "finalized_at": "2025-10-14T12:31:05Z",
    "response": {
      "validated": true,
      "sha256_match": true
    }
  }
}
```

### Falha com Pendência de Upload
```json
{
  "status": "upload_pending",
  "attempts": 1,
  "last_error": "HTTPError: 503 Service Unavailable",
  "local_fallback": {
    "status": "upload_pending",
    "reason": "upload_failed",
    "moved_at": "2025-10-14T12:31:10Z",
    "dest_dir": "/path/failed_clips/upload_failed"
  }
}
```

---

## Modos de Operação

### Light Mode (GN_LIGHT_MODE=1)
- **Sem watermark** e **sem thumbnail**
- Upload direto do vídeo da fila
- Menor uso de CPU/disco
- Ideal para Raspberry Pi 3B/4B com recursos limitados

### Modo Completo (GN_LIGHT_MODE=0)
- Aplica watermark no canto inferior direito
- Pode gerar thumbnail (opcional)
- Reencoda vídeo com H.264 CRF 20
- Maior qualidade visual, porém mais pesado

---

## Configurações Importantes (Variáveis de Ambiente)

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `GN_LIGHT_MODE` | `0` | Ativa modo leve (1=sim, 0=não) |
| `GN_SEG_TIME` | `1` | Duração de cada segmento em segundos |
| `GN_RTSP_URL` | - | URL da câmera RTSP (ex: rtsp://user:pass@ip:554/...) |
| `GN_RTSP_MAX_RETRIES` | `10` | Tentativas de conexão com câmera |
| `GN_RTSP_TIMEOUT` | `5` | Timeout por tentativa (segundos) |
| `GN_RTSP_PRE_SEGMENTS` | `6` | Segmentos pré-clique (modo RTSP) |
| `GN_RTSP_POST_SEGMENTS` | `3` | Segmentos pós-clique (modo RTSP) |
| `GN_GPIO_PIN` | - | Pino BCM para botão físico (ex: 17) |
| `GN_GPIO_COOLDOWN_SEC` | `120` | Cooldown entre disparos GPIO (segundos) |
| `GN_GPIO_DEBOUNCE_MS` | `300` | Debounce do botão (milissegundos) |
| `GN_MAX_ATTEMPTS` | `3` | Tentativas máximas de processamento |
| `API_BASE_URL` | - | URL base do backend |
| `API_TOKEN` | - | Token JWT/Bearer para autenticação |
| `CLIENT_ID` | - | UUID do cliente |
| `VENUE_ID` | - | UUID do venue/local |
| `GN_BUFFER_DIR` | `/dev/shm/grn_buffer` | Diretório do buffer de segmentos |
| `GN_LOG_DIR` | `/usr/src/app/logs` | Diretório de logs do FFmpeg |

---

## Casos de Uso e Cenários

### 1. Captura Normal (Sucesso Completo)
1. FFmpeg captura vídeo continuamente → segmentos em `/dev/shm`
2. SegmentBuffer indexa e remove excedentes a cada 1s
3. Usuário pressiona botão GPIO → cooldown iniciado
4. Sistema aguarda pós-buffer (3 seg no modo RTSP)
5. Concatena 9 segmentos (6 pré + 3 pós) em `.mp4`
6. Enfileira para `queue_raw/` com sidecar JSON
7. Worker processa: **Light Mode** → sem watermark
8. Registra metadados no backend → recebe `upload_url`
9. Calcula SHA256 e faz PUT para storage
10. Notifica backend (finalize) → validação OK
11. Remove artefatos da fila → **ciclo completo**

### 2. Falha de Conectividade (Retry Automático)
1. Raspberry Pi reinicia após queda de energia
2. Docker inicia antes da câmera completar boot
3. `check_rtsp_connectivity` tenta 10x com 5s de intervalo
4. Na 8ª tentativa, câmera responde → FFmpeg inicia
5. Captura continua normalmente

### 3. Falha de Upload (Pendência com Retry)
1. Vídeo processado e registrado no backend
2. Upload falha por timeout de rede (503 Service Unavailable)
3. Worker move para `failed_clips/upload_failed/`
4. JSON atualizado: `status=upload_pending`, `attempts=1`
5. Retry Scanner encontra arquivo após 2 minutos (backoff)
6. Reprocessa: tenta upload novamente
7. Se sucesso → remove de `upload_failed/`
8. Se falha e `attempts >= 3` → permanece em failed definitivo

### 4. Captura Sem Backend (Modo Offline)
1. `API_BASE_URL` não configurada
2. Vídeo capturado e concatenado normalmente
3. Worker pula registro remoto: `status=skipped`
4. Move para `failed_clips/upload_failed/` com reason=`no_api_configured`
5. Vídeo preservado localmente para upload posterior
6. Retry Scanner ignora (sem API não tenta)

---

## Troubleshooting

### Sintoma: "Nenhum segmento capturado — encerrando"

**Causa**: FFmpeg não iniciou ou câmera inacessível

**Verificações**:
```bash
# 1. Logs do sistema
docker logs grava_nois_system

# 2. Logs do FFmpeg
tail -f logs/ffmpeg.log

# 3. Teste manual de conectividade
nc -zv <IP_CAMERA> 554

# 4. Verifica se segmentos estão sendo criados
ls -lh /dev/shm/grn_buffer/
```

**Solução**: Ajustar `GN_RTSP_MAX_RETRIES` ou verificar rede/câmera

---

### Sintoma: Vídeos acumulando em `failed_clips/upload_failed/`

**Causa**: Backend inacessível ou credenciais inválidas

**Verificações**:
```bash
# 1. Testa endpoint do backend
curl -X POST "$API_BASE_URL/api/videos/metadados" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"test": true}'

# 2. Verifica JSON do sidecar
cat failed_clips/upload_failed/highlight_*.json | jq .remote_registration

# 3. Monitora retry scanner
docker logs grava_nois_system | grep "worker:retry"
```

**Solução**: Configurar `API_BASE_URL` e `API_TOKEN` corretos, verificar conectividade

---

### Sintoma: Cooldown impedindo capturas rápidas

**Causa**: `GN_GPIO_COOLDOWN_SEC` muito alto

**Solução**: Ajustar cooldown (padrão 120s):
```bash
# No .env
GN_GPIO_COOLDOWN_SEC=30  # 30 segundos entre capturas
```

---

## Melhorias Futuras (Roadmap)

- [ ] **Logs estruturados**: JSON logging para análise
- [ ] **Métricas**: Integração com Prometheus/Grafana
- [ ] **Watchdog**: Substituir polling por inotify/watchdog
- [ ] **Compressão adaptativa**: Ajustar CRF baseado em duração
- [ ] **Multi-câmera**: Suporte a múltiplas fontes simultâneas
- [ ] **Streaming ao vivo**: HLS/DASH para preview remoto
- [ ] **Retry inteligente**: Backoff exponencial com jitter
- [ ] **Health check HTTP**: Endpoint REST para monitoramento

---

## Referências

- [Documentação FFmpeg Segment](https://ffmpeg.org/ffmpeg-formats.html#segment)
- [pigpio Documentation](http://abyz.me.uk/rpi/pigpio/)
- [Supabase Storage Signed URLs](https://supabase.com/docs/guides/storage/uploads/signed-urls)
- [README Principal](../docs/README.md)
- [Instruções de Instalação](../INSTRUCTIONS.md)
