# Correções coordenadas — 29/09/2026

## Estado da entrega

Correções implementadas em `grava_nois_api`, `grava_nois`, `grava_nois_config`
e `grava_nois_system`, na branch local `fix/e2e-device-reliability`.
As pendências anteriores foram preservadas primeiro em commits: `b7c0da7`
(system: DEV sem câmeras) e `aadad99` (config: documentação Golden).
API e frontend estavam limpos no início. Não houve push ou deploy.

| Frente | Problema tratado | Resultado |
|---|---|---|
| Configuração administrativa | Autenticação parcial, replay e reinício sem confirmação | Controle `.env` v2 autenticado integralmente, prazo e identidade validados; sync recente exigido; recuperação durável; reinício distingue solicitado, admitido e incerto |
| Comandos servidor/host | Publicação confundida com efeito, disputas entre operações, perda de resultados | Admissão serializada, IPC v2 por UUID, resultados persistentes, ACK assinado somente após persistência, resultado incerto sem reexecução automática |
| Frontend | Respostas atrasadas/SSE antecipado, troca de device, rascunhos e estado ambíguo | Correlação de request/device, proteção contra respostas antigas, limpeza de segredos e consulta obrigatória do histórico após envio incerto |
| Config/provisionamento | Healthcheck baseado apenas em PID, defaults divergentes, sanitização arriscada | Saúde do loop real e prontidão das câmeras; validação de configuração; Golden exige cópia descartável e preserva pendências de recuperação |
| Segmentos/replays | Risco de perda durante recuperação/rollback | Compatibilidade v1/v2/v3 preservada; checkpoints/outboxes mantidos; janela de ingestão bloqueia entrega sem apagar replay v3 |
| Operação local | ENTER segurava stdin no encerramento; testes carregavam configuração privada | Leitura interrompível e shutdown com join; suítes isoladas sem dotenv/conexões Python de saída |

Backend e frontend já continham implementação de processamento diferido; não foi
necessário reconstruí-la. Ativação continua desabilitada por padrão e depende da
homologação integrada e do hardware. RF-011–014 (migração arquitetural do runtime)
continuam adiadas, fora desta entrega.

## Verificação de código

- **System:** `.venv/bin/python scripts/test_isolated.py --coverage`: **441 passaram,
  1 ignorado** (câmera opt-in); cobertura de branches domain/application **92,40%**,
  mínimo 90%. Inclui contratos cruzados com runner config e efeitos host simulados.
- **System estático:** Ruff nos módulos novos/reescritos e camadas de arquitetura;
  mypy estrito em **43 arquivos**; sintaxe dos harnesses e `git diff --check`.
- **API:** `npm test`, **38/38 arquivos** aprovados, compilação TypeScript e
  validação sintática dos runners. O runner padrão não lê dotenv nem usa rede.
- **Frontend:** suíte completa inicial com **74 testes** e build de produção aprovados;
  após duas regressões adicionais, suíte focada **6/6** aprovada (**76 testes
  cadastrados**, sem repetir a suíte inteira após o último commit).
- **Config:** **16 testes Python**, **4 suítes Bash**, análise sintática e
  renderização do Compose aprovados. Regressões cruzadas do edge exercitam IPC
  com efeitos invasivos simulados.

Integrações locais executadas antes da mudança de escopo foram encerradas. Os
resultados parciais e o roteiro manual estão em
[e2e-local-validation.md](e2e-local-validation.md). O teste contínuo terminou por
solicitação do usuário, após 7min57s; não equivale à homologação de duas horas.
PostgreSQL descartável validou migrations específicas, concorrência, rollback,
recibos e timeouts da API. O schema auxiliar usa fixtures: isso não comprova uma
migração completa do banco de produção.

## Aplicação e aceite pendente do usuário

A implementação e as verificações de código estão concluídas. A garantia de
operação em produção depende das verificações abaixo, que o usuário optou por
executar. Não há alegação de homologação completa do sistema instalado.

1. Publicar API, frontend, edge e runner compatíveis, mantendo operações remotas
   desabilitadas durante a atualização. Validar sync autenticado v2 antes de
   habilitar alterações administrativas; não existe fallback de escrita v1.
2. Validar migrações no banco de homologação com baseline real e backup. Validar
   broker/Redis/storage e autenticação/RBAC/isolamento de tenant no ambiente alvo.
3. Executar câmera → persistência v3 → processamento → upload/finalize → biblioteca
   do frontend, incluindo interrupção, retomada, janela de ingestão e ACK real.
4. Executar teste contínuo, RTSP e hardware alvo; validar Pico/GPIO, reboot,
   pull/recreate, troca de Wi-Fi e rollback em equipamento de homologação.
5. Validar atualização PWA já instalada, serviço após reinício e sequência de
   rollback preservando volumes, filas e outboxes. Não entregar jobs v3 a worker antigo.

O Compose local agora usa `host_config/.env` privado; preparar esse arquivo e
`runtime_config/config.json` antes de subir. Runner host deve ser instalado pelo
config. Consulte [DEVICE_RELIABILITY.md](../specs/system/DEVICE_RELIABILITY.md)
para caminhos, compatibilidade, confirmação e recuperação de operações incertas.

## Commits de implementação por repositório

- API: `bedf67d`.
- Frontend: `9d2cccc`, `4fb108f`.
- Config: `c128bdf`, `242dec7`.
- System: `b54660a` (controle e recuperação), seguido pelo commit
  `test(system): isole verificacoes e documente homologacao manual`.
