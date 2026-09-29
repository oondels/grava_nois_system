# Administrative control and recovery — v2

## Compatibility and rollout

The API, edge and host runner must be upgraded as a compatible set. HTTP routes and
MQTT topic prefixes remain unchanged. Keep remote operations disabled during the
upgrade; successful authenticated `env.request` v2 / `env.reported` v2 sync proves
control compatibility. There is no v1 write fallback. Old edge rejects inner
`version=v2`; new edge ignores unauthenticated/v1 controls without emitting a
misleading rejection receipt. Runtime capture and existing replay recovery remain
independent of this negotiation. RF-011–014 (runtime cutover) remain a later delivery.

## Env control

`env.request`, `env.desired` and `env.reported` use `signature_version=hmac-sha256-v2`.
All fields except `signature` are sorted recursively as UTF-8 JSON without spaces;
HMAC-SHA256/base64 signs `v2:ENV_CONTROL:` followed by that JSON. Required controls:
`type`, `device_id`, canonical UUID `request_id`, `issued_at`, `expires_at`.
Lifetime is at most 120 seconds, expiry is strict and issuance tolerates 30 seconds
in the future. Desired includes signed boolean `restart_after_apply` and the whole
encrypted envelope; inner device/request must match outer. AES-GCM/HKDF derivation
keeps `grn-env-envelope-v1` as HKDF info but inner version and HMAC prefix are `v2`.
Committed Python and TypeScript vectors verify both directions without external files.

Only an authenticated request may produce a correlated report. All reports are
signed, with fresh issue/expiry and original request/result. The API authenticates
before SSE and keeps a 15-minute pending correlation; no plaintext env is stored
in that correlation. Transport publication is not evidence of application.

Edge persists private, fsynced records in `GN_RUNTIME_CONFIG_DIR/env-control` before
writing the env. Snapshot records contain ciphertext, never plaintext. Desired
records contain hashes/phases/results, never content. Duplicate ID/hash returns
its result without another write or restart; conflicting ID/hash is rejected.
Recovery compares the actual file hash. An interruption after durable intent never emits a speculative rejection: recovery first establishes the actual outcome, preserving the immutable request result. If the write cannot be proved, the result
is `interrupted_apply_requires_sync`; recovery never blindly reapplies content.
Completed records expire locally 24 hours after their request expiry. Incomplete
records are preserved. Backups remain private and are included in Golden sanitization.

`applied_requires_restart` confirms the file write only. `restart_requested` and
`restart_status=not_requested|queued|rejected|uncertain` distinguish intent admission.
`queued` means durable host intent, not service readiness. `restart_error_code`
contains a safe reason. No PID-1 termination fallback is used. Results are retried
on reconnect/every 30 seconds through the correlation lifetime, without reexecution.

## Commands and host handoff

Requests keep HMAC over the entire unsigned canonical JSON, signature version v1,
allowlist, UUID, lifetime 120 seconds and 30-second future skew. Commands are opt-in.
Only successful durable host admission yields `accepted`; disabled, busy, expired,
invalid and persistence failures yield explicit errors. Ambiguous persistence yields
`unknown`. New attempts require a new request ID, never automatic invasive retries.

Host IPC lives under the configured request path's parent in `device-actions/`:

- `requests/<uuid>.json`: schema 2, request ID, source (`mqtt|pico`), action,
  `requested_at`, `expires_at`, allowlisted parameters; password uses a private
  transient file referenced by basename only.
- `processing/`: claimed work. The host serializes execution and recovers orphans
  without overwriting another operation or repeating an uncertain effect.
- `results/`: atomic, durable outcome (`ok|error|unknown`, action, ID, completion,
  stage and safe error code).
- `receipts/`: edge confirmation only after durable local copying.

`admission.lock` protects submission/claim transitions; a separate host lock owns
execution. Existing legacy pending request/processing files block new admission;
the new runner reconciles them rather than overwriting them. The Pico token API's
boolean means token consumed, whereas `request_action` means admitted. These are
intentionally separate contracts. New edge requires the new runner; install runner
before sending v2 intents. Legacy v1 Pico migration is implemented by the host.

