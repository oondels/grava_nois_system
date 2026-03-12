# Edge Lookup

Este arquivo é a porta de entrada do edge system para leitura humana e lookup por code agents. O objetivo é localizar a área certa antes de abrir `main.py`, worker e pipeline completo.

## Edge Overview

- Keywords: edge, captura, raspberry pi, replay, upload, python
- File: [ARCHITECTURE.md](./ARCHITECTURE.md)
- Related: bootstrap, runtime, módulos principais, responsabilidades

## Service Bootstrap

- Keywords: main, bootstrap, worker, trigger, fan-out, startup
- File: [ARCHITECTURE.md](./ARCHITECTURE.md), [PIPELINE.md](./PIPELINE.md)
- Source: [`main.py`](../../../main.py)
- Related: camera runtimes, executor, stdin, GPIO, Pico, business hours

## Capture and Buffer

- Keywords: ffmpeg, buffer, segment buffer, rtsp, v4l2, highlight
- File: [PIPELINE.md](./PIPELINE.md), [ARCHITECTURE.md](./ARCHITECTURE.md)
- Source: [`src/video/capture.py`](../../../src/video/capture.py), [`src/video/buffer.py`](../../../src/video/buffer.py), [`src/video/processor.py`](../../../src/video/processor.py)
- Related: pre/post segments, concat, local files

## Trigger Sources

- Keywords: gpio, pico, serial, enter, trigger source, business hours
- File: [BUSINESS_RULES.md](./BUSINESS_RULES.md), [PIPELINE.md](./PIPELINE.md)
- Source: [`main.py`](../../../main.py), [`src/utils/pico.py`](../../../src/utils/pico.py), [`src/utils/time_utils.py`](../../../src/utils/time_utils.py)
- Related: cooldown, GN_TRIGGER_SOURCE, GN_PICO_PORT

## Worker and Queue

- Keywords: queue_raw, processing worker, retry, failed_clips, sidecar, lock
- File: [PIPELINE.md](./PIPELINE.md), [OPERATIONS.md](./OPERATIONS.md)
- Source: [`src/workers/processing_worker.py`](../../../src/workers/processing_worker.py)
- Related: watermark, upload, finalize, retry policy

## Backend API Integration

- Keywords: api client, register metadata, signed url, finalize, hmac
- File: [INTEGRATIONS.md](./INTEGRATIONS.md), [BUSINESS_RULES.md](./BUSINESS_RULES.md)
- Source: [`src/services/api_client.py`](../../../src/services/api_client.py), [`src/security/request_signer.py`](../../../src/security/request_signer.py)
- Related: DEVICE_ID, DEVICE_SECRET, GN_API_BASE, request signing

## API Error Policy

- Keywords: request_outside_allowed_time_window, signature_mismatch, client_mismatch, delete local record
- File: [BUSINESS_RULES.md](./BUSINESS_RULES.md), [OPERATIONS.md](./OPERATIONS.md)
- Source: [`src/services/api_error_policy.py`](../../../src/services/api_error_policy.py)
- Related: retry vs delete, non-retriable auth failures

## Config and Environment

- Keywords: env, settings, multi camera, rtsp urls, cameras json, light mode
- File: [INTEGRATIONS.md](./INTEGRATIONS.md)
- Source: [`src/config/settings.py`](../../../src/config/settings.py)
- Related: GN_CAMERAS_JSON, GN_RTSP_URLS, GN_LIGHT_MODE, GN_MAX_ATTEMPTS

## Tests

- Keywords: pytest, hmac, trigger, multi camera, retry, ffmpeg command
- File: [OPERATIONS.md](./OPERATIONS.md)
- Source: [`tests`](../../../tests)
- Related: regression, edge safety checks

## Suggested Reading Order

- Geral: [ARCHITECTURE.md](./ARCHITECTURE.md) -> [PIPELINE.md](./PIPELINE.md)
- Trigger e captura: [PIPELINE.md](./PIPELINE.md) -> [BUSINESS_RULES.md](./BUSINESS_RULES.md)
- API/HMAC: [INTEGRATIONS.md](./INTEGRATIONS.md) -> [BUSINESS_RULES.md](./BUSINESS_RULES.md)
- Operação e falhas: [OPERATIONS.md](./OPERATIONS.md)
