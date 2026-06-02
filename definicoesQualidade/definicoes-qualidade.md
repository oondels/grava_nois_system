# Definicoes de Qualidade

Este arquivo reune presets prontos para testar a qualidade de captura e do arquivo final no `grava_nois_system`.

Pontos importantes:

- `GN_LIGHT_MODE` nao e modo de "qualidade alta" nem modo de "remover thumbnail". Hoje ele troca os parametros de encode do watermark e, quando o profile RTSP nao esta explicito, infere captura `compatible`.
- Thumbnail nao faz parte do pipeline ativo atual.
- A qualidade da captura depende principalmente de `GN_RTSP_PROFILE`, `GN_RTSP_REENCODE`, `GN_RTSP_CRF`, `GN_RTSP_PRESET`, `GN_RTSP_FPS` e `GN_RTSP_GOP`.
- A qualidade final do arquivo tambem depende de `GN_HQ_CRF`, `GN_HQ_PRESET`, `GN_LM_CRF`, `GN_LM_PRESET` e `VERTICAL_FORMAT`.

## 1. Alta Qualidade Segura

Preset recomendado para comecar. Mantem boa qualidade, evita crop e usa passthrough na captura quando a camera e estavel.

```bash
GN_RTSP_PROFILE=hq
GN_RTSP_REENCODE=0
GN_RTSP_FPS=

GN_LIGHT_MODE=0
VERTICAL_FORMAT=0
GN_HQ_CRF=18
GN_HQ_PRESET=medium
```

Quando usar:

- quando quiser boa qualidade de producao;
- quando quiser evitar reencode na captura;
- quando a camera tiver timestamp/GOP estavel.

## 2. Maxima Fidelidade

Preset para testar passthrough da captura RTSP, preservando ao maximo o stream original.

```bash
GN_RTSP_REENCODE=0

GN_LIGHT_MODE=0
VERTICAL_FORMAT=0
GN_HQ_CRF=16
GN_HQ_PRESET=slow
```

Quando usar:

- quando a camera entrega stream RTSP estavel;
- quando quiser validar a melhor fidelidade possivel do source.

Risco:

- se a camera tiver DTS/timestamps ruins, voce pode ver stutter, falha de concat ou highlight quebrado.
- se isso acontecer, volte para o preset "Alta Qualidade Segura".

## 3. Qualidade Boa com Vertical

Preset para manter boa captura, mas com entrega final em `9:16`.

```bash
GN_RTSP_REENCODE=1
GN_RTSP_CRF=18
GN_RTSP_PRESET=fast
GN_RTSP_FPS=
GN_RTSP_GOP=30

GN_LIGHT_MODE=0
VERTICAL_FORMAT=1
GN_HQ_CRF=16
GN_HQ_PRESET=slow
```

Quando usar:

- quando o destino final precisa ser vertical;
- quando quiser testar a perda visual causada pelo crop central.

## 4. Perfil Equilibrado

Preset para reduzir custo de CPU e tamanho de arquivo sem cair demais a qualidade.

```bash
GN_RTSP_REENCODE=1
GN_RTSP_CRF=20
GN_RTSP_PRESET=veryfast
GN_RTSP_FPS=
GN_RTSP_GOP=30

GN_LIGHT_MODE=0
VERTICAL_FORMAT=0
GN_HQ_CRF=18
GN_HQ_PRESET=medium
```

## 5. Notas de Ajuste Fino

- `GN_RTSP_CRF`: menor valor = melhor qualidade e arquivo maior. Valores praticos: `17`, `18`, `20`, `23`.
- `GN_RTSP_PRESET`: mais lento = melhor compressao por bitrate, mas usa mais CPU. Ordem tipica: `veryfast` -> `fast` -> `medium`.
- `GN_RTSP_FPS`: deixe vazio para nao forcar filtro de FPS. So fixe se precisar limitar taxa de quadros.
- `GN_RTSP_GOP`: use valor proximo do FPS real da camera. Exemplos: `25` para camera em 25 fps, `30` para camera em 30 fps.
- `GN_HQ_CRF`: controla o encode final com watermark no modo normal. `18` e seguro, `16` e premium, `14` gera arquivos maiores.
- `GN_HQ_PRESET`: `medium` e equilibrado, `slow` comprime melhor com mais CPU.
- `VERTICAL_FORMAT=1`: recorta para `9:16` sem scale forcado. Isso muda framing e descarta laterais.

## 6. Recomendacao Pratica

Ordem sugerida de teste:

1. teste primeiro `Alta Qualidade Segura`;
2. se a camera for estavel, teste `Maxima Fidelidade`;
3. se o destino for reels/stories/shorts, teste `Qualidade Boa com Vertical`.
