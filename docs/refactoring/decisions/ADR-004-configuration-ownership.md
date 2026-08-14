# ADR-004 — Propriedade da configuração

- Status: Aceito
- Data: 2026-07-27

## Contexto

Leituras diretas de env e configuração em diferentes módulos permitem decisões
contraditórias no mesmo processo.

## Decisão

Infrastructure resolve uma vez a precedência `config.json → env legado →
defaults`, valida e produz snapshots imutáveis. Application recebe configuração
explícita por caso de uso; domain recebe apenas valores/políticas necessários.
Secrets são expostos separadamente por `SecretsProvider`.

Adicionar `capture.bufferSeconds`, fallback `GN_MAX_BUFFER_SECONDS` e padrão
`max(40, pre + post + 2 × segmentSeconds)`.

## Consequências

Fluxos migrados não podem chamar `os.getenv()` ou acessar singleton de config.
Mudança pública de configuração atualiza `.env.example`, `README.md` e a spec na
mesma task.

