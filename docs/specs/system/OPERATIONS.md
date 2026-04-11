# Edge Operations

O repositório usa `unittest` como framework de teste.

## Startup behavior

No startup, o serviço:

- carrega env;
- resolve câmeras ativas;
- inicializa MQTT antes das câmeras, quando habilitado e com `DEVICE_ID` válido;
- publica presença/heartbeat/state em modo degradado mesmo que nenhuma câmera suba;
- limpa buffer local por câmera;
- tenta iniciar FFmpeg e `SegmentBuffer` por câmera sem abortar o processo em caso de falha;
- marca câmeras indisponíveis com `camera_status=UNAVAILABLE` e erro sanitizado no snapshot MQTT;
- inicia supervisor por câmera em background para reiniciar FFmpeg com backoff;
- inicia um `ProcessingWorker` por câmera;
- inicializa listeners de ENTER/GPIO/Pico.

Falha de hardware de câmera, rede do broker ou API não deve ser condição bloqueante do runtime principal. O container deve permanecer vivo enquanto o processo Python estiver executando; saúde de câmera é reportada por payload MQTT e logs.

## Shutdown behavior

- listeners e workers usam `Event`/threads daemon;
- locks e threads devem ser liberados sem deixar arquivo `.lock` preso;
- o sistema deve tolerar interrupção sem corromper a fila local.

## Logging

Logs principais:

- `src/utils/logger.py`
- logs de trigger, build, worker, API e falhas HMAC

Práticas:

- truncar assinatura HMAC;
- não expor `DEVICE_SECRET`;
- sanitizar `upload_url`/URLs assinadas antes de persistir respostas de backend em sidecars de retry;
- sanitizar credenciais RTSP antes de gravar comandos FFmpeg em `logs/ffmpeg_<camera_id>.log`;
- registrar contexto suficiente para retry e auditoria local.
- manter `mqtt.log` separado para heartbeat/presença e evitar ruído em `app.log`.

## Test coverage present

Testes visíveis:

- `test_api_error_policy.py`
- `test_capture_ffmpeg_command.py`
- `test_connection_logs.py`
- `test_device_utils.py`
- `test_dual_watermark_command.py`
- `test_mobile_format.py`
- `test_camera_watermark_integration.py`
- `test_legacy_compatibility.py`
- `test_multi_camera_settings.py`
- `test_no_file_collisions.py`
- `test_pico_utils.py`
- `test_retry_upload.py`
- `test_security_signing.py`
- `test_trigger_fanout.py`
- `test_trigger_sources.py`
- `test_worker_multi_camera.py`
- `test_mqtt_settings.py`
- `test_device_presence_service.py`
- `test_mqtt_commands.py`
- `test_device_config_service.py`

Esses testes cobrem os pontos mais sensíveis do edge:

- assinatura HMAC;
- source resolution;
- multi-camera;
- montagem de comando FFmpeg;
- fan-out e colisão de arquivos;
- política de erro da API;
- geracao real de mp4 final a partir da camera configurada, sem Docker, quando `GN_RUN_CAMERA_INTEGRATION=1`.
- composição mobile/vertical do FFmpeg.
- bootstrap e payload mínimo de presença MQTT.
- startup resiliente com MQTT antes das câmeras e câmera não-fatal.
- bloqueio explícito de command/control na fase 1.
- validação, persistência pending/applied e reports da configuração remota.

## Known operational caveats

- a fila é baseada em polling, não eventos;
- o sistema depende fortemente do filesystem local;
- conectividade com backend afeta retry e descarte;
- o worker é sensível a sidecars inconsistentes;
- há `.pyc` em `tests/__pycache__` e `src/__pycache__` no workspace, mas não fazem parte do contrato de runtime;
- MQTT pode ficar habilitado sem broker disponível; isso deve degradar para warning/log e seguir com o pipeline principal.
- câmera pode ficar `UNAVAILABLE` no boot; o supervisor tenta reiniciar FFmpeg sem exigir restart do container.
- remote config depende de `DEVICE_SECRET`/`GN_DEVICE_SECRET` para validar assinatura; sem segredo, mensagens `config/desired` são rejeitadas.

## Audit cautions

Verdades do código atual que costumam ser esquecidas:

- o edge usa um worker por câmera, não um worker único global;
- `GN_HMAC_DRY_RUN` é útil para auditoria sem chamada remota;
- `build_highlight()` espera o pós-buffer antes de concatenar;
- em light mode o SHA256 pode ser adiado/evitado no enqueue;
- `request_outside_allowed_time_window` é descarte local, não retry;
- falhas HMAC/client mismatch também devem sair do loop de retry;
- `HTTP 409` de reupload bloqueado também é descarte local;
- `DEV=true` preserva artefatos locais com status `dev_local_preserved`.

## Change checklist

Antes de alterar fluxos centrais do edge, valide:

1. a mudança preserva o contrato de fila local;
2. locks `.lock` continuam seguros;
3. o trigger continua respeitando janela horária e cooldown;
4. a política de erro da API continua distinguindo retry vs delete;
5. o comportamento de multi-camera continua isolado por câmera;
6. a spec especializada correspondente foi atualizada.
7. `README.md`, `.env.example` e `AGENTS.md` foram atualizados quando houve mudança de contrato operacional;
8. qualquer doc impactada pelo change foi atualizada no mesmo patch.
