#!/usr/bin/env python3
"""Opt-in real-camera/deferred-media + local MQTT protocol qualification.

Uses runtime modules, not main.py. Never uploads or claims HTTP/API E2E.
Artifacts and JSON evidence remain in an explicitly new output directory.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-camera", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="/dev/video0")
    parser.add_argument("--clips", type=int, default=30)
    parser.add_argument("--soak-seconds", type=int, default=0)
    parser.add_argument("--resolution", default="1280x720")
    parser.add_argument("--fps", type=int, default=20)
    args = parser.parse_args()
    if args.clips < 1 or args.soak_seconds < 0:
        parser.error("positive clips / nonnegative soak required")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    # No ambient identity, cameras, credentials, dotenv, URLs or deployment flags.
    executable_path = os.environ.get("PATH", "/usr/bin:/bin")
    os.environ.clear()
    os.environ.update(
        PATH=executable_path,
        PYTHON_DOTENV_DISABLED="1",
        GN_LOG_DIR=str(output / "logs"),
        GN_RUNTIME_CONFIG_DIR=str(output / "runtime_config"),
        GN_CONFIG_PATH=str(output / "config.json"),
        DEV="true",
        DEV_USE_CAMERA="true",
        GN_API_URL="http://127.0.0.1:1",
        GN_INPUT_FRAMERATE=str(args.fps),
        GN_VIDEO_SIZE=args.resolution,
        GN_RTSP_URL="",
        GN_PICO_DOCKER_ACTIONS_ENABLED="false",
        GN_REMOTE_DEVICE_COMMANDS_ENABLED="true",
    )
    sys.path.insert(0, str(REPO))
    os.chdir(output)
    import paho.mqtt.client as paho
    from src.bootstrap.deferred_runtime import DeferredRuntime
    from src.config.config_loader import reset_config_cache
    from src.config.settings import CaptureConfig, MQTTConfig
    from src.domain.delivery import ClipJobState
    from src.infrastructure.filesystem.deferred_repository import (
        DeferredJobRepository,
        exclusive_file,
    )
    from src.security.env_control import sign_control, validate_control
    from src.security.env_envelope import seal_env_envelope
    from src.services.docker_action_request import DockerActionRequestService
    from src.services.mqtt.command_dispatcher import CommandDispatcher
    from src.services.mqtt.command_executor import CommandExecutor
    from src.services.mqtt.device_env_service import DeviceEnvService
    from src.services.mqtt.mqtt_client import MQTTClient
    from src.services.mqtt.operational_event_service import sign_operational
    from src.video.buffer import SegmentBuffer
    from src.video.capture import start_ffmpeg
    from src.video.processor import ffprobe_metadata

    started = time.monotonic()
    identity = {
        "device_id": "e2e-camera-local",
        "client_id": "e2e-client-local",
        "venue_id": "e2e-venue-local",
    }
    secret = "synthetic-local-device-key-no-production"
    prefix = "grn/devices/" + identity["device_id"] + "/"
    report = {
        "scope": "real-camera/runtime-modules/local-mqtt; API/S3 not exercised",
        "started_at": datetime.now(UTC).isoformat(),
        "output": str(output),
        "camera": args.device,
        "resolution": args.resolution,
        "fps": args.fps,
        "target_clips": args.clips,
        "target_soak_seconds": args.soak_seconds,
        "checks": [],
        "clips": [],
        "samples": [],
        "status": "running",
    }
    children = []
    runtime = capture = buffer = mqtt = backend = env_service = dispatcher = None
    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def save():
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        tmp = output / "report.partial.json"
        tmp.write_text(json.dumps(report, indent=2))
        tmp.replace(output / "report.json")

    def check(value, name):
        if not value:
            raise AssertionError(name)
        report["checks"].append(name)
        save()

    def wait(predicate, seconds=15):
        end = time.monotonic() + seconds
        while not predicate():
            if stopping:
                raise InterruptedError("requested stop")
            if time.monotonic() > end:
                raise TimeoutError("local integration condition")
            time.sleep(0.05)

    config = {
        "capture": {
            "v4l2": {"device": args.device, "framerate": args.fps, "videoSize": args.resolution}
        },
        "operationWindow": {"timeZone": "UTC", "start": "00:00", "end": "23:59"},
        "processing": {
            "deferredEnabled": True,
            "additionalWindows": [
                {"weekdays": [1, 2, 3, 4, 5, 6, 7], "start": "00:00", "end": "23:59"}
            ],
            "hqPreset": "veryfast",
            "hqCrf": 20,
            "lmPreset": "ultrafast",
            "lmCrf": 28,
            "lightMode": False,
            "verticalFormat": False,
        },
    }

    def write_config(light):
        config["processing"]["lightMode"] = light
        (output / "config.json").write_text(json.dumps(config))
        reset_config_cache()

    write_config(False)
    try:
        device_name = Path("/sys/class/video4linux") / Path(args.device).name / "name"
        report["camera_name"] = (
            device_name.read_text().strip() if device_name.exists() else "unknown"
        )
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            port = bound.getsockname()[1]
        (output / "mosquitto.conf").write_text(
            f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
        )
        broker_log = (output / "broker.log").open("w")
        broker = subprocess.Popen(
            ["mosquitto", "-c", str(output / "mosquitto.conf")],
            stdout=broker_log,
            stderr=subprocess.STDOUT,
        )
        children.append(broker)
        report["broker_port"] = port
        (output / "processes.json").write_text(
            json.dumps({"harness": os.getpid(), "broker": broker.pid})
        )

        def broker_ready():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return True
            except OSError:
                return False

        wait(broker_ready)
        messages = []
        backend = paho.Client(client_id="fixture-" + uuid.uuid4().hex)
        backend.on_connect = lambda client, userdata, flags, rc: client.subscribe(
            prefix + "#", qos=1
        )
        backend.on_message = lambda client, userdata, message: messages.append(
            (message.topic, json.loads(message.payload))
        )
        backend.connect("127.0.0.1", port)
        backend.loop_start()
        mqtt = MQTTClient(
            MQTTConfig(
                True,
                "127.0.0.1",
                port,
                None,
                None,
                "edge-" + uuid.uuid4().hex,
                30,
                5,
                "grn",
                1,
                False,
                False,
                "e2e-local",
            )
        )
        mqtt.start()
        wait(lambda: mqtt.is_connected)
        cfg = CaptureConfig(
            "webcam",
            output / "buffer",
            output / "recorded_clips",
            output / "queue_raw",
            output / "failed_clips",
            source_type="v4l2",
            device=args.device,
            pre_segments=2,
            post_segments=1,
            pre_seconds=2,
            post_seconds=1,
            scan_interval=0.1,
            max_buffer_seconds=12,
            track_segments=True,
        )
        cfg.ensure_dirs()
        runtime = DeferredRuntime(
            base=output,
            cameras={},
            mqtt_client=mqtt,
            topic_for=lambda suffix: prefix + suffix,
            identity=identity,
            secret=secret,
            watermark=REPO / "files/replay_grava_nois_wm.png",
            client_watermark=None,
            top_watermark=None,
            dev_mode=True,
        )
        runtime.events.start()
        # Event backend is deliberately a signed fixture, not a database/API claim.
        runtime.events.emit(
            "processing.failed", code="local_receipt_check", incident_id="receipt-test"
        )
        wait(lambda: any(topic == prefix + "capture/events" for topic, _ in messages))
        payload = next(payload for topic, payload in messages if topic == prefix + "capture/events")
        event_file = runtime.events.outbox / f"{payload['event_id']}.json"
        check(event_file.exists(), "real MQTT delivery retains outbox without application ACK")
        ack = {
            **identity,
            "event_id": payload["event_id"],
            "ack_hash": payload["content_hash"],
            "status": "persisted",
        }
        ack["signature"] = "invalid"
        backend.publish(prefix + "capture/events/ack", json.dumps(ack), qos=1).wait_for_publish()
        time.sleep(0.2)
        check(event_file.exists(), "forged ACK does not remove outbox")
        receipt_dir = output / "fixture-receipts"
        receipt_dir.mkdir()
        receipt = receipt_dir / f"{payload['event_id']}.json"
        with receipt.open("w") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        ack.pop("signature")
        ack["signature"] = sign_operational(ack, secret)
        backend.publish(prefix + "capture/events/ack", json.dumps(ack), qos=1).wait_for_publish()
        wait(lambda: not event_file.exists())
        check(receipt.exists(), "signed fixture ACK after fsync removes delivered outbox")
        env_path = output / "managed.env"
        env_path.write_text("LOCAL=original\n")
        actions = DockerActionRequestService(
            enabled=False,
            request_path=output / "actions/request.json",
            pull_token="PULL",
            restart_token="RESTART",
        )
        env_service = DeviceEnvService(
            mqtt,
            device_id=identity["device_id"],
            client_id=identity["client_id"],
            venue_id=identity["venue_id"],
            request_topic=prefix + "env/request",
            desired_topic=prefix + "env/desired",
            reported_topic=prefix + "env/reported",
            env_path=env_path,
            device_secret=secret,
            ledger_dir=output / "env-ledger",
            action_service=actions,
        )
        env_service.start()

        def control(kind):
            now = datetime.now(UTC)
            value = {
                "type": kind,
                "device_id": identity["device_id"],
                "request_id": str(uuid.uuid4()),
                "issued_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=120)).isoformat(),
            }
            if kind == "env.desired":
                value.update(
                    restart_after_apply=False,
                    envelope=seal_env_envelope(
                        secret, value["request_id"], identity["device_id"], "LOCAL=updated\n"
                    ),
                )
            return sign_control(secret, value)

        snapshot = control("env.request")
        backend.publish(prefix + "env/request", json.dumps(snapshot), qos=1).wait_for_publish()

        def env_reports(request_id):
            return [
                p
                for t, p in messages
                if t == prefix + "env/reported" and p.get("request_id") == request_id
            ]

        wait(lambda: bool(env_reports(snapshot["request_id"])))
        validate_control(
            secret, env_reports(snapshot["request_id"])[-1], identity["device_id"], "env.reported"
        )
        check(
            env_reports(snapshot["request_id"])[-1]["status"] == "snapshot",
            "env v2 encrypted snapshot over real broker",
        )
        desired = control("env.desired")
        tampered = {**desired, "restart_after_apply": True}
        backend.publish(prefix + "env/desired", json.dumps(tampered), qos=1).wait_for_publish()
        time.sleep(0.25)
        check(
            env_path.read_text() == "LOCAL=original\n", "tampered restart flag rejected over MQTT"
        )
        backend.publish(prefix + "env/desired", json.dumps(desired), qos=1).wait_for_publish()
        wait(lambda: bool(env_reports(desired["request_id"])))
        check(env_path.read_text() == "LOCAL=updated\n", "authenticated env v2 update persisted")
        mtime = env_path.stat().st_mtime_ns
        backend.publish(prefix + "env/desired", json.dumps(desired), qos=1).wait_for_publish()
        time.sleep(0.25)
        check(
            env_path.stat().st_mtime_ns == mtime,
            "duplicate env request does not rewrite managed file",
        )
        dispatcher = CommandDispatcher(
            mqtt,
            device_id=identity["device_id"],
            command_in_topic=prefix + "commands/in",
            command_out_topic=prefix + "commands/out",
            device_secret=secret,
            executor=CommandExecutor(actions),
            ledger_path=output / "commands/ledger.json",
            result_path=output / "commands/result.json",
        )
        dispatcher.start()
        now = datetime.now(UTC)
        command = {
            "type": "device.operation.request",
            "device_id": identity["device_id"],
            "request_id": str(uuid.uuid4()),
            "command": "restart_container",
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=120)).isoformat(),
            "signature_version": "hmac-sha256-v1",
            "parameters": {},
        }
        command["signature"] = dispatcher._sign(command)
        backend.publish(prefix + "commands/in", json.dumps(command), qos=1).wait_for_publish()

        def command_reports():
            return [
                p
                for t, p in messages
                if t == prefix + "commands/out" and p.get("request_id") == command["request_id"]
            ]

        wait(lambda: bool(command_reports()))
        check(
            command_reports()[-1]["status"] == "failed",
            "disabled host runner reports failed rather than accepted",
        )
        capture = start_ffmpeg(cfg)
        children.append(capture)
        (output / "processes.json").write_text(
            json.dumps({"harness": os.getpid(), "broker": broker.pid, "capture": capture.pid})
        )
        buffer = SegmentBuffer(cfg)
        buffer.start()
        wait(lambda: len(buffer.snapshot_last(8)) >= 5, seconds=30)
        check(capture.poll() is None, "physical camera continuous capture active")
        last_sample = 0.0
        for index in range(args.clips):
            if stopping:
                raise InterruptedError("requested stop")
            light = bool(index % 2)
            write_config(light)
            before = time.monotonic()
            job_id = runtime.admit(
                cfg, buffer, str(uuid.uuid4()), datetime.now(UTC).isoformat(), time.monotonic()
            )
            wait_end = time.monotonic() + 45
            while job_id in runtime.preservation.pending and time.monotonic() < wait_end:
                runtime.preservation.tick()
                time.sleep(0.1)
            job = runtime.jobs.get(job_id)
            check(
                job.state is ClipJobState.QUEUED,
                f"clip {index + 1} preserved closed camera segments",
            )
            recovery = False
            if index == 0:
                with exclusive_file(runtime.root / "heavy.lock") as lock:
                    metadata = runtime.media.concatenate(job, lock)
                job = replace(
                    job,
                    state=ClipJobState.PROCESSING,
                    details={
                        **job.details,
                        "duration_sec": metadata["duration_sec"],
                        "media_checkpoint": "ASSEMBLED",
                    },
                )
                runtime.jobs.save(job)
                recovered = DeferredJobRepository(runtime.root)
                recovered.recover()
                check(
                    recovered.get(job_id).state is ClipJobState.ASSEMBLED,
                    "durable restart recovery resumes ASSEMBLED checkpoint",
                )
                recovery = True
            runtime.coordinator.process_media_once()
            job = runtime.jobs.get(job_id)
            check(
                job.state is ClipJobState.WATERMARKED and job.details.get("thumbnail_complete"),
                f"clip {index + 1} media watermark+thumbnail completed",
            )
            final = runtime.jobs.artifact(job, job.artifact_location)
            metadata = ffprobe_metadata(final)
            decoded = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-i", str(final), "-f", "null", "-"],
                capture_output=True,
                timeout=60,
            )
            check(
                decoded.returncode == 0 and not decoded.stderr,
                f"clip {index + 1} fully decodes without errors",
            )
            report["clips"].append(
                {
                    "index": index + 1,
                    "profile": "light" if light else "hq",
                    "job_id": job_id,
                    "bytes": final.stat().st_size,
                    "duration": metadata.get("duration_sec"),
                    "width": metadata.get("width"),
                    "height": metadata.get("height"),
                    "elapsed_seconds": round(time.monotonic() - before, 3),
                    "checkpoint_recovered": recovery,
                    "state": job.state.value,
                }
            )
            runtime.coordinator.deliver_once()
            check(
                runtime.jobs.get(job_id).state is ClipJobState.DEV_PRESERVED,
                f"clip {index + 1} explicitly preserved locally without fake upload",
            )
            save()
            print(
                json.dumps(
                    {
                        "clip": index + 1,
                        "profile": "light" if light else "hq",
                        "elapsed_seconds": report["elapsed_seconds"],
                    }
                ),
                flush=True,
            )
        check(capture.poll() is None, "capture remained active during all media processing")
        report["media_completed_at"] = datetime.now(UTC).isoformat()
        report["status"] = "soaking" if time.monotonic() - started < args.soak_seconds else "passed"
        save()
        while time.monotonic() - started < args.soak_seconds:
            if stopping:
                raise InterruptedError("requested stop")
            if capture.poll() is not None:
                raise RuntimeError("camera stopped during soak")
            diagnostics = buffer.diagnostics(stale_after_sec=5)
            if not diagnostics.buffer_fresh:
                raise RuntimeError("camera buffer stale during soak")
            if time.monotonic() - last_sample >= 30:
                sample = {
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "buffer_segments": diagnostics.segment_count,
                    "segment_age_sec": diagnostics.segment_age_sec,
                    "outbox_pending": len(list(runtime.events.outbox.glob("*.json"))),
                    "load_average": list(os.getloadavg()),
                    "thermal_celsius": [],
                }
                for thermal in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
                    with suppress(OSError, ValueError):
                        sample["thermal_celsius"].append(int(thermal.read_text()) / 1000)
                report["samples"].append(sample)
                save()
                last_sample = time.monotonic()
            time.sleep(1)
        report["status"] = "passed"
        report["soak_completed"] = True
        report["completed_at"] = datetime.now(UTC).isoformat()
        save()
    except BaseException as error:
        report["status"] = (
            "interrupted_by_user" if isinstance(error, InterruptedError) else "failed"
        )
        report["soak_completed"] = False
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        save()
        raise
    finally:
        report["cleanup_errors"] = []
        for name, service in (
            ("buffer", buffer),
            ("env", env_service),
            ("commands", dispatcher),
            ("runtime", runtime),
            ("mqtt", mqtt),
        ):
            if service is not None:
                try:
                    service.stop()
                except Exception as error:
                    report["cleanup_errors"].append(
                        {"resource": name, "type": type(error).__name__}
                    )
        if backend:
            try:
                backend.disconnect()
                backend.loop_stop()
            except Exception as error:
                report["cleanup_errors"].append(
                    {"resource": "fixture", "type": type(error).__name__}
                )
        # These Popen handles belong only to this invocation; no name/global kill.
        for child in reversed(children):
            try:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired) as error:
                report["cleanup_errors"].append({"pid": child.pid, "type": type(error).__name__})
        save()


if __name__ == "__main__":
    main()
