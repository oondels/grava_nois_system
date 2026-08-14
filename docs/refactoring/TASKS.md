# Backlog oficial da refatoração

Estados: `BACKLOG → READY → IN_PROGRESS → REVIEW → DONE`, com `BLOCKED` a partir
de `IN_PROGRESS` e `CANCELLED` a partir de `BACKLOG` ou `READY`.

| ID | Entrega | Estado | Dependências | Owner | Reviewer | Progresso |
|---|---|---|---|---|---|---|
| RF-001 | Governança, branch, templates e ADRs iniciais | DONE | — | documentation_governance | architecture_lead | [RF-001](progress/RF-001.md) |
| RF-002 | Baseline reproduzível, CI e ferramentas | DONE | RF-001 | quality_reliability | architecture_lead | [RF-002](progress/RF-002.md) |
| RF-003 | Caracterização e matriz de contratos | DONE | RF-002 | quality_reliability | capture_replay | [RF-003](progress/RF-003.md) |
| RF-004 | Fundação de domain/application/ports/adapters | DONE | RF-003 | architecture_lead | quality_reliability | [RF-004](progress/RF-004.md) |
| RF-005 | Configuração e identidade | DONE | RF-004 | device_integrations | architecture_lead | [RF-005](progress/RF-005.md) |
| RF-006 | Capture e Replay | DONE | RF-004, RF-005 | capture_replay | quality_reliability | [RF-006](progress/RF-006.md) |
| RF-007 | Supervisão e triggers | DONE | RF-006 | capture_replay | device_integrations | [RF-007](progress/RF-007.md) |
| RF-008 | Persistência e máquina de estados | DONE | RF-004, RF-003 | delivery_pipeline | quality_reliability | [RF-008](progress/RF-008.md) |
| RF-009 | Delivery pipeline | DONE | RF-005, RF-008 | delivery_pipeline | architecture_lead | [RF-009](progress/RF-009.md) |
| RF-010 | Device Management | DONE | RF-005, RF-004 | device_integrations | quality_reliability | [RF-010](progress/RF-010.md) |
| RF-011 | Composition root, EdgeRuntime e cutover | REVIEW | RF-007, RF-009, RF-010 | architecture_lead | quality_reliability | [RF-011](progress/RF-011.md) |
| RF-012 | Hardening operacional | REVIEW | RF-011 | quality_reliability | delivery_pipeline | [RF-012](progress/RF-012.md) |
| RF-013 | Remoção do legado e consolidação documental | BACKLOG | RF-012 | documentation_governance | architecture_lead | [RF-013](progress/RF-013.md) |
| RF-014 | Aceitação final e prontidão para merge | BACKLOG | RF-013 | architecture_lead | quality_reliability | [RF-014](progress/RF-014.md) |

Os critérios completos e o próximo passo de cada entrega ficam exclusivamente no
arquivo de progresso correspondente. O estado deve ser alterado neste arquivo e
no progresso na mesma mudança.
