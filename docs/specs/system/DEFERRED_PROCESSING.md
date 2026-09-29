# Processamento diferido — etapa edge

Ambiente Linux/Python 3.11+ (Dockerfile existente). Implementado no edge, desligado por padrão e restrito a `fixed`. Não liberar em produção antes de concluir os testes de contrato ponta a ponta e a validação no equipamento mais fraco. API/aplicativo já implementam os contratos diferidos nesta linha de trabalho. Rental conserva seu pipeline e suas regras de upload manual. Agendamento distribui o trabalho; não reduz necessariamente seu custo total nem garante prazo de conclusão.

## Ativação e configuração

- `processing.deferredEnabled` / `GN_DEFERRED_PROCESSING_ENABLED`: booleano, padrão `false`; exige restart pelo fluxo de configuração existente.
- `processing.additionalWindows` / `GN_PROCESSING_WINDOWS_JSON`: lista, padrão `[]`; aplicada a quente para a próxima decisão. Env é bootstrap/teste e é mantido pelo fluxo MQTT; não é uma segunda sincronização de produção.
- Fuso único: `operationWindow.timeZone` / `GN_TIME_ZONE`, validado com `ZoneInfo`.
- Exemplo: `[{"weekdays":[1,2,3,4,5],"start":"22:00","end":"23:00"},{"weekdays":[7],"start":"23:00","end":"07:00"}]`. Dias ISO: segunda=1, domingo=7; início inclusivo, fim exclusivo. Até 128 intervalos; sobreposições são união. `start=end` é inválido; lista vazia remove apenas intervalos adicionais.
- Madrugada `[00:00,05:00)` é fixa no código, todos os dias. Fora das janelas, 1800 segundos monotônicos sem gatilho válido de qualquer câmera autorizam o próximo trabalho. Reinício começa uma nova contagem de 1800 segundos. Debounce, cooldown e janela de aceitação dos gatilhos continuam anteriores à admissão.
- O modelo existente associa um equipamento a uma instalação (`venue`) e várias câmeras. A agenda autoriza o equipamento inteiro; não há disponibilidade inferida por quadra nem integração com reservas. UI futura deve deixar esse escopo explícito, sem interpretar pausa de uma quadra como pausa das demais.

Apenas um trabalho de mídia por equipamento. Captura continua ativa segundo suas regras próprias. Clique durante processamento oportunista reinicia a inatividade, mas o clipe atual termina; em janela explícita o clique não remove a autorização. Cada novo clipe consulta novamente agenda e atividade, inclusive após adquirir o lock. O fim da janela não cancela o clipe atual. Preservações em andamento têm prioridade sobre iniciar outro processamento.

## Componentes e persistência

| Responsabilidade | Implementação |
|---|---|
| Integração com gatilhos, supervisão e modos | `main.py`, `DeferredRuntime` em `src/bootstrap/deferred_runtime.py` |
| Segmentos fechados e proteção contra limpeza | `start_ffmpeg`, `SegmentBuffer.protect/pending_segments/copied/release` |
| Preservação incremental | `PreserveReplay.begin/tick` |
| Política civil e monotônica | `window_authorization`, `DeviceActivity` |
| Manifestos e exclusão entre processos | `DeferredJobRepository`, `DeferredLeases`, `exclusive_file` |
| Consumidores de mídia e envio | `DeferredCoordinator`, `ProcessClipJob` |
| Concatenação, watermark e thumbnail reais | `DeferredMedia` |
| Metadados, S3 e finalização | `DeferredVideoGateway`, `GravaNoisAPIClient` |
| Espaço e admissão | `StorageMonitor` |
| Eventos e estado com confirmação de aplicação | `OperationalEventService` |

Staging em `queue_raw/.deferred/<job_id>/`, no volume persistente da fila existente. Startup rejeita `tmpfs`/`ramfs`, inclusive montagens fora de `/dev/shm`. Persistência física/volume sobrevivente à recriação do container continua requisito de provisionamento. `runtime_config/operational/` também deve ser persistente. Não existe banco, broker ou scheduler adicional.

Cada gatilho recebe identificação e horário original; cada câmera tem `job_id` próprio. Antes de esperar pós-buffer, registra uma reserva curta no índice do buffer. FFmpeg publica CSV de segmentos encerrados, com tempos reais, nomes separados por sessão de captura e lista limitada. O segmento aberto não é elegível. Cópia para pasta do trabalho usa `.partial`, fsync, validação de tamanho/mtime e rename. Publicação de manifesto reutiliza o writer atômico v2, incluindo fsync do diretório. Não há lock de buffer durante cópia ou encode.

