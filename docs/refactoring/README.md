# Refatoração para Clean Architecture

Este diretório é o centro de controle da refatoração arquitetural do edge de
replays esportivos. Ele permite que o trabalho seja retomado com segurança por
outra pessoa, agente ou janela de contexto sem depender do histórico da
conversa.

## Como navegar

1. Leia [REFACTORING_PLAN.md](REFACTORING_PLAN.md) para entender o estado alvo,
   os contratos preservados e os gates globais.
2. Consulte [TASKS.md](TASKS.md) para escolher uma task `READY` cujas
   dependências estejam concluídas.
3. Siga [WORKFLOW.md](WORKFLOW.md) antes de alterar código ou o estado de uma
   task.
4. Use exclusivamente `progress/RF-NNN.md` para registrar o contexto transitório
   e o handoff daquela task.
5. Registre decisões duráveis em `decisions/` e mantenha as especificações em
   `docs/specs/system/` sincronizadas com o comportamento real.

## Fontes de verdade

- Arquitetura e estratégia: `REFACTORING_PLAN.md`.
- Estado e dependências: `TASKS.md`.
- Procedimento de execução: `WORKFLOW.md`.
- Contexto da task atual: `progress/RF-NNN.md`.
- Decisões duráveis: `decisions/ADR-NNN-*.md`.
- Contrato operacional: `docs/specs/system/`.

Em caso de divergência, código e documentação devem ser corrigidos juntos. O
diretório `docs/reports/` é histórico e não deve receber progresso desta
iniciativa.

