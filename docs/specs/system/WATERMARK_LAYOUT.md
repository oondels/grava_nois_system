# Watermarks proporcionais no processamento

## Runtime

`src/config/watermark_layout.py` valida o contrato e calcula geometria. `src/services/watermark_catalog.py` importa os três arquivos conhecidos do fluxo legado, publica inventário assinado, baixa imagens por UUID e verifica SHA-256/dimensões. A sincronização sondará até três segmentos TS completos recentes por câmera, ignorando o mais novo que ainda pode estar sendo escrito.

As imagens ficam em `files/client-versions/<sha256>.png`, no volume correspondente a `/opt/.grn/files` do host. Escrita usa arquivo temporário, fsync e replace; symlinks são rejeitados. Downloads precedem qualquer promoção de configuração. A restauração de configuração pendente verifica os arquivos locais novamente, sem depender da rede no boot.

A fila limitada do serviço de configuração executa transferências fora do callback de rede MQTT. O worker imediato salva atomicamente `watermark_snapshot` no sidecar antes de processar; retries reutilizam esse snapshot. O modo diferido usa a política por câmera e copia assets para o job, preservando trabalhos admitidos antes de uma mudança.

`add_image_watermark` compõe filtros FFmpeg independentes por slot, com escala contain e multiplicação do canal alpha pela opacidade. O crop vertical antecede os overlays e usa dimensões pares explícitas. Sem layout v2, o processamento legado continua com os três PNGs e seus fatores de escala anteriores.

## Configuração e operação

- `GN_WATERMARK_LAYOUT_JSON`: JSON compacto gerenciado pelo fluxo remoto; não preencher manualmente para ativar a funcionalidade pela primeira vez.
- `GN_CLIENT_WATERMARK_ENABLED`: chave global; persistida junto ao layout. O conversor usa seu valor ao reconstruir o JSON. Alteração manual de `.env` segue o fluxo habitual de aplicação/recriação.
- `GN_API_BASE`: endereço acessível pelo container. HTTPS por padrão; somente no laboratório, `GN_MAINTENANCE_ALLOW_INSECURE_HTTP=1` permite HTTP e é recusado com `NODE_ENV=production`.
- `DEVICE_ID`/`DEVICE_SECRET`: identidade HMAC existente. Não registrar credenciais em logs.

Não apagar `client-versions` enquanto houver config ou clipes referenciando essas imagens. Não há expurgo automático local novo nesta entrega. Os conversores do edge e provisionador preservam layout/master após recreate. Deploy exige API/migration compatíveis e volume gravável de imagens.

Testes: `python -m unittest tests.test_watermark_layout tests.test_device_config_service tests.test_dual_watermark_command tests.test_deferred_recovery tests.test_env_to_config_cli`. O teste de pixels usa FFmpeg real sobre conteúdo sintético em 720p, 1080p, 4:3 e crop vertical, tolerância de 1 pixel nas bordas e 4 níveis na intensidade. Isso não substitui piloto com câmera física.

## Contrato geométrico v2

`processing.watermark.layout` é uma extensão opcional da configuração operacional existente. Sem ela, permanece o comportamento legado. Há três slots fixos: `institutional`, `clientBottom` e `clientTop`. As imagens são compartilhadas por device; cada câmera possui posição, tamanho, opacidade e habilitação independentes.

- `version: 2`, `clientEnabled: boolean` (chave global das logos do cliente).
- `assets`: os três slots referenciam `{id, sha256, width, height}`; os dois slots do cliente admitem `null` quando desabilitados. A imagem institucional é protegida.
- `default`: disposição-base para novas câmeras; `cameras`: mapa por ID com overrides completos.
- Cada disposição contém os três slots, cada um com `{enabled, x, y, width, height, opacity}`. Coordenadas e dimensões são frações de 0 a 1 do **frame final**, origem no canto superior esquerdo; tamanho positivo e opacidade entre 0 e 1.
- A imagem usa `contain`, centralizada no retângulo, preservando sua proporção e transparência. Não há rotação, recorte da imagem ou filtros de aparência adicionais.
- Retângulos habilitados não podem se sobrepor, incluindo margens transparentes; encostar é permitido. A validação continua ativa com `clientEnabled=false`. A institucional permanece habilitada com opacidade positiva.
- O frame usa dimensões observadas da câmera. Em vertical, largura = `max(2, floor(min(W,H*9/16)/2)*2)` e altura = `floor(H/2)*2`. Sem dimensões de câmera ativa, o primeiro salvamento é bloqueado.
- Conversão para pixels: início arredondado para dentro (`ceil`), fim para dentro (`floor`), escala `min(boxWidth/imageWidth, boxHeight/imageHeight)`, imagem com dimensões inteiras arredondadas para baixo e centralizada. Imagens menores que um pixel na resolução conhecida são inválidas. Mudança extrema para resolução inferior pode exigir novo ajuste.

## Aplicação e compatibilidade

O layout usa o fluxo existente de configuração: versão esperada, desired/reported, hash, HMAC, expiração e confirmação do device. Envio HTTP aceito não significa aplicado. O edge prepara todos os assets imutáveis, verifica SHA-256 e dimensões, persiste configuração e `.env` e aplica sem reiniciar quando apenas o watermark muda. Falhas de download/integridade preservam a configuração anterior. Clipes com snapshot já criado mantêm sua disposição; clipes diferidos mantêm a política congelada na admissão.

O inventário assinado anuncia `layoutVersion=2`, imagens e dimensões observadas. A sincronização existente solicita esse inventário; ela requer captura conectada e acesso HTTP do container à API. Devices antigos continuam com logos legadas. Depois de v2 ativo, a operação legada `update_client_watermark` é recusada pela API. Não remover a extensão nem fazer downgrade do edge enquanto houver configuração/clipes v2.
