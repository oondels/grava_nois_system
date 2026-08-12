# DESIGN_SPEC - Grava Nois System

## Modo rental

O mesmo pipeline edge opera em `fixed` ou `rental`. O modo rental remove a dependência de venue e usa o endpoint de metadata específico; veja `system/CONFIGURATION.md`, `PIPELINE.md` e `BUSINESS_RULES.md`.
Na configuração remota MQTT, o modo rental envia `client_id=null` e `venue_id=null`; a API resolve o cliente pelo contrato temporal.
Registro inicial e retry compartilham a mesma normalização do envelope de clipe; rejeições definitivas no finalize encerram o item local em vez de reiniciar o ciclo.
A identidade da clean architecture aplica as mesmas invariantes: `fixed` exige venue e `rental` exige venue ausente. Checkpoints de delivery nunca persistem credenciais temporárias da URL assinada.

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
- Configuração operacional e remota: [docs/specs/system/CONFIGURATION.md](./system/CONFIGURATION.md)
- Regras operacionais e de segurança local: [docs/specs/system/BUSINESS_RULES.md](./system/BUSINESS_RULES.md)
- Integrações externas e configuração: [docs/specs/system/INTEGRATIONS.md](./system/INTEGRATIONS.md)
- Operação, testes e cautelas: [docs/specs/system/OPERATIONS.md](./system/OPERATIONS.md)

## 3. Suggested Reading Order

Para manutenção geral:

1. [docs/specs/system/README.md](./system/README.md)
2. [docs/specs/system/ARCHITECTURE.md](./system/ARCHITECTURE.md)
3. [docs/specs/system/CONFIGURATION.md](./system/CONFIGURATION.md) quando a task tocar config, env, MQTT ou deploy
4. A spec especializada da área impactada

Para tasks por assunto:

- captura, buffer, ffmpeg, highlights: [docs/specs/system/PIPELINE.md](./system/PIPELINE.md) e [docs/specs/system/ARCHITECTURE.md](./system/ARCHITECTURE.md)
- trigger, GPIO, Pico, janela horária: [docs/specs/system/BUSINESS_RULES.md](./system/BUSINESS_RULES.md) e [docs/specs/system/PIPELINE.md](./system/PIPELINE.md)
- upload, HMAC, API, retry policy: [docs/specs/system/INTEGRATIONS.md](./system/INTEGRATIONS.md) e [docs/specs/system/BUSINESS_RULES.md](./system/BUSINESS_RULES.md)
- MQTT, presença, heartbeat e futuro command/control: [docs/specs/system/ARCHITECTURE.md](./system/ARCHITECTURE.md), [docs/specs/system/INTEGRATIONS.md](./system/INTEGRATIONS.md) e [docs/specs/system/BUSINESS_RULES.md](./system/BUSINESS_RULES.md)
- worker, sidecar, fila, falhas, reprocessamento: [docs/specs/system/PIPELINE.md](./system/PIPELINE.md) e [docs/specs/system/OPERATIONS.md](./system/OPERATIONS.md)
- configuração e deploy do edge: [docs/specs/system/CONFIGURATION.md](./system/CONFIGURATION.md), [docs/specs/system/INTEGRATIONS.md](./system/INTEGRATIONS.md) e [docs/specs/system/OPERATIONS.md](./system/OPERATIONS.md)

## 4. Source-of-Truth Rule

As specs são lookup e compressão de contexto. A fonte de verdade final continua sendo:

1. código fonte;
2. testes;
3. estas specs.

Quando houver divergência, a spec deve ser atualizada para refletir o comportamento real do edge.
