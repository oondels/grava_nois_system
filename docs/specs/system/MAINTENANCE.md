# Logos gerenciadas e agente de manutenção

O serviço independente no host recebe operações em `maintenance/*`; a captura mantém seus tópicos existentes. Resultados de IPC com `source=maintenance` pertencem ao agente do host e são ignorados pelo dispatcher do edge. A captura pode ficar desconectada enquanto o host continua administrável.

## Assets e compatibilidade

A label Docker `grn.watermarks=1` declara suporte ao manifesto. `files/client-watermarks.json` contém `{ "version": 1, "slots": { "bottom": "<sha256>", "top": "<sha256>" } }`; slots omitidos usam a seleção legada. Arquivos em `files/client-versions/<sha256>.png` são imutáveis, locais e não entram no build nem no Git.

`WatermarkAssets` valida formato, tamanho, digest e symlinks, guardando a última seleção válida. O worker legado resolve a seleção imediatamente antes de aplicar watermark; trabalhos em andamento mantêm seus caminhos imutáveis. O pipeline diferido resolve a seleção ao montar a política e mantém as cópias de assets já preservadas em cada job. Reenvio de artefato processado não refaz watermark.

O `.env` continua sendo a única fonte de `GN_CLIENT_WATERMARK_ENABLED`. Desabilitada, a flag omite ambas as logos. A marca institucional usa o caminho atual e não participa da edição remota. Seleção legada acontece no bootstrap; manifesto gerenciado pode ser aplicado durante o runtime. Não remover revisões antigas referenciadas por jobs; o host preserva as revisões nesta entrega.

## Volumes e testes

Compose passa `files` para leitura/escrita; configuração, filas e logs continuam persistentes. Scripts, compose e estado privado do agente ficam no host. Não se monta `/opt/.grn` inteiro nem socket Docker.

Validar `tests/test_watermark_assets.py`, watermark/worker, snapshots diferidos e `tests/test_host_action_contract.py`. Deploy depende do provisionamento compatível, migration/API e broker com ACL dos tópicos de manutenção. Reinício físico, nova imagem e câmera precisam de piloto; testes com subprocessos simulados não os homologam.
