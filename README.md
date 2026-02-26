# Grava Nóis — Sistema de Captura de Vídeos

> **Objetivo:** Capturar replays com pré/pós-buffer, gerar highlights, processar com marca d'água/thumbnail e fazer upload automático para backend via URL assinada. Otimizado para rodar em Raspberry Pi.
>
> **Regra de operação:** O sistema respeita janela de horário comercial configurável no trigger local e também descarta clipes rejeitados pela API por restrição de horário.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-required-green.svg)](https://ffmpeg.org/)

---

## 📋 Índice

- [Arquitetura do Sistema](#arquitetura-do-sistema)
- [Início Rápido](#início-rápido)
- [Fluxo de Funcionamento](#fluxo-de-funcionamento)
- [Estrutura de Diretórios](#estrutura-de-diretórios)
- [Configuração](#configuração)
- [GPIO (Botão Físico)](#gpio-botão-físico)
- [Modo Leve (Light Mode)](#modo-leve-light-mode)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)

---

## 🏗️ Arquitetura do Sistema

### Arquivos Principais

- **`main.py`**: Serviço principal + worker de processamento
- **`video_core.py`**: Núcleo de captura e manipulação de vídeos
- **`src/utils/logger.py`**: Sistema de logging centralizado
- **`src/services/api_client.py`**: Cliente HTTP para comunicação com backend

### Dependências

- Python 3.10+
- FFmpeg/ffprobe
- pigpio (opcional, para GPIO)
- requests, python-dotenv (em `requirements.txt`)

### Fluxo Simplificado

```
[ Câmera RTSP/V4L2 ]
       ↓
[ FFmpeg - Segmentos de 1s ]
       ↓
[ SegmentBuffer - Mantém buffer circular ]
       ↓
[ ENTER/GPIO → build_highlight() ]
       ↓
[ Enqueue → queue_raw/ ]
       ↓
[ ProcessingWorker ]
   ├─ Watermark (opcional)
   ├─ Thumbnail (opcional)
   └─ Upload via API
       ↓
[ Backend (URL assinada) ]
```

---

## 🚀 Início Rápido

### 1. Instalação no Raspberry Pi

```bash
# Atualizar sistema e instalar dependências
sudo apt update && sudo apt install -y ffmpeg pigpio python3-venv
sudo systemctl enable --now pigpiod

# Clonar repositório (ou transferir arquivos)
cd /home/pi/grava_nois_system

# Criar ambiente virtual
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Configuração Básica

Crie um arquivo `.env` na raiz do projeto:

```bash
# Configuração da câmera RTSP (obrigatório)
GN_RTSP_URL=rtsp://user:pass@192.168.1.100:554/cam/realmonitor?channel=1&subtype=0

# API Backend (opcional, mas recomendado)
GN_API_BASE=https://api.gravanois.com
GN_API_TOKEN=seu_token_jwt_aqui
GN_CLIENT_ID=uuid-do-cliente
GN_VENUE_ID=uuid-do-local
DEVICE_ID=raspberrypi-001
DEVICE_SECRET=troque_por_um_segredo_forte

# GPIO (opcional)
GN_GPIO_PIN=17
GN_GPIO_COOLDOWN_SEC=120

# Janela de funcionamento local (opcional)
GN_TIME_ZONE=America/Sao_Paulo
GN_START_TIME=07:00
GN_END_TIME=23:30

# Modo leve (recomendado para Pi 3B/1GB)
GN_LIGHT_MODE=1

# Modo desenvolvimento (sem chamadas externas)
DEV=true

# Dry-run da assinatura HMAC (sem chamar backend)
GN_HMAC_DRY_RUN=0

# Configurações de buffer
GN_SEG_TIME=1
GN_RTSP_PRE_SEGMENTS=6
GN_RTSP_POST_SEGMENTS=3
```

### 3. Executar

```bash
source .venv/bin/activate
python3 main.py
```

**Gerar highlight:** Pressione `ENTER` no terminal ou o botão físico conectado ao GPIO.

---

## 🔄 Fluxo de Funcionamento

### 1. Captura Contínua

O FFmpeg captura vídeo da fonte (RTSP ou V4L2) e grava segmentos de 1 segundo em `/dev/shm/grn_buffer/`:

```
buffer000000.ts
buffer000001.ts
buffer000002.ts
...
```

### 2. Buffer Circular

O `SegmentBuffer` mantém apenas os últimos ~40 segundos de vídeo, apagando segmentos antigos automaticamente.

### 3. Trigger (ENTER ou GPIO)

Ao pressionar ENTER ou botão físico:

1. Sistema valida se o horário atual está dentro da janela `GN_START_TIME` → `GN_END_TIME` no fuso `GN_TIME_ZONE`
2. Se estiver fora da janela, o trigger é ignorado e o `build_highlight()` não é executado
3. Se estiver dentro da janela, aguarda `post_seconds` (padrão: 3 segmentos = 3s)
4. Seleciona `pre_segments + post_segments` (padrão: 6 + 3 = 9 segmentos)
5. Concatena com `ffmpeg` (sem reencode)
6. Salva em `recorded_clips/highlight_YYYYMMDD-HHMMSSZ.mp4`

### 4. Enfileiramento

O vídeo é movido para `queue_raw/` junto com um arquivo JSON contendo metadados:

```json
{
  "type": "highlight_raw",
  "status": "queued",
  "created_at": "2026-02-13T10:00:00Z",
  "file_name": "highlight_20260213-100000Z.mp4",
  "size_bytes": 1234567,
  "sha256": "abc123...",
  "meta": {
    "codec": "h264",
    "width": 1280,
    "height": 720,
    "fps": 30.0,
    "duration_sec": 9.0
  }
}
```

### 5. Processamento (Worker)

O `ProcessingWorker` varre a fila periodicamente:

**Modo Normal:**
1. Aplica marca d'água (centro)
2. Gera thumbnail (meio do vídeo)
3. Registra metadados no backend → recebe `upload_url`
4. Faz upload para URL assinada (S3/Supabase)
5. Notifica backend sobre conclusão
6. Remove arquivo da fila

**Modo Leve (`GN_LIGHT_MODE=1`):**
1. Registra metadados no backend → recebe `upload_url`
2. Faz upload direto (sem marca d'água)
3. Notifica backend sobre conclusão
4. Remove arquivo da fila

**Modo DEV (`DEV=true`):**
1. Executa processamento local normalmente (watermark no modo normal, ou fluxo leve)
2. Não chama API externa (`register/upload/finalize`)
3. Marca `remote_registration` como `skipped` com motivo `DEV mode`
4. Limpa `queue_raw` (remove vídeo cru + sidecar) sem mover para `failed_clips`
5. Preserva o arquivo final local em `highlights_wm/` quando estiver no modo normal

### 6. Tratamento de Erros

- **Retry automático:** Até 3 tentativas com backoff
- **Pasta de falhas:** Vídeos que falharam vão para `failed_clips/upload_failed/`
- **Reprocessamento:** Sistema tenta reprocessar falhas periodicamente
- **Exceção de horário comercial:** Se a API rejeitar o registro com `HTTP 403` por janela de horário (`request_outside_allowed_time_window`), o worker exclui o vídeo e sidecar local imediatamente (sem retry e sem enviar para `failed_clips`)
- **Erros HMAC/device não-retriáveis:** Quando a API retorna erros de autenticação/integridade do device (ex.: `signature_mismatch`, `client_mismatch`, `device_revoked`), o worker remove o registro local (vídeo + sidecar) para evitar loop infinito de retry.

---

## 📁 Estrutura de Diretórios

```
grava_nois_system/
├── main.py                      # Serviço principal + worker
├── video_core.py                # Funções de captura e processamento
├── requirements.txt             # Dependências Python
├── .env                         # Configuração (não commitado)
│
├── src/
│   ├── utils/
│   │   └── logger.py            # Sistema de logging centralizado
│   ├── security/
│   │   ├── hmac.py              # Hash/HMAC/nonce/timestamp
│   │   └── request_signer.py    # Canonical string + headers HMAC
│   └── services/
│       ├── api_client.py        # Cliente HTTP para backend
│       └── api_error_policy.py  # Regra de decisão para erros da API
│
├── files/
│   └── replay_grava_nois.png    # Logo para marca d'água
│
├── logs/
│   ├── app.log                  # Logs da aplicação (DEBUG)
│   └── ffmpeg.log               # Logs do FFmpeg
│
├── recorded_clips/              # Highlights após concat
├── queue_raw/                   # Fila de processamento
├── highlights_wm/               # Vídeos com marca d'água (modo normal)
├── failed_clips/                # Vídeos que falharam
│   ├── upload_failed/           # Falhas de upload (retry automático)
│   ├── build_failed/            # Falhas na construção
│   └── enqueue_failed/          # Falhas ao enfileirar
│
├── tests/
│   ├── test_security_signing.py # Testes de assinatura HMAC
│   └── test_api_error_policy.py # Testes de política de erros da API
│
└── docs/
    ├── README.md                # Documentação original
    ├── fluxo-funcional.md       # Diagrama detalhado
    └── grava_nois_fluxo.png     # Diagrama visual
```

### Diretórios Criados Automaticamente

O sistema cria os seguintes diretórios se não existirem:
- `/dev/shm/grn_buffer/` (ou `GN_BUFFER_DIR`)
- `recorded_clips/`
- `queue_raw/`
- `highlights_wm/` (apenas em modo normal)
- `failed_clips/`
- `logs/`

---

## ⚙️ Configuração

### Variáveis de Ambiente

Todas as configurações podem ser feitas via variáveis de ambiente ou arquivo `.env`:

#### Câmera RTSP

```bash
# URL completa da câmera (obrigatório)
GN_RTSP_URL=rtsp://user:pass@192.168.1.100:554/cam/realmonitor?channel=1&subtype=0

# Health check (opcional)
GN_RTSP_MAX_RETRIES=10          # Tentativas de conexão (padrão: 10)
GN_RTSP_TIMEOUT=5               # Timeout por tentativa em segundos (padrão: 5)

# Configuração de segmentos RTSP
GN_SEG_TIME=1                   # Duração de cada segmento (padrão: 1s)
GN_RTSP_PRE_SEGMENTS=6          # Segmentos antes do clique (padrão: 6)
GN_RTSP_POST_SEGMENTS=3         # Segmentos depois do clique (padrão: 3)
```

#### Backend API

```bash
GN_API_BASE=https://api.gravanois.com
GN_API_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
GN_CLIENT_ID=550e8400-e29b-41d4-a716-446655440000
GN_VENUE_ID=6ba7b810-9dad-11d1-80b4-00c04fd430c8
CLIENT_ID=550e8400-e29b-41d4-a716-446655440000  # fallback para X-Client-Id
DEVICE_ID=raspberrypi-001
DEVICE_SECRET=troque_por_um_segredo_forte
GN_HMAC_DRY_RUN=0  # 1=nao envia request; apenas monta, assina e loga
```

#### GPIO

```bash
GN_GPIO_PIN=17                  # Pino BCM (padrão: desabilitado)
GN_GPIO_DEBOUNCE_MS=300         # Debounce em ms (padrão: 300)
GN_GPIO_COOLDOWN_SEC=120        # Cooldown entre cliques (padrão: 120s)
```

#### Processamento

```bash
GN_LIGHT_MODE=1                 # 0=normal (watermark), 1=leve (sem watermark)
GN_MAX_ATTEMPTS=3               # Tentativas de processamento (padrão: 3)
GN_BUFFER_DIR=/dev/shm/grn_buffer  # Diretório de buffer (padrão: /dev/shm)
```

#### Modo Desenvolvimento

```bash
DEV=true                        # Pula chamadas de rede no ProcessingWorker
```

Com `DEV=true`:
- O processamento local do vídeo continua ativo.
- O worker não faz requisições HTTP para registro, upload e finalização.
- O item é removido de `queue_raw` como sucesso local (sem `upload_failed`).

#### Janela de Funcionamento

```bash
GN_TIME_ZONE=America/Sao_Paulo  # Fuso para validação de horário local
GN_START_TIME=07:00             # Início da janela (HH:MM)
GN_END_TIME=23:30               # Fim da janela (HH:MM)
```

Observações:
- Se `GN_START_TIME`/`GN_END_TIME` estiverem inválidos, o sistema faz fallback para `07:00` e `23:30`.
- Se `GN_TIME_ZONE` estiver inválido, o sistema faz fallback para `America/Sao_Paulo`.
- A comparação usa apenas hora/minuto.

#### Logging

```bash
GN_LOG_DIR=/caminho/custom/logs # Diretório de logs (fallback: <raiz-do-projeto>/logs)
```

Observações:
- Se `GN_LOG_DIR` não for definido, o sistema cria e usa `logs/` na raiz do projeto (mesma pasta de `main.py`).
- Em falhas `401/403` nas rotas assinadas, o logger registra somente `path`, `timestamp`, `nonce`, `body_sha256` e assinatura truncada.
- `DEVICE_SECRET` nunca é escrito nos logs.
- O fallback padrão não depende de caminho absoluto de container Docker.

### Câmera V4L2 (Local)

Para usar câmera USB local em vez de RTSP, **não defina** `GN_RTSP_URL` e configure:

```bash
GN_INPUT_FRAMERATE=30
GN_VIDEO_SIZE=1280x720
```

O código usará `/dev/video0` automaticamente.

---

## 🔌 GPIO (Botão Físico)

### Requisitos

- Raspberry Pi com pinos GPIO
- Daemon `pigpiod` em execução
- Biblioteca Python `pigpio` instalada

### Instalação

```bash
sudo apt install -y pigpio
sudo systemctl enable --now pigpiod
```

### Fiação

- **Pino BCM 17** → Um lado do botão
- **GND** → Outro lado do botão
- Pull-up interno habilitado automaticamente

### Configuração

```bash
export GN_GPIO_PIN=17
export GN_GPIO_COOLDOWN_SEC=120  # Ignora cliques por 2min após disparo
python3 main.py
```

### Comportamento

- **Borda detectada:** FALLING (pressionar)
- **Debounce:** 300ms (configurável)
- **Cooldown:** 120s entre disparos válidos (evita cliques acidentais)
- **Fallback:** Se GPIO não disponível, funciona apenas com ENTER

---

## 🪶 Modo Leve (Light Mode)

**Recomendado para Raspberry Pi 3B ou inferior.**

### Ativação

```bash
export GN_LIGHT_MODE=1
python3 main.py
```

### Diferenças

| Recurso | Modo Normal | Modo Leve |
|---------|-------------|-----------|
| Marca d'água | ✅ Sim | ❌ Não |
| Thumbnail | ✅ Sim | ❌ Não |
| Reencode | ✅ H.264 CRF 20 | ❌ Copy direto |
| Cálculo SHA-256 | 2x (fila + upload) | 1x (upload) |
| CPU | ~80% por vídeo | ~15% por vídeo |
| Tempo/vídeo | ~30s | ~5s |
| Pasta de saída | `highlights_wm/` | `queue_raw/` → upload direto |

### Quando Usar

- ✅ Hardware limitado (Pi 3B, 1GB RAM)
- ✅ Múltiplos cliques em sequência
- ✅ Marca d'água será aplicada pelo backend
- ❌ Precisa de marca d'água local imediata

---

## 🔧 Troubleshooting

### 1. Câmera RTSP Não Conecta

**Sintomas:**
- "Câmera não acessível após 10 tentativas"
- "Nenhum segmento capturado — encerrando"

**Diagnóstico:**

```bash
# Verificar conectividade TCP
nc -zv 192.168.1.100 554

# Testar RTSP diretamente
ffplay rtsp://user:pass@192.168.1.100:554/cam/realmonitor

# Ver logs do FFmpeg
tail -f logs/ffmpeg.log

# Ver logs da aplicação
tail -f logs/app.log
```

**Soluções:**

```bash
# 1. Aumentar tentativas/timeout
export GN_RTSP_MAX_RETRIES=20
export GN_RTSP_TIMEOUT=10

# 2. Verificar URL
export GN_RTSP_URL=rtsp://admin:senha@192.168.1.100:554/cam/realmonitor

# 3. Verificar firewall
sudo ufw allow 554/tcp
```

### 2. GPIO Não Funciona

**Sintomas:**
- "pigpiod não está acessível"
- Botão não dispara

**Soluções:**

```bash
# Iniciar daemon
sudo systemctl start pigpiod
sudo systemctl status pigpiod

# Verificar pino
gpio readall  # Lista todos os pinos

# Testar manualmente
pigs r 17  # Lê estado do pino 17
```

### 3. Disco Cheio

**Sintomas:**
- "No space left on device"
- Sistema travando

**Soluções:**

```bash
# Limpar buffer manualmente
rm -f /dev/shm/grn_buffer/*.ts

# Limpar vídeos processados
rm -f queue_raw/*.mp4
rm -f highlights_wm/*.mp4

# Ajustar buffer máximo
export GN_BUFFER_DIR=/home/pi/buffer  # Usar disco em vez de RAM
```

### 4. Upload Falhando

**Sintomas:**
- Vídeos acumulando em `failed_clips/upload_failed/`
- "Erro de rede ao POST"

**Diagnóstico:**

```bash
# Verificar logs
grep "upload falhou" logs/app.log

# Verificar conectividade
curl -I https://api.gravanois.com

# Testar token
curl -H "Authorization: Bearer $GN_API_TOKEN" https://api.gravanois.com/health
```

**Soluções:**

```bash
# Reprocessamento automático ativado por padrão
# Vídeos em failed_clips/upload_failed/ são retentados a cada 2 minutos

# Forçar reprocessamento manual (mover de volta para fila)
mv failed_clips/upload_failed/*.mp4 queue_raw/
mv failed_clips/upload_failed/*.json queue_raw/
```

### 5. Upload Rejeitado por Horário Comercial (HTTP 403)

**Sintomas:**
- Log com `Upload rejeitado por horário. Arquivo será excluído: ...`
- O arquivo não aparece em `failed_clips/upload_failed/`

**Comportamento esperado:**
- O worker entende essa resposta como rejeição de regra de negócio (não erro transitório).
- O vídeo e o sidecar JSON são removidos localmente.
- Não entra em `max_attempts` e não passa por retry.

### 6. Erros HMAC/Autenticação do Device

Quando o backend retorna erro no formato:

```json
{
  "success": false,
  "data": null,
  "message": "signature_mismatch",
  "error": { "code": "UNAUTHORIZED", "details": null },
  "requestId": "..."
}
```

o device aplica uma política local (em `src/services/api_error_policy.py`) para decidir exclusão imediata ou retry.

**Apaga registro local (vídeo + sidecar):**
- `missing_headers`
- `invalid_timestamp`
- `invalid_nonce`
- `invalid_body_hash`
- `invalid_signature_format`
- `device_not_found`
- `device_revoked`
- `client_mismatch`
- `missing_raw_body`
- `integrity_failed`
- `signature_mismatch`
- `device_not_authenticated`
- `Forbidden - video does not belong to device client`

**Mantém em retry/backoff:**
- `timestamp_out_of_range` (normalmente resolvido com timestamp novo/sincronismo de relógio)
- `replay_detected` (nonce já usado; próximo envio gera nonce novo)
- `replay_store_unavailable`
- `device_hmac_verification_failed`
- outros erros transitórios de rede/infra

### 7. CPU/Memória Alta

**Soluções:**

```bash
# 1. Ativar modo leve
export GN_LIGHT_MODE=1

# 2. Reduzir resolução da câmera
# Configurar na câmera para 720p em vez de 1080p

# 3. Usar buffer em disco
export GN_BUFFER_DIR=/home/pi/buffer

# 4. Aumentar intervalo de varredura do worker
# (Editar main.py: scan_interval=3 em vez de 1)
```

### 8. Modo DEV Não Está Isolando Rede

**Sintomas:**
- Mesmo com `.env` configurado, ainda aparecem logs de chamada HTTP.

**Checklist:**

```bash
# O valor deve ser true/1/yes (case-insensitive)
grep '^DEV=' .env

# Exemplo válido
DEV=true
```

**Observação:**
- Em `DEV=true`, o worker deve logar:
  `Modo DEV ativado. Pulando comunicação com a API e upload para a nuvem.`

### 9. Logs Muito Grandes

```bash
# Logs rotativos estão ativados por padrão
# Máximo: 10MB por arquivo, 5 backups = 50MB total

# Limpar manualmente se necessário
rm logs/app.log.*
> logs/app.log
```

---

## 📚 Sistema de Logging

### Níveis de Log

- **Console:** INFO e acima (mensagens importantes)
- **Arquivo `logs/app.log`:** DEBUG e acima (tudo)
- **Arquivo `logs/ffmpeg.log`:** saída consolidada do FFmpeg (`stdout` + `stderr`)

### Formato

```
[2026-02-13 10:35:05] [INFO    ] [grava_nois:setup_logger:72] Logger configurado
[2026-02-13 10:35:10] [WARNING ] [grava_nois:_process_one:355] API não configurada
[2026-02-13 10:35:15] [ERROR   ] [grava_nois:upload:401] Upload falhou: timeout
```

### Uso no Código

```python
from src.utils.logger import logger

logger.info("Informação geral")
logger.warning("Alerta")
logger.error("Erro")
logger.exception("Erro com stack trace completo")
```

---

## 🌐 Cliente de API

### Uso

```python
from src.services.api_client import GravaNoisAPIClient

# Instanciar (lê configurações do .env)
api_client = GravaNoisAPIClient()

# Verificar se está configurado
if not api_client.is_configured():
    logger.warning("API não configurada")
    return

# Registrar metadados
response = api_client.register_clip_metadados({
    "venue_id": api_client.venue_id,
    "duration_sec": 9.0,
    "captured_at": "2026-02-13T10:00:00Z",
    "meta": {...},
    "sha256": "abc123..."
})

# Upload para URL assinada
status_code, reason, headers = api_client.upload_file_to_signed_url(
    upload_url=response["upload_url"],
    file_path=Path("video.mp4")
)

# Finalizar upload
api_client.finalize_clip_uploaded(
    clip_id=response["clip_id"],
    size_bytes=1234567,
    sha256="abc123...",
    etag=headers.get("etag")
)
```

### Endpoints

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| POST | `/api/videos/metadados/client/{id}/venue/{id}` | Registra metadados |
| PUT | `{upload_url}` | Upload para storage (S3/Supabase) |
| POST | `/api/videos/{clip_id}/uploaded` | Confirma upload |

---

## 🗺️ Roadmap

### Implementado ✅

- ✅ Captura contínua com buffer circular
- ✅ Highlights sob demanda (ENTER/GPIO)
- ✅ Worker de processamento com retry
- ✅ Marca d'água e thumbnail (modo normal)
- ✅ Upload via URL assinada
- ✅ Health check RTSP com retry automático
- ✅ Modo leve para hardware limitado
- ✅ Sistema de logging estruturado
- ✅ Cliente de API centralizado
- ✅ Reprocessamento automático de falhas

### Próximos Passos 🚧

- [ ] Logs estruturados em JSON
- [ ] Watchdog com inotify (substituir varredura)
- [ ] Métricas Prometheus/Grafana
- [ ] Testes unitários e de integração
- [ ] Dashboard web para monitoramento
- [ ] Compressão de vídeos antigos
- [ ] Upload paralelo (múltiplos vídeos)
- [ ] Detecção de movimento (trigger automático)

---

## 📖 Documentação Adicional

- **[Fluxo Funcional Detalhado](docs/fluxo-funcional.md)** — Diagrama completo do sistema
- **[Documentação Original](docs/README.md)** — Referência técnica completa
- **[Resumo da Refatoração](REFACTORING_SUMMARY.md)** — Mudanças recentes na arquitetura

---

## 📝 Licença

MVP interno do projeto **Grava Nóis**. Uso restrito ao time até formalização de licença.

---

## 🤝 Contribuindo

Para contribuir com o projeto:

1. Leia a documentação completa em `docs/`
2. Teste em ambiente local antes de deploy
3. Siga as convenções de logging estabelecidas
4. Atualize a documentação se necessário

---

## 📞 Suporte

Em caso de problemas:

1. Verifique os logs em `logs/app.log` e `logs/ffmpeg.log`
2. Consulte a seção [Troubleshooting](#troubleshooting)
3. Entre em contato com a equipe de desenvolvimento

---

**Última atualização:** 2026-02-13
**Versão:** 2.3.1 (fallback dinâmico de logs na raiz do projeto)