A preservação percorre segmentos posteriores conforme são fechados. Aceita somente cobertura contínua do intervalo pedido, sem mistura de sessões; tolerância de borda/gap de 40 ms. Os tempos CSV são associados ao relógio monotônico pela primeira observação local: não representam sincronização absoluta do relógio da câmera. Essa aproximação e streams com GOP irregular exigem validação no hardware. O conteúdo inclui segmentos completos, como no pipeline existente; o intervalo efetivo pode exceder pré/pós solicitados e fica no manifesto. Não se infere duração pela contagem de arquivos.

`manifest.json` v3 registra: identidades técnicas/tenant/câmera, `trigger_id`, `captured_at`, `created_at`, `requested`, sessão e origem temporal, segmentos ordenados com duração/tamanho/caminho, `preserved_interval`, estado, tentativas por etapa, último erro, backoff, artefatos, ID remoto e recibo de upload. `policy` congela qualidade HQ/light, enquadramento, watermark, DEV, versão aplicada e parâmetros da montagem. Assets são copiados antes da elegibilidade. A agenda posterior não redefine conteúdo. Caminhos de artefatos devem ser relativos e permanecer no diretório do trabalho; symlinks e traversal são rejeitados. URLs assinadas ficam apenas em memória.

## Estados, falhas e recuperação

```text
PREPARING → QUEUED → PROCESSING → ASSEMBLED → PROCESSING → WATERMARKED
           concatenação                   watermark       thumbnail_complete
WATERMARKED → REGISTERED → UPLOADED → FINALIZED → cleaned
      └── DEV_PRESERVED (DEV=true; sem HTTP/S3)
```

- `PREPARING`: salvamento incompleto, nunca consumido. Expiração do pós-buffer (`post + max(30 s, 3×segmentSeconds)`), captura interrompida ou cobertura insuficiente geram `FAILED`; cópias existentes ficam disponíveis. Reinício converte preparação interrompida em falha explícita, sem anunciar preservação.
- `PROCESSING` guarda `media_checkpoint=QUEUED|ASSEMBLED`; reinício retoma esse ponto e substitui somente saída parcial. Watermark pronta e thumbnail incompleta retomam apenas thumbnail.
- `RETRY_PENDING` tem `retry_from`, `next_attempt_at` e tentativas por concat/watermark/thumbnail/registration/upload/finalize. Backoff 120 s exponencial, limitado a 900 s; limite usa `processing.maxAttempts`. Falha de um item não impede seleção de outros elegíveis mais antigos.
- `BLOCKED`: impedimento conhecido (espaço: 30 s; janela de ingestão/configuração da API/conflito remoto: 900 s). Não consome tentativa. Espera por agenda também não é falha nem tentativa.
- `FAILED`: erro terminal ou tentativas esgotadas; segmentos e artefatos permanecem. Correção/reabertura é operação futura explícita, não descarte automático.
- `REGISTERED`: reutiliza identidade e renova URL pelo registro/deduplicação existente. `UPLOADED`: persiste ID, tamanho, SHA-256 e ETag para repetir somente confirmação. `FINALIZED` exige resposta oficial de finalização validada pelo backend; falha posterior de limpeza mantém FINALIZED e tenta limpeza depois de 120 s.
- `DEV_PRESERVED`: preserva mídia localmente e não chama HTTP/S3; não simula upload confirmado. DEV não controla ativação. Presença/configuração MQTT mantêm seus contratos próprios.

`flock` do equipamento e do trabalho substituem expiração de lease para estes trabalhos. O descritor global é herdado pelo processo de mídia: mesmo se o processo Python morrer, um FFmpeg órfão não libera concorrência antecipadamente. Execução usa `timeout --signal=TERM --kill-after=5s`, limite `max(300 s, 30×duração)`, prioridade `nice +10`, encode/filtros com uma thread, ffprobe com 30 s. Essas ferramentas são dependências Linux do runtime. Não é prova de segurança de desempenho no Raspberry Pi mais fraco.

Limpeza remove segmentos, intermediários, final, thumbnail e assets apenas depois de FINALIZED. Manifesto permanece. DEV, FAILED, BLOCKED e arquivos desconhecidos/corrompidos não são apagados automaticamente. `invalid_manifests` e `legacy_pending` expõem itens que exigem intervenção.

