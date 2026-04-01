# Explicação do Projeto `grava_nois_system`

## Visão Geral

O `grava_nois_system` é um serviço em Python para captura contínua de vídeo (RTSP ou câmera local), geração de highlights sob demanda e envio desses clipes para o backend do ecossistema Grava Nóis.

O sistema foi pensado para rodar em Raspberry Pi e ambientes Linux com baixo custo operacional, usando `ffmpeg` para processamento de mídia e uma fila local para tolerância a falhas.

---

## Objetivo do Sistema

O projeto resolve o fluxo de borda (edge) de gravação:

1. Captura vídeo continuamente em segmentos curtos.
2. Quando recebe um trigger (ENTER ou botão GPIO), monta um highlight com pré e pós-buffer.
3. Enfileira o arquivo para processamento assíncrono.
4. Processa e tenta enviar o vídeo para o backend via URL assinada.
5. Finaliza o upload no backend com validação de integridade.

---

## Tecnologias Utilizadas

- Linguagem: `Python 3.10+` (Docker usa `python:3.11-slim`).
- Mídia: `ffmpeg` e `ffprobe`.
- HTTP: `requests`.
- Configuração: `.env` com `python-dotenv`.
- GPIO (opcional): `pigpio` para botão físico no Raspberry Pi.
- Containerização: `Docker` + `docker-compose`.
- Testes: suíte em `tests/` (ex.: segurança de assinatura, multi-câmera, políticas de erro).

Dependências Python diretas em `requirements.txt`:
- `pigpio==1.78`
- `python-dotenv==1.1.1`
- `requests==2.31.0`

---

## Arquitetura do Projeto

### Entrypoint

- `main.py`: inicializa câmeras, processos de captura, buffer em memória/disco, workers e gatilhos de disparo.

### Camadas principais (`src/`)

- `src/config/settings.py`
  - Carrega configurações de captura.
  - Suporta câmera única e múltiplas câmeras (`GN_CAMERAS_JSON`, `GN_RTSP_URLS`, `GN_RTSP_URL`).

- `src/video/capture.py`
  - Verifica conectividade RTSP antes de iniciar o `ffmpeg`.
  - Inicia processo de segmentação contínua de vídeo.

- `src/video/buffer.py`
  - Mantém um buffer circular com os segmentos mais recentes.
  - Remove segmentos antigos automaticamente.

- `src/video/processor.py`
  - Monta highlight por concatenação de segmentos.
  - Extrai metadados (`ffprobe`), gera hash (`sha256`) e enfileira o clipe.
  - Em modo completo, aplica marca d’água e pode gerar thumbnail.

- `src/workers/processing_worker.py`
  - Processa fila `queue_raw/` em background.
  - Registra metadados no backend, faz upload para URL assinada e finaliza upload.
  - Implementa lock por arquivo (`.lock`), retentativas e movimentação para falhas.

- `src/services/api_client.py`
  - Cliente HTTP central do backend.
  - Chama endpoints de registro e finalização.
  - Realiza upload via `PUT` em URL assinada.
  - Suporte a assinatura HMAC em endpoints sensíveis.

- `src/security/hmac.py` e `src/security/request_signer.py`
  - Geração de hash/body digest, nonce, timestamp e assinatura HMAC.

- `src/services/api_error_policy.py`
  - Classifica erros da API para decidir se deve reter ou excluir registro local.

- `src/utils/logger.py` e `src/utils/time_utils.py`
  - Logging centralizado com arquivo rotativo.
  - Validação de janela de horário comercial para aceitar/ignorar triggers.

---

## Fluxo de Funcionamento

1. O `ffmpeg` grava segmentos de 1 segundo no buffer.
2. Um trigger (ENTER/GPIO) dispara captura de highlight.
3. O sistema aguarda pós-buffer e concatena segmentos pré+pós em `.mp4`.
4. O clipe é movido para `queue_raw/` com sidecar JSON (`.json`).
5. O worker processa a fila:
   - **Modo normal**: watermark + upload.
   - **Modo leve (`GN_LIGHT_MODE=1`)**: upload direto (sem watermark/thumbnail).
6. Backend retorna `upload_url` assinada.
7. Upload `PUT` para storage.
8. API recebe confirmação final (`/uploaded`) com `size_bytes`, `sha256` e opcionalmente `etag`.
9. Em sucesso, limpa artefatos locais; em falha, move para `failed_clips/` e agenda retry.

---

## Modos de Operação

- **Produção (padrão):** fluxo completo com comunicação remota.
- **Light mode (`GN_LIGHT_MODE=1`):** reduz custo de CPU, sem watermark/thumbnail.
- **DEV (`DEV=true`):** não chama API remota; preserva processamento local para testes de pipeline.

---

## Diretórios Operacionais

- `recorded_clips/`: highlights recém-construídos.
- `queue_raw/`: fila de processamento.
- `highlights_wm/`: saída com watermark (modo normal).
- `failed_clips/`: falhas e pendências (`upload_failed`, `build_failed`, etc.).
- `logs/`: logs da aplicação e `ffmpeg`.
- Buffer de segmentos: padrão em `/dev/shm/grn_buffer` (ou `GN_BUFFER_DIR`).

---

## Configurações Importantes (.env)

- Captura: `GN_RTSP_URL`, `GN_SEG_TIME`, `GN_RTSP_PRE_SEGMENTS`, `GN_RTSP_POST_SEGMENTS`.
- API: `GN_API_BASE`, `GN_API_TOKEN`, `GN_CLIENT_ID`, `GN_VENUE_ID`.
- Segurança device: `DEVICE_ID`, `DEVICE_SECRET`, `GN_HMAC_DRY_RUN`.
- Execução: `GN_LIGHT_MODE`, `GN_MAX_ATTEMPTS`, `DEV`.
- GPIO: `GN_GPIO_PIN`, `GN_GPIO_DEBOUNCE_MS`, `GN_GPIO_COOLDOWN_SEC`.
- Janela de funcionamento: `GN_TIME_ZONE`, `GN_START_TIME`, `GN_END_TIME`.
- Logs: `GN_LOG_DIR`.

---

## Confiabilidade e Segurança Implementadas

- Buffer circular para evitar crescimento infinito de armazenamento.
- Retry com limite de tentativas para falhas de upload/processamento.
- Lock por arquivo para evitar processamento simultâneo do mesmo job.
- Assinatura HMAC em endpoints protegidos de metadados/finalização.
- Validação de horário de operação local e tratamento de rejeições de janela no backend.
- Política de descarte local para erros não-retriáveis de autenticação/integridade.

---

## Como Rodar (Resumo)

1. Configurar `.env`.
2. Garantir `ffmpeg` instalado.
3. Instalar dependências Python (`pip install -r requirements.txt`) ou subir com Docker.
4. Executar:

```bash
python3 main.py
```

Ou com Docker:

```bash
docker compose up -d
```

Trigger manual: pressionar `ENTER` no terminal.

---

## Resumo Final

O `grava_nois_system` é o componente de borda responsável por transformar stream contínuo de câmera em highlights com pipeline robusto de fila + upload assinado + finalização validada, com foco em operação estável em hardware limitado (como Raspberry Pi) e integração segura com o backend Grava Nóis.

