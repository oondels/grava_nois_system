# Suporte a Múltiplas Câmeras RTSP (N Câmeras)

## 1. Visão Geral

Este documento define os requisitos de produto para a refatoração do sistema de captura e geração de highlights para suportar **N câmeras RTSP simultaneamente**, mantendo compatibilidade retroativa com o modo single-camera.

O sistema atual está acoplado a uma única câmera (`GN_RTSP_URL`). O objetivo desta iniciativa é transformar a aplicação em um **supervisor multi-camera**, com isolamento total por câmera, concorrência segura no trigger e organização de artefatos sem colisões.

---

## 2. Objetivos

### 2.1 Objetivo Principal

Permitir que o sistema:

- Capture simultaneamente múltiplas câmeras RTSP
- Gere highlights para todas as câmeras a partir de um único trigger
- Isole buffers, arquivos e falhas por câmera
- Mantenha estabilidade e previsibilidade operacional

### 2.2 Objetivos Secundários

- Manter compatibilidade com `GN_RTSP_URL` (modo legado)
- Permitir migração gradual para `GN_CAMERAS_JSON`
- Garantir ausência de colisões de arquivos
- Melhorar observabilidade por câmera
- Preparar arquitetura para extensões futuras (ex.: v4l2)

---

## 3. Fora de Escopo

- Interface gráfica de gestão de câmeras
- Configuração dinâmica em runtime
- Orquestração distribuída entre múltiplos containers
- Persistência de métricas em banco externo

---

## 4. Stakeholders

- Engenharia Backend
- Operações (DevOps)
- Responsável por hardware (GPIO/trigger físico)
- Time de Upload/Processamento

---

## 5. Problema Atual

O sistema atual:

- Está rigidamente acoplado a 1 única câmera
- Usa diretórios globais compartilhados
- Possui risco de colisão de arquivos por timestamp em segundos
- Executa build e enqueue de forma serial
- Usa variável global `GN_RTSP_URL`

Isso impede:

- Escalabilidade horizontal por câmera
- Instalações com múltiplas quadras
- Isolamento seguro de falhas

---

## 6. Proposta de Solução

### 6.1 Novo Modelo de Configuração

#### Fonte Primária (Recomendada)

`GN_CAMERAS_JSON`

