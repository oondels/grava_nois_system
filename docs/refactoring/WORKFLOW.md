# Workflow da refatoração

## Antes de iniciar

1. Leia o plano, os ADRs aplicáveis, `TASKS.md` e o progresso da task.
2. Confirme que todas as dependências estão `DONE`.
3. Verifique worktree e baseline; não sobrescreva mudanças de outra pessoa.
4. Confirme owner e reviewer. Um agente só pode assumir uma task por vez.
5. Mude `BACKLOG` para `READY` quando as dependências e o contexto estiverem
   completos; mude para `IN_PROGRESS` apenas ao começar.
6. Preencha branch, snapshot, critérios e próximo passo no progresso.

## Durante a execução

- Trabalhe apenas no escopo declarado.
- Use commits pequenos com assunto iniciado por `RF-NNN`.
- Registre decisões, comandos, resultados, arquivos, riscos e próximo passo no
  progresso ao final de toda sessão.
- Crie ADR quando uma decisão alterar limites, contrato, persistência ou
  estratégia; não esconda decisão arquitetural no log.
- Atualize código, testes e documentação na mesma mudança.
- Não edite arquivos sob responsabilidade simultânea de outro agente. Mudanças
  em composition root, plano, backlog e índices são serializadas pelo
  `architecture_lead`.
- Em um bug inesperado, adicione teste de caracterização e registre se será
  corrigido na task ou movido para nova task.

## Pausa, bloqueio e handoff

Antes de pausar:

1. deixe o repositório em estado identificável;
2. registre o último commit e arquivos em edição;
3. registre comandos e resultados sem dizer apenas “testes passaram”;
4. descreva o bloqueio ou o próximo passo como uma ação executável;
5. atualize o log cronológico.

Use `BLOCKED` somente quando a task não puder avançar. Informe causa, evidência,
quem pode desbloquear e condição de retomada. O próximo agente deve conseguir
continuar lendo apenas plano, backlog, ADRs e progresso.

## Revisão e conclusão

1. O owner valida critérios e gates e move a task para `REVIEW`.
2. Reviewer de papel diferente confere arquitetura, regressões, documentação e
   evidências.
3. Pendências retornam a task para `IN_PROGRESS`.
4. Após aprovação e integração, o owner preenche resumo final, resultados,
   commit e task desbloqueada, então atualiza ambos os arquivos para `DONE`.

Uma task não está concluída com código apenas: testes, documentação, handoff e
revisão são parte da entrega.

## Template do progresso

Cada `progress/RF-NNN.md` mantém estas seções, mesmo quando vazias:

- metadados: status, owner, reviewer, branch e dependências;
- objetivo, escopo e fora de escopo;
- critérios de aceitação;
- snapshot atual;
- decisões/ADRs;
- arquivos modificados;
- testes/comandos/resultados;
- riscos e bloqueios;
- próximo passo exato;
- log de sessões;
- resumo final e próxima task desbloqueada.

