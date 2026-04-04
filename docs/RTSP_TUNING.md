# RTSP Capture Tuning Guide

Este documento descreve as opções de configuração disponíveis para otimizar a captura RTSP em diferentes cenários de câmera e rede.

## Visão Geral

O sistema de captura RTSP foi projetado para lidar com câmeras WiFi instáveis (ex: Tapo C500) que podem gerar:
- DTS (Decode Timestamp) não-monotônicos
- Perda de pacotes
- Timestamps não-confiáveis
- Frames corrompidos

Por padrão, o sistema aplica **re-encoding com error concealment** para mitigar esses problemas. Todas as opções abaixo podem ser configuradas via variáveis de ambiente.

---

## Variáveis de Ambiente

### Modo de Processamento

#### `GN_RTSP_REENCODE` (default: `1`)
Controla se o stream RTSP será **re-encodado** ou passado diretamente.

- `GN_RTSP_REENCODE=1` (default): Re-encode com libx264
  - ✅ Reconstrui frames corrompidos (error concealment)
  - ✅ Garante keyframes em pontos consistentes
  - ❌ Consome mais CPU
  - **Recomendado para**: Câmeras WiFi instáveis

- `GN_RTSP_REENCODE=0`: Passthrough direto (copy)
  - ✅ Baixíssima CPU
  - ✅ Sem re-encoding, zero latência extra
  - ❌ Depende de timestamps e DTS confiáveis
  - **Recomendado para**: Câmeras cabeadas ou com DTS estável

```bash
# Teste passthrough (use com câmeras estáveis)
GN_RTSP_REENCODE=0 python main.py
```

---

### Timestamps e Sincronização

#### `GN_RTSP_USE_WALLCLOCK` (default: `0`)
Usa relógio do sistema como timestamps em vez de confiar nos timestamps do stream RTSP.

- `GN_RTSP_USE_WALLCLOCK=0` (default): Usa PTS/DTS do stream
  - ✅ Respeita timing original da câmera
  - ✅ Evita jitter em redes estáveis
  - ❌ Pode gerar problemas se DTS é não-monotônico

- `GN_RTSP_USE_WALLCLOCK=1`: Usa wallclock do host
  - ✅ Útil quando câmera gera DTS não-monotônicos
  - ❌ Pode introduzir jitter em redes instáveis
  - **Recomendado para**: Câmeras com problemas de timestamp

```bash
# Teste wallclock em câmeras problemáticas
GN_RTSP_USE_WALLCLOCK=1 python main.py
```

---

### Qualidade de Encoding (quando `GN_RTSP_REENCODE=1`)

#### `GN_RTSP_CRF` (default: `23`)
Qualidade de compressão H.264 (0-51, menor = melhor qualidade, maior tamanho).

- `18-20`: Qualidade alta (arquivo grande, CPU média)
- `23`: Padrão (bom balanço qualidade/tamanho)
- `28-30`: Arquivo pequeno (qualidade reduzida, CPU baixa)

```bash
# Qualidade ultra-alta para sports
GN_RTSP_CRF=18 python main.py

# Arquivo pequeno para armazenamento limitado
GN_RTSP_CRF=30 python main.py
```

#### `GN_RTSP_PRESET` (default: `veryfast`)
Velocidade de encoding (ultrafast, superfast, veryfast, fast, medium, slow).

- `ultrafast` / `superfast`: Baixa CPU, qualidade reduzida
- `veryfast` (default): Balanço bom
- `fast` / `medium`: Melhor qualidade, CPU média
- `slow`: Máxima qualidade, CPU alta

```bash
# Melhor qualidade com CPU mais potente
GN_RTSP_PRESET=fast GN_RTSP_CRF=20 python main.py

# Mínima CPU em Raspberry fraco
GN_RTSP_PRESET=ultrafast GN_RTSP_CRF=28 python main.py
```

#### `GN_RTSP_GOP` (default: `25`)
Intervalo entre keyframes em quadros (Group of Pictures).

- Padrão: `25` (keyframe a cada ~1s em 25fps)
- Aumentar: Arquivo menor, pior seek
- Diminuir: Melhor seek, arquivo maior

---

### Taxa de Frames

#### `GN_RTSP_FPS` (default: vazio)
Limita a taxa de frames do stream (ex: `15`, `20`, `24`, `30`).

Aplica filtro ffmpeg `fps=N` **leve** (não é re-encode pesado):
- Descarta frames desnecessários
- Mantém qualidade dos frames restantes
- Reduz carga de processamento

```bash
# Limita a 15fps (reduz CPU sem perder qualidade de encode)
GN_RTSP_FPS=15 python main.py

# Limita a 20fps em câmeras 30fps
GN_RTSP_FPS=20 python main.py
```