```json
[
  {
    "id": "cam01",
    "name": "quadra_norte",
    "rtsp_url": "rtsp://user:pass@192.168.1.101:554/stream1",
    "enabled": true
  }
]

### Fallbacks

1. `GN_RTSP_URLS` (CSV)
2. `GN_RTSP_URL` (modo legado single-camera)

### 6.2 Identidade Obrigatória de Câmera

Cada câmera deve possuir:

- `camera_id` (único e obrigatório)
- `camera_name` (opcional)
- `rtsp_url`
- `source_type`
- Configurações específicas opcionais (pre/post/seg_time)

---

## 7. Requisitos Funcionais

### RF1 – Suporte a N câmeras simultâneas

O sistema deve:

- Inicializar N instâncias independentes de:
    - FFmpeg
    - SegmentBuffer
    - Runtime state

### RF2 – Trigger Fan-out

Ao receber um trigger (GPIO/ENTER):

- Gerar `trigger_id` único
- Disparar concorrente para todas as câmeras ativas
- Executar:
    - `build_highlight`
    - `enqueue_clip`

### RF3 – Lock por câmera

Cada câmera deve possuir:

- `capture_lock`
- Apenas 1 highlight por câmera simultaneamente
- Outras câmeras não devem ser bloqueadas

### RF4 – Isolamento completo de diretórios

Estrutura obrigatória:
/dev/shm/grn_buffer/<camera_id>/
queue_raw/<camera_id>/
recorded_clips/<camera_id>/
failed_clips/<camera_id>/
highlights_wm/<camera_id>/
logs/ffmpeg_<camera_id>.log

Nenhum artefato temporário pode ser compartilhado entre câmeras.

### RF5 – Nomes de arquivos únicos

Os arquivos devem conter:

- `camera_id`
- timestamp de alta resolução ou UUID
- `trigger_id` quando aplicável

### RF6 – Observabilidade por câmera

Logs devem incluir:

- `camera_id`
- `trigger_id`
- status (success/failure/busy)

---

## 8. Requisitos Não Funcionais

### RNF1 – Backward Compatibility

Se nenhuma config multi-camera for definida:

- Sistema opera como single-camera
- Nenhuma breaking change externa

### RNF2 – Estabilidade

- Falha de uma câmera não deve derrubar todas
- Política configurável: fail-fast ou degradação parcial

### RNF3 – Concorrência Controlada

- Uso de `ThreadPoolExecutor`
- Limite via `GN_TRIGGER_MAX_WORKERS`
- Evitar explosão de threads

### RNF4 – Sem Deadlocks

- Sem lock global
- Lock apenas por câmera
- Sem joins bloqueantes indefinidos

---

## 9. Arquitetura Proposta

### 9.1 Estrutura de Runtime

Para cada câmera:
CameraRuntime:
  - cfg: CaptureConfig
  - ffmpeg_proc
  - segbuf
  - capture_lock
  - state

No main.py:
dict[camera_id, CameraRuntime]

### 9.2 Supervisor

Responsável por:

- Bootstrap das câmeras
- Monitoramento
- Shutdown coordenado
- Dispatch de triggers

---

## 10. Impacto Esperado no Código

Arquivos com maior impacto:

- `main.py` (maior refatoração)
- `src/config/settings.py`
- `src/video/capture.py`
- `src/video/processor.py`
- `src/workers/processing_worker.py`

Mudanças estruturais:

- Separação de configuração global para multi-camera
- Isolamento de diretórios
- Inclusão de metadados `camera_id`
- Revisão do worker

---

## 11. Plano de Entrega (Fases)

### Fase A – Configuração

- Parser multi-camera
- Evolução de `CaptureConfig`
- Compatibilidade legado

### Fase B – Runtime Multi-camera

- Supervisor
- Start/stop por câmera

### Fase C – Trigger Concorrente

- Pool de execução
- Lock por câmera
- `trigger_id`

### Fase D – Isolamento de Artefatos

- Refatoração de paths
- Sidecar com `camera_id`

### Fase E – Documentação e Testes

- `.env.example`
- Docker healthcheck
- Testes unitários e integração

---

## 12. Critérios de Sucesso

O projeto será considerado concluído quando:

- [x]  Sistema suporta ≥ 2 câmeras simultaneamente
- [x]  1 trigger gera highlights para todas as câmeras
- [ ]  Nenhuma colisão de arquivos ocorre sob concorrência
- [ ]  Falha de uma câmera não afeta as demais
- [ ]  Compatibilidade legado validada
- [ ]  Testes automatizados cobrindo:
    - Parser
    - Fan-out
    - Isolamento de diretórios
    - Worker multi-camera

---

## 13. Riscos

| Risco | Mitigação |
| --- | --- |
| Deadlock entre câmeras | Lock apenas por câmera |
| Explosão de threads | Limite configurável |
| Colisão de arquivos | Timestamp de alta resolução + UUID |
| Falha silenciosa de FFmpeg | Log por câmera + healthcheck |
| Migração quebrar legado | Fallback e testes de regressão |

---

## 14. Decisões Arquiteturais para Aprovação

- ✅ `GN_CAMERAS_JSON` como padrão oficial
- ✅ Fallback para `GN_RTSP_URL`
- ✅ Isolamento obrigatório por diretório
- ✅ Trigger fan-out concorrente com lock por câmera
- ✅ Supervisor central multi-runtime

---

## 15. Métricas de Validação

- Tempo médio de build por câmera
- Tempo total de fan-out
- Taxa de falhas por câmera
- Número máximo estável de câmeras simultâneas
- Uso de CPU/RAM por câmera

---

## 16. Estado Atual da Aprovação

**Status:** Aguardando validação de engenharia para iniciar Fase A.

Fim do PRD.
