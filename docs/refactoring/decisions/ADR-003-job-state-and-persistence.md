# ADR-003 — Estado e persistência de jobs

- Status: Aceito
- Data: 2026-07-27

## Contexto

Campos implícitos no sidecar, contagem distribuída de tentativas, sleeps e locks
sem expiração tornam retomada e concorrência difíceis de provar.

## Decisão

Modelar o job com estados explícitos e transições validadas. Persistir sidecars
com `schema_version`, mantendo leitura legada, por temp file, flush, `fsync` e
replace atômico. Uma tentativa incrementa `attempts` uma vez; retry usa
`next_attempt_at`. Locks tornam-se leases com `job_id`, `boot_id`, PID e
`acquired_at`. Cleanup depende do estado e o artefato enviado é preservado.

## Consequências

Todas as transições e retomadas serão testadas. JSON inválido será isolado para
investigação, sem descarte silencioso. Compatibilidade de leitura é obrigatória
até remoção formal do legado.