Edge keeps `device-operations/<request_id>.json` and `outbox/<report_id>.json` under
the persistent runtime directory. It conservatively honors the previous command
ledger during migration. Reports add `type=device.operation.report`, UUID report ID
and `reported_at`; all fields are signed under the existing canonical HMAC v1.
Each state report has a stable ID and retries every 30 seconds. A successful MQTT
publish never removes it. API signs `commands/ack` with `type=device.operation.ack`,
device ID, request ID, report ID, `status=persisted`, signature version v1 and HMAC.
Only matching authenticated ACK durably removes that exact report (including directory fsync). Host receipts do not
substitute for API persistence. Accepted and final reports can be acknowledged in
any order without regressing the operation. Corrupt state is retained for diagnosis and isolated per record so it cannot starve unrelated reports. Every host receipt follows a durable local copy, including unknown/terminal commands and env/config/Pico results; unknown outcomes remain available for reconciliation without losing the host evidence.

API serializes active operations and applies monotonic outcomes. A transport timeout
may remain unknown through the existing 15-minute operational deadline. Execution
terminals do not regress; late outcomes require reconciliation. An interrupted
admission with no provable host intent becomes unknown without automatic replay.
No exactly-once claim is made for a host effect whose outcome cannot be observed.

## Operation and validation

The terminal listener uses interruptible file-descriptor reads and is joined on
shutdown; EOF only disables ENTER. The inactive clean-architecture command adapter
fails closed without phase-one unsigned reports until its separate migration.

Cross-repository regression: `tests.test_host_action_contract` imports the sibling config runner (override `GN_CONFIG_REPO` when needed), uses disposable state and mocks invasive host effects. It covers real IPC, ACK order, corrupted records, copy failure, post-rename interruption and 64-hex Wi-Fi PSK compatibility.

Regression coverage: `tests.test_device_env_service`, `tests.test_env_envelope_cross`,
`tests.test_mqtt_commands`, `tests.test_docker_action_request`, `tests.test_terminal_trigger`.
Use isolated test env: do not load local camera/backend credentials for unit tests.
Keep replay v1/v2/v3, operational outbox and administrative records on persistent
volumes. Rollback must preserve those volumes and must not send v3 jobs to old workers.

Functional qualification on the development PC does not qualify minimum Pi hardware
or physical GPIO/Pico/reboot/Wi-Fi. Those release gates remain explicit. API/frontend
already implement deferred processing; remaining release work is integrated/hardware
validation and any defects it reveals, not rebuilding the feature.

## Local health and Compose

The actual main loop writes a private runtime-health.json every five seconds under
GN_RUNTIME_CONFIG_DIR (default runtime_config in the project/container). The probe
`python -m src.cli.healthcheck` checks freshness (20 seconds), PID and process-start
identity, initialized loop and shutdown state. A live Python PID alone is insufficient.
`--ready` additionally requires fresh buffers and live FFmpeg for every configured
camera; an explicitly empty camera set is valid. The freshness threshold is the
same ten seconds used by supervision. MQTT outage does not turn capture liveness
into failure. Docker reports liveness; the host runner/doctor also check readiness
before declaring service available. The probe never restarts the service itself.

Local Compose uses a dedicated `host_config/.env`, not a writable mount of the whole
checkout. Prepare that private file before running Compose, with the same values
used to generate runtime_config/config.json. The same file supplies container env
and administrative edits. `/run/grn-device-actions` shares transient Wi-Fi secrets
with the installed host runner; running Compose alone does not install that runner.
Native `python main.py` retains its existing root .env workflow. Do not commit either
managed env file. Capture, replay/rental data, logs and runtime state remain mounted
persistently. Updating existing Compose must accompany the compatible edge image;
older images do not provide the new healthcheck CLI.