## Fila antiga e rollback

A importação roda no monitor em background, sem atrasar o início dos supervisores de captura. O importador examina MP4/TS em `queue_raw` e `failed_clips`, reconhece sidecars v1/v2 e mantém os originais até finalizar. Vídeos já concatenados entram como `ASSEMBLED`, nunca como conjuntos de segmentos. TS identificado como já processado exige inspeção do container e permanece na origem com impedimento explícito; MP4 processado é reutilizado; upload concluído mantém recibo/ID para finalizar. Arquivo final ausente, hash de recibo divergente, formato desconhecido e etapa ambígua permanecem bloqueados na origem, com evento. Importação usa UUID determinístico por origem e marcador `.deferred.json`; não transforma cadastros remotos em novos trabalhos por retry. Sidecars DEV preservados não são reenviados.

Desligar `deferredEnabled` nesta versão exige restart: novos cliques voltam à montagem imediata; enquanto existir `.deferred`, o consumidor compatível continua responsável pela fila antiga/nova e compartilha o lock pesado com essa montagem. Trabalhos v3 já preservados mantêm sua política de agendamento. Não há processamento paralelo com `ProcessingWorker` nesses equipamentos.

Não fazer downgrade binário para uma versão sem suporte v3 com pendências. Para rollback binário, drenar/finalizar ou preservar volume íntegro offline e manter esta versão responsável pela recuperação. Marcadores e manifestos não devem ser removidos para forçar releitura por worker antigo. Nenhuma migração destrutiva é executada no startup.

## MQTT de configuração

Mantidos `config/desired`, `config/reported`, `config/request`, `config/state`, HMAC, escopo, `correlation_id`, versão e hash existentes. O contrato existente é **snapshot completo**, não PATCH: o backend deve preservar campos não editados antes de assinar. Um desired antigo sem campos diferidos já ativos é rejeitado, evitando desativação silenciosa por aplicativo desatualizado.

Versão menor que aplicada/pendente é rejeitada; mesma versão/hash devolve resultado efetivo sem reaplicar; mesma versão com hash diferente é conflito. Campos de agenda passam pela allowlist e validação. `deferredEnabled` requer restart e é rejeitado para rental. Janela adicional é hot reload; nenhuma configuração pode remover a madrugada ou alterar identidade/credenciais.

`config.transaction.json` recupera interrupção entre persistência do env, pending, config e state. A promoção e a leitura da configuração compartilham lock. `applied` só após gravação e promoção efetiva; `pending_restart` não significa aplicada. Reconexão/requisição de estado reconcilia resposta perdida. Reenvio da mesma versão conserva `issued_at`/hash, pois `updatedAt` integra o hash existente; após expiração, reconciliar por `config/request` ou emitir nova versão, sem reutilizar a versão com outro hash. Backend deve manter essa distinção ao correlacionar reports: hash de configuração pendente não comprova aplicação. Backend/aplicativo aceitam os novos campos e autorizam client apenas para `additionalWindows`, no seu escopo; ativação continua controle administrativo de liberação.

## Eventos e estado: contrato implementado, sujeito à homologação integrada

O edge implementa transporte MQTT real sobre o cliente existente; não ativa RabbitMQ. Configuração permanece no protocolo acima. Telemetria usa namespace resolvido pelo `topic_for` existente, com extensões explicitamente novas:

| Canal | Comportamento |
|---|---|
| `grn/devices/{device_id}/capture/events` | Evento `schema_version=2`, QoS 1, não retido |
| `grn/devices/{device_id}/capture/events/ack` | ACK aplicativo assinado após commit/deduplicação na API |
| `grn/devices/{device_id}/state` | Snapshot `type=device.operational_state`, v2, não retido; backend precisa discriminar do estado de presença legado |
| `grn/devices/{device_id}/state/ack` | ACK aplicativo do hash exato do snapshot |

Evento contém `event_id` UUID, `sequence` persistente, `type`, identidades, `job_id`/`trigger_id`/`captured_at` quando disponíveis, `occurred_at`, `stage`, `code`, `severity`, `attempt`, `impact`, `situation` (`open|resolved`) e `incident_id`. Payloads não recebem exceções, headers, URLs assinadas ou credenciais. Repetição idêntica por incidente/código/situação é limitada a uma ocorrência por 60 s no processo; novos estados/recuperações continuam eventos distintos. A cópia em outbox mantém o mesmo ID em cada retry.

