# Repository Guidelines

## Project Structure & Module Organization
Core runtime code lives in `src/`: `src/video/` handles capture, buffering, and highlight generation, `src/workers/` processes queued clips, `src/config/` loads environment-driven settings, and `src/security/` contains signing logic. Tests live in `tests/` and follow the runtime split with focused files such as `test_mobile_format.py` and `test_trigger_fanout.py`. Static assets and watermark images are stored in `files/`. Operational docs and system specs are under `docs/` and `docs/specs/system/`. Runtime artifact directories such as `queue_raw/`, `recorded_clips/`, `highlights_wm/`, and `failed_clips/` are local working folders, not source modules.

## Build, Test, and Development Commands
Set up the local environment with:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Run the edge service locally with `python3 main.py`. Use environment overrides inline when validating pipeline variants, for example `GN_RTSP_USE_WALLCLOCK=1 GN_RTSP_FPS=20 python3 main.py`. Run the main targeted test suite with `python -m unittest tests.test_mobile_format`. For the optional real-camera integration flow, use `GN_RUN_CAMERA_INTEGRATION=1 PYTHONPATH=. python -m unittest tests.test_camera_watermark_integration`.

## Coding Style & Naming Conventions
Use Python with 4-space indentation, type hints where the module already uses them, and small, direct functions. Match existing naming: `snake_case` for functions, variables, and test methods; `PascalCase` for classes; uppercase for environment variables such as `GN_WM_REL_WIDTH`. Keep configuration changes mirrored across `.env.example`, `README.md`, and relevant specs when behavior changes.

## Testing Guidelines
This repository uses `unittest`. Add tests in `tests/` with filenames starting `test_` and methods named `test_*`. Prefer focused regression tests around FFmpeg command generation, queue behavior, retry policy, and environment-flag combinations. If a change affects vertical/mobile output, update `tests/test_mobile_format.py`; if it affects real-device capture, document any opt-in integration coverage.

## Commit & Pull Request Guidelines
Recent history favors short imperative subjects, often with Conventional Commit style such as `feat(system): ...` or `fix(system): ...`, but plain imperative messages are also present. Keep commits scoped to one behavior change. Pull requests should explain the runtime impact, list any new env vars, mention updated docs/specs, and include the exact test commands run.

## Security & Configuration Tips
Never commit `.env` or camera credentials. Treat `.env.example` as the public contract for new settings. Be careful with changes that touch upload, HMAC, or deletion/retry paths; those flows are operationally sensitive.
