# Edge Operations

## Startup behavior

No startup, o serviço:

- carrega env;
- resolve câmeras ativas;
- limpa buffer local;
- inicia FFmpeg e `SegmentBuffer` por câmera;
- inicia um `ProcessingWorker` por câmera;
- inicializa listeners de ENTER/GPIO/Pico.

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
- `test_security_signing.py`
- `test_trigger_fanout.py`
- `test_trigger_sources.py`
- `test_worker_multi_camera.py`
- `test_mqtt_settings.py`
- `test_device_presence_service.py`
- `test_mqtt_commands.py`

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
- bloqueio explícito de command/control na fase 1.

## Known operational caveats

- a fila é baseada em polling, não eventos;
- o sistema depende fortemente do filesystem local;
- conectividade com backend afeta retry e descarte;
- o worker é sensível a sidecars inconsistentes;
- há `.pyc` em `tests/__pycache__` e `src/__pycache__` no workspace, mas não fazem parte do contrato de runtime;
- existe `docker-compose.yml` no repositório do system com mudança local não relacionada, então commits devem ser isolados.
- MQTT pode ficar habilitado sem broker disponível; isso deve degradar para warning/log e seguir com o pipeline principal.

## Audit cautions

Verdades do código atual que costumam ser esquecidas:

- o edge usa um worker por câmera, não um worker único global;
- `GN_HMAC_DRY_RUN` é útil para auditoria sem chamada remota;
- `build_highlight()` espera o pós-buffer antes de concatenar;
- em light mode o SHA256 pode ser adiado/evitado no enqueue;
- `request_outside_allowed_time_window` é descarte local, não retry;
- falhas HMAC/client mismatch também devem sair do loop de retry.

## Change checklist

Antes de alterar fluxos centrais do edge, valide:

1. a mudança preserva o contrato de fila local;
2. locks `.lock` continuam seguros;
3. o trigger continua respeitando janela horária e cooldown;
4. a política de erro da API continua distinguindo retry vs delete;
5. o comportamento de multi-camera continua isolado por câmera;
6. a spec especializada correspondente foi atualizada.
7. `README.md` e `CHANGELOG.md` foram atualizados quando houve mudança de comportamento.
