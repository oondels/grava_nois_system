# MOBILE_FORMAT - Otimização de Vídeo para Celular

## O que é

`MOBILE_FORMAT` é uma flag que otimiza o formato do vídeo final para visualização em dispositivos móveis. No fluxo horizontal, ela limita a saída a **720p** de altura. No fluxo vertical (`VERTICAL_FORMAT=1`), o pipeline faz `crop 9:16` primeiro e entrega **1080x1920**, que é o formato-alvo para Reels/TikTok.

## Status Padrão

- **Padrão: ATIVO** (`MOBILE_FORMAT=1`)
- Todos os vídeos gerados serão otimizados para móvel por padrão

## Configuração

### Ativar/Desativar

Adicione ao seu `.env`:

```bash
# Ativo (padrão - recomendado)
MOBILE_FORMAT=1

# Desativo (mantém resolução original)
MOBILE_FORMAT=0
```

### Valores Aceitos

Qualquer um desses ativa a flag:
- `1`, `true`, `yes`, `y`, `on`

Qualquer outro valor (ou ausência) desativa:
- `0`, `false`, vazio, etc.

---

## Como Funciona

### Quando MOBILE_FORMAT=1 (Ativo)

1. **Detecção de Resolução**
   - Lê metadados do vídeo capturado
   - Se `VERTICAL_FORMAT=1`: recorta no centro para `9:16` e escala para `1080x1920`
   - Caso contrário, se altura > 720p: redimensiona para 720p
   - Se altura ≤ 720p: mantém original

2. **Redimensionamento**
   - Horizontal: `scale=-2:720` (altura máx 720p, largura proporcional)
   - Vertical: `crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920`
   - Preserva aspect ratio
   - Aplica interpolação de qualidade

3. **Resultado**
   - Exemplo: 1920x1080 → 1280x720 (~44% menor arquivo)
   - Exemplo com `VERTICAL_FORMAT=1`: 1920x1080 → 1080x1920
   - Exemplo: 4K (3840x2160) → 1280x720 (~90% menor arquivo)
   - Qualidade visual preservada em telas de celular

### Quando MOBILE_FORMAT=0 (Desativo)

- Vídeo mantém resolução de captura
- Arquivo maior, mas qualidade máxima
- Útil para arquivamento ou edição posterior

---

## Casos de Uso

### ✅ Recomendado ATIVAR (MOBILE_FORMAT=1)

- **WiFi/4G lento**: Arquivo menor = upload mais rápido
- **Armazenamento limitado**: Reduz espaço em disco do edge device
- **Visualização móvel**: Maioria dos usuários acessa via celular
- **Streaming**: Reduz consumo de banda do backend

**Cenário típico**: Um replay de 10s capturado em 1080p
- COM `MOBILE_FORMAT=1`: ~2-3 MB
- SEM `MOBILE_FORMAT=0`: ~6-9 MB

### ⚠️ Considerar DESATIVAR (MOBILE_FORMAT=0)

- **Câmeras 4K ou Full HD premium**: Quer manter qualidade máxima
- **Análise esportiva profissional**: Requer detalhe fino (ex: movimento do pé)
- **Edição posterior**: Material bruto precisa de qualidade alta
- **Arquivamento legal**: Conformidade regulatória

---

## Monitoramento

### Verificar Configuração Ativa

Consulte os logs do worker:

```bash
tail -f logs/worker_*.log | grep -i "mobile_format"
```

Exemplo de saída:
```
[2026-04-04 16:30:45] [INFO] Mobile format ativo: redimensionando 1920x1080 → máx 720p
```

### Verificar Sidecar JSON

Cada arquivo processado gera `.json` com metadados:

```json
{
  "wm_encode": {
    "preset": "veryfast",
    "crf": 20,
    "mobile_format": true
  },
  "meta_wm": {
    "width": 1280,
    "height": 720,
    "duration_sec": 10.5
  }
}
```

---

## Impacto de Qualidade

### Visualmente

