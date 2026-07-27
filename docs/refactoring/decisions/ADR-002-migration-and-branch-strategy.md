# ADR-002 — Estratégia de migração e branch

- Status: Aceito
- Data: 2026-07-27

## Contexto

Uma troca integral sem checkpoints tornaria regressões difíceis de localizar,
mas integrar arquitetura parcialmente migrada na branch principal exporia um
runtime inconsistente.

## Decisão

Usar `refactor/clean-architecture` como branch longa, dividida em tasks e commits
pequenos `RF-NNN`. Preservar a implementação atual e introduzir wrappers até que
testes comparativos validem o novo wiring. Só remover legado após cutover,
hardening e auditoria de imports.

## Consequências

A branch exige sincronização frequente e gates por task. Commits permanecem
reversíveis e a branch principal recebe a entrega apenas após RF-014.