---

## Cenários Comuns

### Câmera WiFi Instável (ex: Tapo C500)

```bash
# Padrão (recomendado)
GN_RTSP_REENCODE=1          # Re-encode com error concealment
GN_RTSP_PRESET=veryfast     # CPU moderada
GN_RTSP_CRF=23              # Qualidade padrão
```

Se vê **glitches ou stutter**:
```bash
# Tenta usar wallclock para sincronização
GN_RTSP_USE_WALLCLOCK=1 python main.py
```

Se a **CPU está alta** (>80%):
```bash
GN_RTSP_PRESET=ultrafast   # Reduce CPU drasticamente
GN_RTSP_CRF=26             # Qualidade um pouco menor
GN_RTSP_FPS=20             # Limita a 20fps
```

### Câmera Cabeada (Ethernet)

```bash
# DTS é estável, pode usar passthrough
GN_RTSP_REENCODE=0  # Passthrough puro, quase zero CPU
```

### Câmera com CPU Limitada (Raspberry Pi Zero)

```bash
GN_RTSP_PRESET=superfast   # Mínima CPU
GN_RTSP_CRF=28             # Arquivo pequeno
GN_RTSP_FPS=15             # 15fps é suficiente para replay
GN_RTSP_REENCODE=1         # Garante error concealment
```

### Câmera com Arquivo Grande (mais qualidade)

```bash
GN_RTSP_PRESET=fast        # Melhor qualidade
GN_RTSP_CRF=18             # Alta qualidade
GN_RTSP_FPS=30             # 30fps (máximo qualidade)
```

---

## Como Testar

### Script Automático

```bash
./test_wallclock_quality.sh "rtsp://user:pass@192.168.1.20/stream1"
```

Gera 4 arquivos de teste (30s cada):
1. Passthrough SEM wallclock
2. Passthrough COM wallclock
3. Re-encode SEM wallclock (padrão)
4. Re-encode COM wallclock

Compare visualmente em VLC:
- Procure por stutter, jitter, frame drops
- Verifique sincronização de áudio/vídeo
- Teste em diferentes posições do arquivo (início, meio, fim)

### Teste Manual Rápido

```bash
# Captura 30s com configuração A
GN_RTSP_REENCODE=1 GN_RTSP_USE_WALLCLOCK=0 python main.py &
sleep 35 && killall -9 python

# Compare: ls -lh queue_raw/
```

---

## Diagnóstico de Problemas

### Stutter ou Congelamento

**Causa**: Timestamps não-monotônicos ou frames corrompidos

**Tente**:
```bash
# 1. Primeiro: wallclock (resolve DTS não-monotônico)
GN_RTSP_USE_WALLCLOCK=1 python main.py

# 2. Se ainda houver stutter: reduz FPS
GN_RTSP_FPS=20 python main.py

# 3. Se CPU está alta: reduz preset
GN_RTSP_PRESET=superfast python main.py
```

### Qualidade Pixelada ou Artefatos

**Causa**: CRF muito alto ou frame corruption não sendo mitigado

**Tente**:
```bash
# 1. Melhora CRF (lower = better)
GN_RTSP_CRF=20 python main.py

# 2. Melhora preset (better quality)
GN_RTSP_PRESET=fast python main.py

# 3. Verifica error concealment
GN_RTSP_REENCODE=1 python main.py  # Não use copy
```

### CPU Muito Alta (>90%)

**Causa**: Re-encode é pesado para o hardware

**Tente** (em ordem de impacto):
```bash
# 1. Reduz FPS
GN_RTSP_FPS=15 python main.py

# 2. Reduz preset
GN_RTSP_PRESET=ultrafast python main.py

# 3. Aumenta CRF (lower quality)
GN_RTSP_CRF=28 python main.py

# 4. Se nada ajudar: passthrough (risco: pode ter stutter)
GN_RTSP_REENCODE=0 python main.py
```

---

## Logs e Monitoramento

Logs do FFmpeg são salvos em `logs/ffmpeg_*.log`:

```bash
# Ver últimas linhas (diagnosticar erros)
tail -f logs/ffmpeg_cam01.log

# Procurar por erros comuns
grep -i "error\|corrupt\|timeout" logs/ffmpeg_*.log
```

---

## Referências

- [FFmpeg RTSP Options](https://ffmpeg.org/ffmpeg-protocols.html#rtsp)
- [FFmpeg Error Concealment](https://ffmpeg.org/ffmpeg-codecs.html#Error-concealment)
- [libx264 Documentation](https://trac.ffmpeg.org/wiki/Encode/H.264)
