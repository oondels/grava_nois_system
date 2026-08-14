# Plano de refatoração para Clean Architecture

## Objetivo

Reorganizar o edge de captura de replays esportivos para tornar regras de
negócio explícitas, dependências substituíveis e falhas recuperáveis, preservando
o contrato operacional atual durante a migração.

Resultados esperados:

- manutenção segura e responsabilidades pequenas;
- domínio e casos de uso independentes de FFmpeg, MQTT, HTTP, filesystem,
  threads, ambiente e logging;
- configuração centralizada e imutável;
- pipeline de jobs persistente, idempotente e testável;
- documentação canônica, testes de caracterização e gates arquiteturais.

## Arquitetura alvo

```text
src/
├── domain/
│   ├── capture/
│   ├── replay/
│   ├── delivery/
│   └── configuration/
├── application/
│   ├── capture/
│   ├── replay/
│   ├── delivery/
│   ├── device/
│   └── ports/
├── infrastructure/
│   ├── media/
│   ├── filesystem/
│   ├── http/
│   ├── mqtt/
│   ├── config/
│   ├── hardware/
│   ├── security/
│   └── observability/
└── bootstrap/
    ├── container.py
    └── runtime.py
```

A direção permitida é:

```text
bootstrap → infrastructure → application → domain
```

- `domain`: tipos, invariantes, políticas e transições puras.
- `application`: casos de uso e ports; conhece apenas o domínio.
- `infrastructure`: adapters para ferramentas e sistemas externos.
- `bootstrap`: composition root, ciclo de vida, concorrência e wiring explícito.
- `main.py`: entrypoint mínimo, sem regras operacionais.

É proibido service locator ou container global. Dependências serão fornecidas
por construtor. Os ports iniciais são `CaptureProcess`, `SegmentRepository`,
`MediaTool`, `ClipJobRepository`, `JobLeaseRepository`,
`VideoBackendGateway`, `EventPublisher`, `OperationalConfigRepository`,
`SecretsProvider`, `Clock` e `IdGenerator`.

## Contextos e comportamento alvo

- **Capture:** câmera, processo, segmentos, readiness e supervisão.
- **Replay:** trigger, janela, cooldown, seleção e construção do clipe.
- **Delivery:** watermark, registro, upload, finalize, retry e limpeza.
- **Device Management:** presença, diagnóstico, configuração e `.env` remotos.
- **Configuration/Identity:** snapshots imutáveis, identidade e secrets.

Correções obrigatórias:

- remover leituras globais de env/config dos fluxos migrados;
- tornar `capture.bufferSeconds` configurável, com fallback
  `GN_MAX_BUFFER_SECONDS` e padrão
  `max(40, pre + post + 2 × segmentSeconds)`;
- modelar sidecar como máquina de estados versionada e gravada atomicamente;
- incrementar `attempts` uma vez por tentativa e agendar retry por
  `next_attempt_at`, sem dormir dentro do worker;
- substituir locks por leases recuperáveis;
- preservar para retry o artefato realmente enviado, inclusive em light mode;
- centralizar cleanup por estado;
- extrair `CameraSupervisor`, `TriggerRouter`, `CaptureReplay`,
  `ProcessClipJob` e `EdgeRuntime`;
- manter wrappers legados até o cutover validado.

Estados principais do job:

```text
QUEUED → PROCESSING → WATERMARKED → REGISTERED → UPLOADED → FINALIZED
                 ↘ RETRY_PENDING ↗
                 ↘ FAILED | DISCARDED | DEV_PRESERVED
```

## Compatibilidade

Até o cutover, preservar CLI, variáveis e configuração pública, diretórios,
nomes de arquivos, leitura de sidecars legados, endpoints HTTP, HMAC,
tópicos/payloads MQTT e protocolos GPIO/Pico. Mudanças públicas exigem atualização
conjunta de `.env.example`, `README.md` e da especificação especializada.

O sidecar novo será aditivamente versionado com `schema_version`; leitores
aceitarão formatos antigo e novo. Bugs só serão corrigidos por task explícita,
com teste que caracterize o problema.

## Estratégia de entrega

O trabalho ocorre na branch longa `refactor/clean-architecture`, com commits
pequenos, reversíveis e identificados por `RF-NNN`. A execução atual permanece
disponível como referência até RF-011. Cada task tem um owner e um reviewer de
papel diferente; mudanças concorrentes no mesmo arquivo são proibidas.

As fases são:

1. governança e baseline;
2. caracterização e fundação;
3. configuração, captura e triggers;
4. persistência e delivery;
5. integrações e novo runtime;
6. hardening, remoção do legado e aceitação.

## Gates globais

- `pytest` como runner único, preservando testes `unittest`;
- Ruff, mypy gradual e teste AST de limites de dependência;
- ao menos 90% de branch coverage em `domain` e `application`;
- 100% das transições de job e decisões de retry, descarte, readiness,
  cooldown e roteamento;
- baseline global de cobertura nunca diminui;
- integração FFmpeg com mídia sintética é obrigatória;
- checklist manual de câmera, Pico e GPIO bloqueia o merge final.

Uma task só é `DONE` quando critérios, testes, cobertura, limites de camada,
documentação, progresso final e revisão cruzada estiverem concluídos.

## Responsabilidades

- `architecture_lead`: limites, ADRs, backlog, composition root e integração.
- `quality_reliability`: baseline, CI, testes, concorrência e falhas.
- `capture_replay`: câmera, buffer, replay, trigger e supervisão.
- `delivery_pipeline`: job, sidecar, lease, retry, upload e cleanup.
- `device_integrations`: config, HTTP, MQTT, segurança, GPIO e Pico.
- `documentation_governance`: fontes canônicas, links e coerência.

