# Formato Vertical e Qualidade Final

Este documento substitui o contrato antigo de `MOBILE_FORMAT`.

## Estado Atual

`MOBILE_FORMAT` não faz parte do contrato ativo do pipeline. O worker atual controla a composição final com:

- `processing.verticalFormat` ou `VERTICAL_FORMAT`;
- `processing.hqCrf` / `GN_HQ_CRF`;
- `processing.hqPreset` / `GN_HQ_PRESET`;
- `processing.lmCrf` / `GN_LM_CRF`;
- `processing.lmPreset` / `GN_LM_PRESET`;
- `processing.watermark.*`.

## `VERTICAL_FORMAT`

Quando `VERTICAL_FORMAT=1`, o pipeline aplica crop central 9:16 antes do watermark:

```text
crop=ih*9/16:ih:(iw-ih*9/16)/2:0
```

Não existe scale forçado para `1080x1920` no fluxo atual. A resolução final é a resolução resultante do crop.

Exemplo:

```text
1920x1080 -> 607x1080
```

Esse comportamento preserva altura e evita upscale artificial, mas descarta laterais do vídeo.

## Qualidade

Para qualidade máxima:

```bash
GN_LIGHT_MODE=0
VERTICAL_FORMAT=0
GN_RTSP_PROFILE=hq
GN_RTSP_REENCODE=0
GN_RTSP_FPS=
GN_HQ_CRF=16
GN_HQ_PRESET=slow
```

Para entrega vertical:

```bash
GN_LIGHT_MODE=0
VERTICAL_FORMAT=1
GN_RTSP_PROFILE=hq
GN_RTSP_REENCODE=0
GN_HQ_CRF=16
GN_HQ_PRESET=slow
```

## Validação

Inspecione a resolução e o codec do arquivo final:

```bash
ffprobe -hide_banner -v error \
  -select_streams v:0 \
  -show_entries stream=codec_name,width,height,r_frame_rate,bit_rate,pix_fmt \
  -show_entries format=duration,size,bit_rate \
  -of json highlights_wm/arquivo.mp4
```

Verifique o comando de watermark nos testes:

```bash
python -m unittest tests.test_mobile_format
```