- **Reprodução em celular (telas ≤ 6")**
  - 720p é suficiente (DPI típico 300+)
  - Nenhuma degradação perceptível

- **Análise em computador desktop**
  - 720p pode parecer comprimido se em 4K monitor
  - Recomendado: desativar se precisar de detalhe fino

### Tecnicamente

| Aspecto | Impacto |
|---------|---------|
| **Nitidez** | Mínima redução (aceitável em móvel) |
| **Artifacts** | Nenhum novo (redimensionamento suave) |
| **Codec** | Sem mudança (H.264) |
| **Frame rate** | Sem mudança (original mantido) |

---

## Exemplos

### Exemplo 1: Câmera 1080p, MOBILE_FORMAT=1

```bash
# .env
MOBILE_FORMAT=1
GN_RTSP_URL=rtsp://camera:pass@192.168.1.20/stream1
```

**Resultado**:
```
Capturado:    1920x1080 @ 30fps (buffer segmentos)
Processado:   1280x720 @ 30fps (com watermark)
Arquivo:      highlight_cam01_20260404-163000.mp4
Tamanho:      2.8 MB (10 segundos)
```

### Exemplo 2: Câmera 720p, MOBILE_FORMAT=1

```bash
# .env
MOBILE_FORMAT=1
GN_RTSP_URL=rtsp://camera:pass@192.168.1.20/stream1
```

**Resultado**:
```
Capturado:    1280x720 @ 30fps
Processado:   1280x720 @ 30fps (sem redimensionamento)
Arquivo:      highlight_cam01_20260404-163000.mp4
Tamanho:      4.1 MB (10 segundos)
```
➜ Nenhuma transformação (já está no alvo)

### Exemplo 3: Análise Esportiva Profissional (MOBILE_FORMAT=0)

```bash
# .env
MOBILE_FORMAT=0
GN_RTSP_URL=rtsp://camera:pass@192.168.1.20/stream1
```

**Resultado**:
```
Capturado:    1920x1080 @ 60fps
Processado:   1920x1080 @ 60fps (sem redimensionamento)
Arquivo:      highlight_cam01_20260404-163000.mp4
Tamanho:      18.5 MB (10 segundos)
```
➜ Mantém qualidade máxima para edição

---

## Configurações Recomendadas por Cenário

| Cenário | MOBILE_FORMAT | Outros | Notas |
|---------|---|---|---|
| **Quadra com WiFi fraco** | 1 | `GN_RTSP_FPS=20` | Arquivo pequeno + menos CPU |
| **Replay casual** | 1 | `GN_WM_PRESET=veryfast` | Padrão otimizado |
| **Análise esportiva** | 0 | `GN_RTSP_CRF=18` | Máxima qualidade |
| **Armazenamento limitado** | 1 | `GN_RTSP_FPS=15` | Máxima compressão |

---

## Troubleshooting

### Vídeos ficam pixelados

**Não é culpa do MOBILE_FORMAT.** Culprits comuns:
- CRF muito alto (ex: 28+) → tente `GN_RTSP_CRF=20`
- Câmera com DTS não-monotônico → tente `GN_RTSP_USE_WALLCLOCK=1`
- WiFi instável → tente `GN_RTSP_REENCODE=1`

### Vídeos muito grandes apesar de MOBILE_FORMAT=1

Verifique:
1. Flag está realmente ativa? `grep MOBILE_FORMAT .env`
2. Logs mostram scale? `tail logs/worker_*.log | grep -i redimensionando`
3. Sidecar JSON tem `"mobile_format": true`?

Se tudo ok mas arquivo grande, pode ser:
- Câmera entrando com ≤ 720p (nada a redimensionar)
- CRF está muito baixo (alta qualidade) → tente aumentar
- Duração do clipe é longa → normal

---

## Performance

### Impacto na CPU

Redimensionamento tem overhead mínimo:

| Transformação | CPU (Raspberry Pi 4) |
|---|---|
| Sem MOBILE_FORMAT | ~40-50% |
| COM MOBILE_FORMAT (1920→720) | ~42-52% (~2% extra) |

**Conclusão**: Impacto negligenciável.

### Impacto no Tempo de Processamento

```
Arquivo 10s, 1920x1080 → 720p
├─ Watermark: 1.2s
├─ Redimensionamento: 0.1s (já dentro do ffmpeg)
└─ Total: 1.3s
```

**Observação**: Redimensionamento ocorre **durante** o encoding (não adiciona tempo).

---

## Referência Rápida

```bash
# Ativar (padrão)
echo "MOBILE_FORMAT=1" >> .env

# Desativar
echo "MOBILE_FORMAT=0" >> .env

# Verificar se está ativo
grep -i mobile_format .env

# Ver logs de redimensionamento
grep -i "redimensionando\|mobile.*format" logs/*.log
```

---

## Integração com Backend

O backend recebe no JSON de metadados:

```json
{
  "wm_encode": {
    "mobile_format": true
  },
  "meta_wm": {
    "width": 1280,
    "height": 720
  }
}
```

Use para:
- Adaptar player (aspect ratio)
- Gerar thumbnails apropriadas
- Logging/analytics de qualidade

---

## Veja Também

- [`docs/RTSP_TUNING.md`](RTSP_TUNING.md) — Otimização de captura RTSP
- `README.md` — Guia geral do sistema
- `.env.example` — Exemplo completo de configuração
