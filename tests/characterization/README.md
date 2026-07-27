# Legacy contract characterization

These tests freeze externally observable behavior before the clean-architecture
migration. They deliberately call the current public modules and normalize only
values that are inherently unstable (temporary roots and timestamps).

| Contract | Classification | Covered behavior |
| --- | --- | --- |
| Enqueue sidecar | PRESERVE | File move, sidecar name, fields and legacy status |
| API error policy | PRESERVE | Backend messages that delete or retain local media |
| Multi-camera paths | PRESERVE | Isolated paths and camera-qualified file names |
| Request signing | PRESERVE | Canonical string, headers and HMAC signature |
| FFmpeg capture | PRESERVE | Default RTSP command and segment output pattern |

The expected values live in `tests/characterization/fixtures/legacy_contracts.json`.
Update that fixture only when an intentional contract change has been approved.

