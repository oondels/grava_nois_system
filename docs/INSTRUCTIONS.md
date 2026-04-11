# Instruções de Instalação e Execução — Grava Nóis System

Este documento explica como preparar e rodar o sistema **Grava Nóis** em um equipamento novo (Raspberry Pi ou servidor Linux).

---

## 0. Instalar dependências do sistema

No host (fora do Docker):

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose ffmpeg pigpio
```

---

## 1. Criar arquivo `.env`

Na raiz do projeto, crie um arquivo `.env` com as variáveis abaixo:

```ini
API_BASE_URL=
CLIENT_ID=
VENUE_ID=
GN_LIGHT_MODE=1
GN_INPUT_FRAMERATE=30
GN_SEG_TIME=1
GN_VIDEO_SIZE=1280x720
GPIO_PIN=17
GN_GPIO_COOLDOWN_SEC=120
GN_GPIO_DEBOUNCE_MS=300

# Configuração da câmera RTSP
GN_RTSP_URL=rtsp://user:pass@ip:554/cam/realmonitor?channel=1&subtype=0

# Health check RTSP: configurações de retry e timeout
GN_RTSP_MAX_RETRIES=10  # Número de tentativas de conexão (10 x 5s = até 50s de espera)
GN_RTSP_TIMEOUT=5       # Timeout por tentativa em segundos

# Diretório para logs do FFmpeg (facilita debug de problemas de conexão)
GN_LOG_DIR=/usr/src/app/logs

#TERM=xterm-256color
#API_TOKEN=
```

> **Atenção**: Ajuste `GN_RTSP_URL` para a URL da sua câmera IP ou use `/dev/video0` para webcam local.

### Novas Variáveis de Ambiente (Health Check RTSP)

O sistema agora possui um mecanismo de retry automático para evitar falhas quando o Raspberry Pi reinicia mais rápido que a câmera IP:

- **`GN_RTSP_MAX_RETRIES`**: Número de tentativas de conexão com a câmera (padrão: 10)
  - Cada tentativa aguarda 5s, totalizando até 50s de espera
  - Ajuste para mais se sua câmera demora muito para inicializar

- **`GN_RTSP_TIMEOUT`**: Timeout por tentativa em segundos (padrão: 5)
  - Tempo máximo para estabelecer conexão TCP com a câmera

- **`GN_LOG_DIR`**: Diretório para logs do FFmpeg (padrão: `/usr/src/app/logs`)
  - Logs detalhados facilitam debug de problemas de conexão
  - Verifique `logs/ffmpeg_<camera_id>.log` para diagnóstico

---

## 2. Ativar serviço `pigpiod` (para GPIO físico no Raspberry Pi)

```bash
sudo apt install -y pigpio
sudo systemctl enable --now pigpiod
```

> Se não for usar botão físico, pode ignorar esta etapa.

---

## 3. Comandos de verificação para Docker

Verifique se o serviço do Docker já está habilitado:

```bash
sudo systemctl is-enabled docker
```

Se a resposta for `disabled`, habilite-o:

```bash
sudo systemctl enable docker
```

---

## 4. Inicializar o container Docker

Na raiz do projeto:

```bash
docker compose up -d
```

O container `grava_nois_system` será iniciado em segundo plano. Ele:

* Abre captura da câmera via `ffmpeg`
* Mantém buffer circular de segmentos (`.ts`)
* Aguarda **ENTER** no teclado ou disparo via **GPIO**
* Constrói clipes (`.mp4`) e os envia ao backend via **URL assinada**

---

## 5. Estrutura de diretórios

* `recorded_clips/` — clipes recém‑gerados
* `queue_raw/` — clipes aguardando upload
* `failed_clips/` — clipes com falha
* `files/` — arquivos auxiliares (ex: watermark)

---

## 6. Logs

Para acompanhar logs em tempo real:

```bash
# Logs do container Docker
docker logs -f grava_nois_system

# Logs do FFmpeg por camera (para debug de conexão com câmera)
tail -f logs/ffmpeg_cam01.log

# Verificar health do container
docker ps  # Coluna STATUS deve mostrar "healthy" após ~60s
```

**Interpretando os logs de inicialização:**

Durante o boot, você verá mensagens do health check RTSP:

```
[rtsp-check] Verificando conectividade com câmera 192.168.68.104:554...
[rtsp-check] Tentativa 1/10...
[rtsp-check] ✗ Falha: [Errno 111] Connection refused
[rtsp-check] Aguardando 5s antes de tentar novamente...
[rtsp-check] Tentativa 2/10...
[rtsp-check] ✓ Câmera acessível em 192.168.68.104:554!
[ffmpeg] Logs sendo salvos em: /usr/src/app/logs/ffmpeg_cam01.log
```

Se a câmera não estiver acessível, o container permanece vivo, publica status degradado quando MQTT estiver disponível e o supervisor tenta reiniciar o FFmpeg em background.

---

## 7. Troubleshooting

### Problema: "Nenhum segmento capturado — encerrando"

**Causa**: Sistema não conseguiu conectar à câmera RTSP ou FFmpeg não iniciou corretamente.

**Solução**:
1. Verifique logs do FFmpeg: `tail -f logs/ffmpeg_cam01.log`
2. Confirme que a câmera está ligada e acessível na rede
3. Teste conectividade manual: `nc -zv <IP_CAMERA> 554`
4. Verifique se `GN_RTSP_URL` está correta no `.env`
5. Aumente `GN_RTSP_MAX_RETRIES` se a câmera demora muito para inicializar

### Problema: Container reiniciando constantemente

**Causa**: o processo principal Python está encerrando ou falhando antes de manter o runtime ativo.

**Solução**:
1. Verifique logs: `docker logs grava_nois_system`
2. Confirme que `.env`/`config.json` carregam sem erro fatal
3. Verifique logs do FFmpeg em `logs/ffmpeg_cam01.log`
4. Teste conectividade com a câmera manualmente

### Problema: Perda de energia - sistema não reconecta

**Solução**: O sistema agora possui retry automático implementado. Após queda de energia:
- O container aguarda até 60s antes de começar health checks
- Tenta conectar à câmera conforme `GN_RTSP_MAX_RETRIES` e timeouts configurados
- Se falhar, marca a câmera como indisponível e tenta novamente pelo supervisor
- Configure `GN_RTSP_MAX_RETRIES` para mais tentativas se necessário

---

## 8. Encerrar o sistema

```bash
docker compose down
```

---