Assinatura v2: `signature_version=hmac-sha256-v2`; `content_hash` é SHA-256 hexadecimal do JSON UTF-8 canônico (`sort_keys`, sem espaços, `ensure_ascii=False`), excluindo somente `signature` e `content_hash`. `signature` é HMAC-SHA256/base64 de `v2:OPERATIONAL:<content_hash>`, com o segredo do device. O ACK usa a mesma função e deve conter identidades idênticas, `event_id`, `ack_hash` do payload persistido e `status=persisted|duplicate`; outros status não sincronizam. ACK de snapshot antigo não remove o atual. Implementação de referência: `sign_operational`; fixture de backend simulado nos testes.

Publicação MQTT, PUBACK e `publish_json=True` não removem pendência. Sem extensão no backend, mensagens permanecem locais; não existe sucesso fictício. Retry independente da agenda de vídeos: no máximo uma publicação/s, backoff 30 s exponencial até 900 s com jitter ±20%. Reconexão reutiliza subscriptions do cliente e repete IDs. Envio ocorre em thread própria.

Outbox: 64 MiB, dos quais 8 MiB reservados a falha/recuperação. Na saturação, não apaga não confirmados: registra contador `suppressed`, estado `saturated` e erro local; detalhes do trabalho continuam no manifesto quando possível. Não há promessa de persistência com disco indisponível. Histórico confirmado: até 32 MiB ou sete dias; snapshots atuais são substituíveis, eventos não. Último ACK persiste. Limites são escolhas operacionais iniciais a validar.

Snapshot fornece até 50 impedimentos atuais por trabalho (`issues`, mais `issues_total`/`issues_truncated`), filas por estado, idade da pendência mais antiga, execução atual, volumes/bytes livres, configuração aplicada, inatividade, manifestos inválidos, importações pendentes e saúde da outbox. Backend deve persistir histórico separado do estado atual e aceitar apenas sequência crescente por dispositivo para atualizar projeção corrente. Evento atrasado não pode reabrir alerta já resolvido; snapshot e sequência são autoritativos. `delivery.finalized` resolve incidentes anteriores do trabalho e `processing.recovered` resolve suas falhas de mídia; não interpretar a ausência em uma lista truncada como confirmação individual de recuperação. RBAC da API deve limitar cliente às próprias instalações e filtrar detalhes administrativos.

## Armazenamento e segurança de execução

Alerta estrito `free_bytes < 4_000_000_000` (**GB decimal**, não GiB). A fronteira exata não abre alerta. Monitor a cada 30 s mede `st_dev` dos caminhos reais de staging, finais/legados, telemetria e logs; volumes repetidos são medidos uma vez. Recuperação: dois polls consecutivos com pelo menos 4.500.000.000 bytes (histerese para evitar oscilação), com evento resolved. Não inicia processamento extra por disco baixo.

Antes de copiar/encodar: reserva básica 256 MiB; estimativa de preservação baseada nos segmentos anteriores e proporção pós/pré, mais 8 MiB para assets; reservas de preservações concorrentes são consideradas. Antes da mídia: orçamento adicional de 3,6×bytes de entrada; monitora reserva a cada segundo enquanto FFmpeg roda. Reserva insuficiente bloqueia a operação e mantém pendências. Memória disponível deve ser pelo menos 256 MiB. São estimativas conservadoras iniciais, não garantia para todo bitrate/CRF/cgroup; a qualificação no hardware deve ajustá-las explicitamente.

Upload pronto não depende da agenda e pode liberar espaço após confirmação final. Não há limpeza de pendências para acomodar outro trabalho. Hash só é calculado depois de existir o vídeo final; a URL assinada só é obtida no envio.

## Dependências de ingestão e liberação

Mantido: `POST /api/videos/metadados/client/:clientId/venue/:venueId` → PUT direto S3 → `POST /api/videos/:videoId/uploaded`. HMAC usa timestamp/nonce da requisição atual; `captured_at` é o gatilho original. A API mantém validação de storage antes de concluir.

Na versão de backend auditada, `requestTimeWindow` já avalia `captured_at`, com fallback comercial 07:00–23:30; a descrição de restrição ao horário de envio estava desatualizada. Ainda é obrigatório validar e documentar esse contrato em todos os modos, biblioteca, chave de storage, retenção e cobrança para captura no dia anterior. A rejeição específica de horário passa a BLOCKED no v3, preservando o lance; 401/403 de autenticação continuam terminais. Nenhum bypass de autorização.

