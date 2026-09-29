# Verificação local — 29/09/2026

## Resultado observado e interrupção solicitada

A pedido do usuário, a execução contínua e a integração complexa foram interrompidas e ficam sob sua condução. **Não houve homologação de duas horas nem conclusão câmera → API → S3 → frontend.**

Antes da interrupção, o hardware identificado foi a webcam física **ACER HD User Facing**, `/dev/video0`, com suporte MJPEG/YUYV em 1280×720, 640×480 e 640×360. `/dev/video1` identifica a mesma câmera; não foi utilizado como segunda fonte.

| Verificação | Evidência real |
|---|---|
| Captura física contínua | FFmpeg/v4l2 do módulo edge, 1280×720, 20 fps |
| Replays | 30/30: 15 HQ (`crf20/veryfast`) e 15 light (`crf28/ultrafast`) |
| Pipeline | SegmentBuffer → PreserveReplay → manifesto v3 → DeferredCoordinator/DeferredMedia → watermark/thumbnail |
| Integridade de mídia | Todos os 30 arquivos foram decodificados integralmente por FFmpeg sem erro |
| Concorrência | Captura permaneceu ativa durante processamento dos 30 replays |
| Recuperação | Checkpoint persistido PROCESSING/ASSEMBLED reaberto por novo repositório, retomada concluída |
| Tempo por replay HQ | 6,858–7,195 s; média 7,044 s, incluindo preservação/pós-buffer e mídia |
| Tempo por replay light | 5,775–6,051 s; média 5,913 s |
| Lote completo | Aproximadamente 203 s desde inicialização do harness |
| Execução contínua | Interrompida em **477,38 s (7 min 57 s)**; alvo de 7.200 s não concluído |
| MQTT real | Mosquitto temporário, dois clientes reais, loopback |
| `.env` v2 | Snapshot criptografado, controle autenticado, adulteração do restart rejeitada e duplicata sem reescrita |
| Telemetria | PUBACK não removeu pendência; ACK falso ignorado; ACK de fixture assinado após fsync removeu outbox |
| Comandos no primeiro soak | Policy de comandos remotos desabilitada produziu falha; esse resultado **não comprova admissão pelo runner do host** |
| API inicial | JWT real: acesso anônimo admin negado401 e client→admin negado403; próximo sync falhou409 por wiring ausente no helper local |
| API helper posterior | Backend corrigiu export do router, publisher wiring e adicionou tenantB. Não houve novo teste complexo após a interrupção |
| Upload/S3/finalize/browser integrado | **Não executado**; nenhum sucesso de entrega simulado |

A execução usou módulos reais do runtime, sem iniciar `main.py`, GPIO, reboot ou troca de Wi-Fi. A entrega dos replays ficou explicitamente `DEV_PRESERVED`. O ACK MQTT inicial veio de uma fixture que gravou receipt com fsync, **não de PostgreSQL/API**. O smoke Chrome do frontend é separado, com adaptadores HTTP/SSE sintéticos.

Os replays possuem pré-buffer de 2 s e pós-buffer de 1 s; estes resultados não qualificam durações maiores, câmera RTSP de produção, hardware mínimo ou carga contínua de partidas. Não há medição suficiente para afirmar ausência de perda de frames, eficiência térmica de duas horas ou garantia de desempenho em outro equipamento.

## Artefatos preservados e cleanup

- Relatório detalhado: `/tmp/gn-e2e-camera-soak/report.json`, status `interrupted_by_user`, `soak_completed=false`.
- Replays/manifests: `/tmp/gn-e2e-camera-soak/queue_raw/.deferred/`.
- Log: `/tmp/gn-e2e-camera-soak-launch.log`; 10 amostras de acompanhamento no relatório.
- Probe inicial de 2 clipes: `/tmp/gn-e2e-camera-probe/report.json`.
- Probe API incompleto: `/tmp/gn-e2e-api-probe/report.json`.
- Infra sintética e contagens: `/tmp/gn-e2e-infra/infra.json`, `stats.json`; logs API em `/tmp/gn-e2e-api-launch.log` contêm somente identidade/JWT de fixtures descartáveis.

Encerrados com SIGTERM e limpeza graciosa: harness câmera 80899, câmera FFmpeg 80974, broker 80905, infra 84814, API/PG runner 100326. Processos confirmados ausentes; container Redis com label `grn.e2e.disposable=true` removido; portas 32768/42763/49853 fechadas. PostgreSQL preexistente 5432 e Redis preexistente 6379 permaneceram ativos. Nenhum serviço de produção foi acessado.

