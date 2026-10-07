# Logos gerenciadas e agente de manutenção

## Validação funcional no dispositivo — 2026-10-07

Implementação concluída e validação funcional confirmada pelo responsável pelos testes no dispositivo piloto em 2026-10-07:

| Funcionalidade | Estado e cenário validado |
| --- | --- |
| Recuperação com captura parada | Implementada e validada: container parado com `docker stop grava_nois_system`, manutenção independente conectada e recuperação por recriação remota. |
| Configuração com reinício | Implementada e validada: alteração de configuração, solicitação de reinício e confirmação da aplicação no dispositivo. |
| Logos e layout de watermark | Implementados e validados: atualização remota da imagem e alteração de posição e dimensões pelo layout da câmera. |
| Diagnósticos e logs | Implementados e validados: coleta remota pelo fluxo administrativo. |

A falha de reinício observada durante o piloto foi resolvida após atualizar os scripts instalados em `/opt/.grn/bin`. Atualizar o repositório ou baixar/recriar a imagem Docker não substitui esses scripts. O procedimento de atualização deve contemplar API, frontend, imagem do edge e scripts do host compatíveis.

Este registro usa a confirmação dos testes feita pelo responsável no piloto; não representa uma nova execução automatizada nem homologação de todos os equipamentos e ambientes. As flags de ativação, autenticação e requisitos de implantação permanecem vigentes.

O serviço independente no host recebe operações em `maintenance/*`; a captura mantém seus tópicos existentes. Resultados de IPC com `source=maintenance` pertencem ao agente do host e são ignorados pelo dispatcher do edge. A captura pode ficar desconectada enquanto o host continua administrável.

## Assets e compatibilidade

A label Docker `grn.watermarks=1` declara suporte ao manifesto. `files/client-watermarks.json` contém `{ "version": 1, "slots": { "bottom": "<sha256>", "top": "<sha256>" } }`; slots omitidos usam a seleção legada. Arquivos em `files/client-versions/<sha256>.png` são imutáveis, locais e não entram no build nem no Git.

`WatermarkAssets` valida formato, tamanho, digest e symlinks, guardando a última seleção válida. O worker legado resolve a seleção imediatamente antes de aplicar watermark; trabalhos em andamento mantêm seus caminhos imutáveis. O pipeline diferido resolve a seleção ao montar a política e mantém as cópias de assets já preservadas em cada job. Reenvio de artefato processado não refaz watermark.

O `.env` continua sendo a única fonte de `GN_CLIENT_WATERMARK_ENABLED`. Desabilitada, a flag omite ambas as logos. A marca institucional usa o caminho atual e não participa da edição remota. Seleção legada acontece no bootstrap; manifesto gerenciado pode ser aplicado durante o runtime. Não remover revisões antigas referenciadas por jobs; o host preserva as revisões nesta entrega.

## Volumes e testes

Compose passa `files` para leitura/escrita; configuração, filas e logs continuam persistentes. Scripts, compose e estado privado do agente ficam no host. Não se monta `/opt/.grn` inteiro nem socket Docker.

Validar `tests/test_watermark_assets.py`, watermark/worker, snapshots diferidos e `tests/test_host_action_contract.py`. Deploy depende do provisionamento compatível, migration/API e broker com ACL dos tópicos de manutenção. Reinício físico, nova imagem e câmera precisam de piloto; testes com subprocessos simulados não os homologam.

## Watermarks v2 (2026-10-06)

O editor por câmera, contrato normalizado, catálogo durável, compatibilidade e implantação estão em [WATERMARK_LAYOUT.md](WATERMARK_LAYOUT.md). As regras anteriores de geometria fixa e troca de PNG permanecem apenas para devices sem layout v2.
