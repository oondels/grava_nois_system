# DESIGN_SPEC

## 1. Overview

`grava_nois_system` é o software edge de captura e upload do ecossistema Grava Nóis. Esta spec é a entrada principal para lookup por code agents e auditoria técnica.

Objetivo desta estrutura:

- localizar rapidamente a área correta do edge;
- evitar abrir `main.py` e o pipeline inteiro sem necessidade;
- separar bootstrap, pipeline, regras locais e integrações externas.

## 2. Spec Navigation

Use este arquivo como índice. Para detalhes, abra apenas a spec especializada relevante para a task.

- Edge lookup: [docs/specs/system/README.md](./system/README.md)
- Arquitetura e módulos internos: [docs/specs/system/ARCHITECTURE.md](./system/ARCHITECTURE.md)
- Pipeline de captura até upload: [docs/specs/system/PIPELINE.md](./system/PIPELINE.md)
- Regras operacionais e de segurança local: [docs/specs/system/BUSINESS_RULES.md](./system/BUSINESS_RULES.md)
- Integrações externas e configuração: [docs/specs/system/INTEGRATIONS.md](./system/INTEGRATIONS.md)
- Operação, testes e cautelas: [docs/specs/system/OPERATIONS.md](./system/OPERATIONS.md)

## 3. Suggested Reading Order

Para manutenção geral:

1. [docs/specs/system/README.md](./system/README.md)
2. [docs/specs/system/ARCHITECTURE.md](./system/ARCHITECTURE.md)
3. A spec especializada da área impactada

Para tasks por assunto:

- captura, buffer, ffmpeg, highlights: [docs/specs/system/PIPELINE.md](./system/PIPELINE.md) e [docs/specs/system/ARCHITECTURE.md](./system/ARCHITECTURE.md)
- trigger, GPIO, Pico, janela horária: [docs/specs/system/BUSINESS_RULES.md](./system/BUSINESS_RULES.md) e [docs/specs/system/PIPELINE.md](./system/PIPELINE.md)
- upload, HMAC, API, retry policy: [docs/specs/system/INTEGRATIONS.md](./system/INTEGRATIONS.md) e [docs/specs/system/BUSINESS_RULES.md](./system/BUSINESS_RULES.md)
- worker, sidecar, fila, falhas, reprocessamento: [docs/specs/system/PIPELINE.md](./system/PIPELINE.md) e [docs/specs/system/OPERATIONS.md](./system/OPERATIONS.md)
- configuração e deploy do edge: [docs/specs/system/INTEGRATIONS.md](./system/INTEGRATIONS.md) e [docs/specs/system/OPERATIONS.md](./system/OPERATIONS.md)

## 4. Source-of-Truth Rule

As specs são lookup e compressão de contexto. A fonte de verdade final continua sendo:

1. código fonte;
2. testes;
3. estas specs.

Quando houver divergência, a spec deve ser atualizada para refletir o comportamento real do edge.