Deduplicação atual da API: venue + SHA-256 + clipMode. Timeout ambíguo de registro reutiliza hash/ID; conflito de identidade/409 fica bloqueado para reconciliação, sem inventar endpoint. Backend precisa cobrir reenvio após finalização cuja resposta se perdeu, renovação de URL sem duplicata e commit/deduplicação/ACK de eventos. Não registrar novamente deliberadamente um clipe já concluído.

Backend e frontend implementam schemas/RBAC, configuração, eventos/estado com ACK, reconciliação de ingestão, agenda e UI. Sequência restante: contrato integrado em ambiente isolado com interrupções/reconexões/rollback; equipamento mais fraco com captura ativa; ativação administrativa controlada. Não usar DEV como feature flag.

## Validação

Testes novos: `tests.test_deferred_processing`, `tests.test_deferred_recovery`, `tests.test_deferred_media_integration`. Cobrem agenda e inatividade, pin/cópias sobrepostas, arquivo aberto, cobertura temporal, recuperação, fila antiga v1/v2, upload/finalização separados, rejeição de horário/auth, outbox/ACK, disco e saturação. Integração FFmpeg gera mídia sintética em diretório temporário, sem câmera/rede; exige ffmpeg/ffprobe/timeout/nice. Testes reais de câmera continuam opt-in e não foram usados para qualificação de desempenho desta etapa.

Antes de produção, medir duração por etapa (preservação/concat/encode/thumbnail/registro/PUT/finalize), latência de gatilho, frames perdidos e reinícios da captura, CPU/RSS/temperatura, I/O, máximo simultâneo de disco, crescimento/idade das filas e tempo de recuperação de rede. Comparar HQ/light e câmeras simultâneas no hardware mínimo com carga sustentada; não há resultados de desempenho presumidos.

### Verificação desta etapa (22/09/2026)

Base auditada: `c879623`. Suíte completa: **390 passed, 2 skipped, 1 failed**; cobertura com branches de domínio/aplicação **92,83%** (gate ≥90%). A falha é `test_command_handler_matches_legacy_rejection_payload`, em `tests/test_device_application_contracts.py`: o dispatcher ativo ignora comando sem assinatura, enquanto o adapter antigo produz rejeição de fase 1. A mesma falha foi reproduzida numa extração isolada do HEAD original, sem estas mudanças; não foi mascarada nem se alterou segurança de comandos para satisfazê-la. Smoke adicional da inicialização assíncrona passou após retirar importação síncrona do bootstrap.

Ruff 0.9.10 (`check` e `format --check` nas camadas de arquitetura), mypy 1.15.0 estrito (43 arquivos), `bash -n env_to_config.sh` e `git diff --check`: aprovados. Foram usadas as versões já fixadas em `requirements-dev.txt`. A mídia FFmpeg e o processo órfão foram exercitados com arquivos/processos sintéticos; nenhuma câmera ou serviço de produção foi acessado.

Comando da suíte com ambiente isolado e sem carregar `.env` local:

```bash
env -i PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  GN_CONFIG_PATH=/tmp/gn-no-runtime-config.json .venv/bin/python - <<'PY'
import io, logging, os, tempfile
with tempfile.TemporaryDirectory(prefix="gn-tests-") as root:
    os.environ["GN_LOG_DIR"] = root
    os.environ["COVERAGE_FILE"] = root + "/.coverage"
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    logging.disable(logging.CRITICAL)
    import coverage, pytest
    code = pytest.main(["-q", "-o", "addopts=--strict-config --strict-markers --cov=src --cov-branch --cov-report="])
    cov = coverage.Coverage(data_file=os.environ["COVERAGE_FILE"])
    cov.load()
    total = cov.report(include=["src/domain/*", "src/application/*"], file=io.StringIO())
    print(f"Architecture coverage: {total:.2f}%")
    raise SystemExit(code if total >= 90 else 2)
PY
.venv/bin/ruff check src/domain src/application tests/test_architecture_boundaries.py
.venv/bin/ruff format --check src/domain src/application tests/test_architecture_boundaries.py
.venv/bin/mypy src/domain src/application
bash -n env_to_config.sh
git diff --check
```
