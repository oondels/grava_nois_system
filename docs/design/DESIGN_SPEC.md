# DESIGN SPEC — Grava Nóis Edge System (Python)

> Este documento descreve exclusivamente o **software de captura (edge)** em Python.
> Não inclui responsabilidades do Backend API, Frontend Web ou infraestrutura de nuvem.

---

# 1. Objetivo do Sistema Edge

O sistema Python é responsável por:

* Captura contínua de vídeo (RTSP ou V4L2)
* Manutenção de buffer circular de segmentos
* Geração de highlight sob trigger (ENTER, GPIO ou Pico serial)
* Persistência local do clip e sidecar JSON
* Gerenciamento de fila local
* Upload para URL assinada fornecida pelo backend
* Finalização remota do upload
* Reprocessamento e política de retry

O edge **não implementa regras de negócio do backend**, não gerencia usuários web e não controla billing.

---

# 2. Arquitetura Interna

## 2.1 Componentes principais

### main.py

* Bootstrap do sistema
* Carregamento de configurações (.env)
* Inicialização das câmeras
* Inicialização dos workers
* Orquestração de triggers

### src/config/settings.py

* Carregamento e validação de configurações
* Construção de `CaptureConfig`

### src/video/capture.py

* Execução do FFmpeg
* Segmentação contínua
* Gerenciamento do processo de captura

### src/video/buffer.py

* Implementação de buffer circular
* Remoção automática de segmentos antigos

### src/video/processor.py

* Construção de highlight a partir do buffer
* Concatenação/remux
* Geração de hash SHA256
* Criação de sidecar JSON

### src/workers/processing_worker.py

* Consumo da fila local
* Registro remoto de metadados
* Upload via URL assinada
* Finalização remota
* Política de retry e descarte

### src/services/api_client.py

* Cliente HTTP para backend
* Assinatura HMAC (quando necessário)
* Upload para signed URL

### src/security/*

* Implementação de HMAC
* Construção de headers assinados

---

# 3. Pipeline Operacional

## 3.1 Captura Contínua

* FFmpeg grava segmentos `.ts`
* Segmentos armazenados em diretório de buffer
* Buffer mantém apenas janela configurada

## 3.2 Trigger

* Pode ser:

  * ENTER (CLI)
  * GPIO (botão físico)
  * Pico serial (token textual via USB)
* Seleção de origem física por `GN_TRIGGER_SOURCE`:

  * `auto` (padrão): Raspberry Pi usa GPIO; outros hosts usam Pico serial
  * `gpio`: força apenas GPIO
  * `pico`: força apenas Pico serial
  * `both`: habilita GPIO e Pico serial simultaneamente
* Se `GN_TRIGGER_SOURCE=gpio` estiver ativo sem `GN_GPIO_PIN`, o sistema tenta fallback para Pico serial
* Pico serial só é habilitado com porta válida (`GN_PICO_PORT` existente ou auto-detecção em `/dev/serial/by-id/*`, `/dev/ttyACM*`, `/dev/ttyUSB*`)
* Não há fallback forçado para `/dev/ttyACM0` quando nenhuma porta existe
* Trigger dispara fan-out por câmera
* Lock por câmera evita sobreposição de processamento

## 3.3 Construção do Highlight

* Seleciona segmentos pré e pós-evento
* Concatena/remuxa para MP4
* Calcula SHA256
* Cria sidecar JSON com metadados locais
* Move para fila (`queue_raw`)

## 3.4 Worker de Processamento

Para cada item da fila:

1. Registrar metadados no backend
2. Receber `upload_url`
3. Executar PUT para storage
4. Finalizar upload
5. Atualizar sidecar
6. Limpar arquivos locais se sucesso

---

# 4. Modelo de Dados Local

## 4.1 CaptureConfig

Campos principais:

* camera_id
* rtsp_url
* seg_time
* pre_segments
* post_segments
* buffer_dir
* queue_dir
* failed_dir

## 4.2 Sidecar JSON

Campos típicos:

* file_name
* created_at
* sha256
* size_bytes
* meta
* status
* attempts
* remote_registration
* remote_upload
* remote_finalize

---

# 5. Confiabilidade

## 5.1 Fila baseada em filesystem

* Diretório `queue_raw`
* Lock por arquivo (`.lock`)
* Retry limitado por `GN_MAX_ATTEMPTS`
* Scanner periódico de falhas

## 5.2 Idempotência

* Verificação de estados antes de repetir upload ou finalize
* Evita duplicidade de processamento

## 5.3 Política de Falha

* Erros temporários → retry
* Erros não-retriáveis → mover para failed ou remover

---

# 6. Segurança no Edge

## 6.1 Assinatura HMAC

Headers enviados quando exigido:

* X-Device-Id
* X-Client-Id
* X-Timestamp
* X-Nonce
* X-Body-SHA256
* X-Signature

## 6.2 Proteção Local

* Lock de processamento
* Controle de janela operacional (horário permitido)
* Cooldown para trigger físico (GPIO e Pico serial)

---

# 7. Configuração

Principais variáveis:

### Captura

* GN_RTSP_URL
* GN_CAMERAS_JSON
* GN_SEG_TIME
* GN_RTSP_PRE_SEGMENTS
* GN_RTSP_POST_SEGMENTS
* GN_BUFFER_DIR

### API

* GN_API_BASE
* GN_CLIENT_ID
* GN_VENUE_ID
* GN_API_TOKEN
* DEVICE_ID
* DEVICE_SECRET

### Execução

* GN_LIGHT_MODE
* GN_MAX_ATTEMPTS
* DEV

### GPIO

* GN_GPIO_PIN
* GN_GPIO_DEBOUNCE_MS
* GN_GPIO_COOLDOWN_SEC

### Trigger físico / Pico

* GN_TRIGGER_SOURCE (`auto`, `gpio`, `pico`, `both`)
* GN_FORCE_RASPBERRY_PI (`1` ou `0`, para testes/override de plataforma)
* GN_PICO_PORT
* GN_PICO_TRIGGER_TOKEN

---

# 8. Deploy

## 8.1 Requisitos

* Python 3.10+
* FFmpeg / ffprobe
* requests
* python-dotenv
* pigpio (opcional)

## 8.2 Docker

* Base: python:3.11-slim
* Execução: `python main.py`
* Volume para buffer, fila e logs

---

# 9. Limitações Atuais

* Fila baseada em polling (não event-driven)
* Dependência de filesystem local
* Observabilidade baseada em logs
* Dependência de conectividade para upload remoto

---

# 10. Não é responsabilidade deste sistema

* Autenticação de usuários web
* Gestão de contratos
* Billing
* Banco de dados
* Frontend
* Emissão de URLs assinadas (apenas consumo)
* Mensageria interna do backend

---

# 11. Diretrizes Futuras

* Melhorar métricas locais (latência, taxa de erro)
* Migrar polling para watcher baseado em eventos
* Versionar schema do sidecar
* Padronizar logs estruturados