Os arquivos em `/tmp` são evidências locais temporárias, não artefatos versionados. A coleta de vídeos pode conter imagem do ambiente; mantenha o diretório privado.

## Harness entregue para execução pelo usuário

Arquivos:

- `scripts/e2e_local_camera.py`: opt-in `--allow-camera`, diretório novo, ambiente limpo, captura e 30 replays alternados, checkpoints, MQTT real e soak opcional. A versão entregue habilita policy de comandos remotos com ações host desabilitadas para que o teste futuro examine a rejeição do runner; esta alteração posterior ao primeiro soak recebeu apenas verificação estática.
- `scripts/e2e_local_infra.py`: inicia Redis exclusivo da imagem local `redis:7-alpine` e storage HTTP loopback compatível com PUT/HEAD/GET básico, sem validar assinatura S3. Usa `--pull=never`, porta aleatória, não usa Redis 6379 preexistente. SIGTERM remove somente seu container.
- `scripts/e2e_local_api.py`: harness **ainda não validado integralmente** que conecta módulos MQTT/HTTP reais à API descartável, verifica configuração, autenticação/RBAC/tenant, ACK e transfere cópias dos replays, com perda de resposta finalize e recuperação de registro. O storage é fixture, não AWS real. Artefatos originais não são apagados.

Pré-requisitos: Python com `requirements.txt`, FFmpeg/ffprobe/coreutils, mosquitto, webcam acessível, Docker com imagem Redis já presente; repositório API com Node/dependências e PostgreSQL 16 tools utilizados por `tools/test-device-control-postgres.cjs`. A `.venv` local do edge foi reparada nesta sessão; use `.venv/bin/python` com as dependências instaladas. Os testes históricos acima utilizaram um ambiente temporário antes desse reparo.

Execução manual, em terminais separados, sem reutilizar diretórios anteriores:

```bash
GN_E2E_ROOT=$(mktemp -d /tmp/grn-user-e2e-XXXXXX)
GN_E2E_PYTHON="$PWD/.venv/bin/python"
# Terminal 1: captura; encerre com Ctrl-C se necessário.
"$GN_E2E_PYTHON" scripts/e2e_local_camera.py --allow-camera \
  --output "$GN_E2E_ROOT/camera" --clips 30 --soak-seconds 7200
# Terminal 2: infraestrutura temporária; copie GN_E2E_ROOT do terminal 1.
python3 scripts/e2e_local_infra.py --output "$GN_E2E_ROOT/infra"
```

Com a câmera/broker e a infra ativos, leia `camera/report.json` (`broker_port`) e `infra/infra.json` (`redis_port`, porta de `s3_endpoint`). No repositório API, iniciar o helper com **essas portas loopback**, redirecionando sua saída para arquivo privado:

```bash
# Substitua cada valor pelos números dos JSONs desta execução.
GN_LOCAL_REDIS_PORT=<porta_redis> GN_LOCAL_MQTT_PORT=<porta_broker> \
GN_LOCAL_S3_PORT=<porta_storage> \
node tools/test-device-control-postgres.cjs --serve > "$GN_E2E_ROOT/api.log" 2>&1
```

Esperar `LOCAL_API_READY` no log e 30 clipes completos no relatório da câmera. No repositório edge:

```bash
"$GN_E2E_PYTHON" scripts/e2e_local_api.py \
  --api-log "$GN_E2E_ROOT/api.log" --infra "$GN_E2E_ROOT/infra/infra.json" \
  --camera "$GN_E2E_ROOT/camera" --output "$GN_E2E_ROOT/api-check" --clips 30
```

Ao terminar, Ctrl-C nos processos donos API, infra e câmera; os handlers encerram apenas recursos criados por eles. Não usar `pkill`, `docker stop` genérico, `git clean` ou parar PostgreSQL/Redis preexistentes. Os diretórios de mídia/relatórios ficam preservados para revisão.

O helper SQL usa schema de fixtures a partir das entidades porque as migrations históricas não incluem o baseline original; testa migrations específicas em banco descartável. Portanto, seu sucesso não provaria migração segura do banco de produção. O navegador integrado com API real, atualização PWA existente, teste de perda de frames e soak concluído continuam critérios de aceite do usuário.

Acabamento posterior à interrupção: os três scripts receberam `ruff check --fix`, `ruff format` e validação estática; cleanup continua mesmo se um cliente falhar ao desconectar e registra erros por recurso. Nenhuma integração foi reexecutada após esse acabamento.
