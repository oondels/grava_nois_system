# ADR-001 — Arquitetura alvo

- Status: Aceito
- Data: 2026-07-27

## Contexto

O runtime mistura regras, orchestration, ferramentas externas e persistência em
módulos extensos, dificultando teste, troca de tecnologia e recuperação de
falhas.

## Decisão

Adotar Clean Architecture com `domain`, `application`, `infrastructure` e
`bootstrap`. Dependências apontam para dentro; domínio é Python puro,
application contém casos de uso e ports, infrastructure implementa adapters e
bootstrap faz wiring manual por construtor. `main.py` será entrypoint mínimo.

## Consequências

Novas abstrações devem nascer de uma necessidade de substituição/teste, não por
simetria. Imports serão fiscalizados por teste AST. Durante a migração, fachadas
legadas mantêm compatibilidade até o cutover.

