# Changelog

## 2026-04-04

### Added
- Variavel `GN_WM_REL_WIDTH` para ajustar o tamanho relativo das logos no watermark sem editar codigo.
- Novo teste `tests/test_camera_watermark_integration.py` para validar captura real, highlight, crop vertical e geracao do mp4 final com watermark em modo `DEV`.

### Changed
- Fluxo vertical consolidado em `1080x1920`, com crop `9:16` antes do branding.
- Testes de mobile/vertical atualizados para refletir a nova saida final do pipeline.
- README e specs do edge atualizados com os novos controles de watermark e o fluxo de integracao real com camera.
- Teste de integracao com camera passou a gravar artefatos em pasta persistente configuravel por `GN_CAMERA_INTEGRATION_OUTPUT_DIR`.

## 2026-03-05

### Added
- Suporte a watermark duplo no worker: logo principal + `client_logo`.
- Novo script `optimze_image.py` para gerar logos otimizadas em PNG RGBA.
- Novos assets otimizados:
  - `files/replay_grava_nois_wm.png`
  - `files/client_logo_wm.png`
- Novo teste `tests/test_dual_watermark_command.py` para validar o comando ffmpeg com 2 logos.

### Changed
- Fluxo voltou a priorizar latencia do trigger: `build_highlight` permanece rapido e sem watermark.
- Watermark segue assíncrono no `ProcessingWorker` com preset mais rapido por padrao (`GN_WM_PRESET=veryfast`).
- Posicionamento das 2 logos ajustado para centro (empilhadas).
- `main.py` passou a priorizar automaticamente arquivos `_wm.png` quando existirem.
- README atualizado com o comportamento atual do watermark e uso do script de otimizacao.
