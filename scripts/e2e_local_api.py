#!/usr/bin/env python3
"""Bridge real camera artifacts through real edge HTTP and local API/SQL/S3 fixture.

Requires the disposable API bootstrap and e2e_local_infra.py. Does not start
production entrypoints or modify original camera artifacts. All endpoints must
be loopback. Reports contain no JWT or device secrets.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api-log", type=Path, required=True)
    p.add_argument("--infra", type=Path, required=True)
    p.add_argument("--camera", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--clips", type=int, default=30)
    a = p.parse_args()
    if a.clips < 1:
        p.error("positive clip count required")
    ready = [
        line[len("LOCAL_API_READY ") :]
        for line in a.api_log.read_text().splitlines()
        if line.startswith("LOCAL_API_READY ")
    ]
    if not ready:
        raise RuntimeError("disposable API not ready")
    api = json.loads(ready[-1])
    infra = json.loads(a.infra.read_text())
    source = a.camera.resolve()
    camera = json.loads((source / "report.json").read_text())
    for address in (api["url"], infra["s3_endpoint"]):
        if urlparse(address).hostname != "127.0.0.1" or urlparse(address).scheme != "http":
            raise ValueError("only loopback test endpoints permitted")
    if len(camera["clips"]) < a.clips:
        raise ValueError("requested camera clips not yet completed")
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    out.chmod(0o700)
    path = os.environ.get("PATH", "/usr/bin:/bin")
    os.environ.clear()
    os.environ.update(
        PATH=path,
        PYTHON_DOTENV_DISABLED="1",
        GN_LOG_DIR=str(out / "logs"),
        GN_CONFIG_PATH=str(out / "config.json"),
        GN_RUNTIME_CONFIG_DIR=str(out / "runtime"),
        GN_API_BASE=api["url"],
        GN_CLIENT_ID=api["clientId"],
        GN_VENUE_ID=api["venueId"],
        DEVICE_ID=api["deviceId"],
        DEVICE_SECRET=api["deviceSecret"],
        DEV="false",
        GN_PICO_DOCKER_ACTIONS_ENABLED="false",
        GN_REMOTE_DEVICE_COMMANDS_ENABLED="true",
    )
    os.chdir(out)
    sys.path.insert(0, str(REPO))
    import requests
    from src.application.delivery.deferred_coordinator import UTCClock
    from src.application.delivery.process_clip_job import ProcessClipJob
    from src.application.delivery.retry_policy import RetryPolicy
    from src.bootstrap.deferred_runtime import DeferredArtifacts
    from src.config.settings import MQTTConfig
    from src.domain.delivery import ClipJobState
    from src.infrastructure.filesystem.deferred_repository import (
        DeferredJobRepository,
        DeferredLeases,
    )
    from src.infrastructure.http.deferred_gateway import DeferredVideoGateway
    from src.services.api_client import GravaNoisAPIClient
    from src.services.docker_action_request import DockerActionRequestService
    from src.services.mqtt.command_dispatcher import CommandDispatcher
    from src.services.mqtt.command_executor import CommandExecutor
    from src.services.mqtt.device_config_service import DeviceConfigService
    from src.services.mqtt.device_env_service import DeviceEnvService
    from src.services.mqtt.device_presence_service import DevicePresenceService
    from src.services.mqtt.mqtt_client import MQTTClient
    from src.services.mqtt.operational_event_service import OperationalEventService

    report = {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "scope": (
            "real webcam artifacts + real edge services + HTTP API routers/services "
            "+ PostgreSQL + Redis + MQTT; S3 is loopback HTTP fixture"
        ),
        "checks": [],
        "clips": [],
    }
    started = time.monotonic()

    def save():
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        tmp = out / "report.partial.json"
        tmp.write_text(json.dumps(report, indent=2))
        tmp.replace(out / "report.json")

    def check(value, name):
        if not value:
            raise AssertionError(name)
        report["checks"].append(name)
        save()

    def request(method, path, token="admin", **kwargs):
        headers = (
            {}
            if token is None
            else {
                "Authorization": "Bearer "
                + api["adminToken" if token == "admin" else "clientToken"]
            }
        )
        return requests.request(method, api["url"] + path, headers=headers, timeout=15, **kwargs)

    def wait(predicate, seconds=20):
        until = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() > until:
                raise TimeoutError("local API integration condition")
            time.sleep(0.1)

    mqtt = presence = env_service = config_service = events = dispatcher = None

    def stop(_signum, _frame):
        raise InterruptedError("requested stop")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        device = api["deviceId"]
        prefix = "grn/devices/" + device + "/"
        identity = {"device_id": device, "client_id": api["clientId"], "venue_id": api["venueId"]}
        cfg = MQTTConfig(
            True,
            "127.0.0.1",
            camera["broker_port"],
            None,
            None,
            "api-edge-" + uuid.uuid4().hex,
            30,
            2,
            "grn",
            1,
            False,
            False,
            "local-api-e2e",
        )
        mqtt = MQTTClient(cfg)
        presence = DevicePresenceService(
            mqtt,
            cfg,
            device_id=device,
            client_id=api["clientId"],
            venue_id=api["venueId"],
            boot_id=str(uuid.uuid4()),
            runtime_snapshot_provider=lambda: {
                "queue_size": 0,
                "health": {},
                "cameras": [],
                "runtime": {},
            },
        )
        presence.start()
        mqtt.start()
        wait(lambda: mqtt.is_connected)
        presence.publish_online()
        actions = DockerActionRequestService(
            enabled=False,
            request_path=out / "actions/request.json",
            pull_token="PULL",
            restart_token="RESTART",
        )
        env_path = out / "managed.env"
        env_path.write_text("LOCAL=original\n")
        env_service = DeviceEnvService(
            mqtt,
            device_id=device,
            client_id=api["clientId"],
            venue_id=api["venueId"],
            request_topic=prefix + "env/request",
            desired_topic=prefix + "env/desired",
            reported_topic=prefix + "env/reported",
            env_path=env_path,
            device_secret=api["deviceSecret"],
            ledger_dir=out / "env-ledger",
            action_service=actions,
        )
        env_service.start()
        config_service = DeviceConfigService(
            mqtt,
            device_id=device,
            client_id=api["clientId"],
            venue_id=api["venueId"],
            desired_topic=prefix + "config/desired",
            reported_topic=prefix + "config/reported",
            request_topic=prefix + "config/request",
            config_path=out / "config.json",
            env_path=env_path,
            device_secret=api["deviceSecret"],
        )
        config_service.start()
        dispatcher = CommandDispatcher(
            mqtt,
            device_id=device,
            command_in_topic=prefix + "commands/in",
            command_out_topic=prefix + "commands/out",
            device_secret=api["deviceSecret"],
            executor=CommandExecutor(actions),
            ledger_path=out / "commands/ledger.json",
            result_path=out / "commands/result.json",
        )
        dispatcher.start()
        time.sleep(0.5)
        check(
            request("GET", f"/admin/devices/{device}/config", token=None).status_code == 401,
            "anonymous admin configuration denied",
        )
        check(
            request("GET", f"/admin/devices/{device}/config", token="client").status_code == 403,
            "client cannot access admin configuration",
        )
        if api.get("otherDeviceId"):
            for suffix in ("config", "operational", "config/stream"):
                foreign = request(
                    "GET",
                    f"/api/clients/me/devices/{api['otherDeviceId']}/{suffix}",
                    token="client",
                )
                check(
                    foreign.status_code in (403, 404),
                    f"cross-tenant {suffix} denied by real HTTP authorization",
                )
            foreign = request(
                "PATCH",
                f"/api/clients/me/devices/{api['otherDeviceId']}/config",
                token="client",
                json={"desiredConfig": {"processing": {"additionalWindows": []}}},
            )
            check(foreign.status_code in (403, 404), "cross-tenant configuration mutation denied")
        sync = request("POST", f"/admin/devices/{device}/config/sync")
        check(sync.ok, "admin config sync accepted over HTTP")
        current = {}

        def config_applied():
            nonlocal current
            response = request("GET", f"/admin/devices/{device}/config")
            current = response.json().get("data", {})
            return response.ok and bool((current.get("config") or {}).get("reportedConfig"))

        wait(config_applied)
        check(True, "edge snapshot persisted by API and readable through frontend HTTP contract")
        client_config = request("GET", f"/api/clients/me/devices/{device}/config", token="client")
        check(client_config.ok, "owner client reads filtered config")
        filtered = client_config.json()["data"]["config"]["reportedConfig"]
        check(
            "mqtt" not in filtered
            and "operationWindow" not in filtered
            and set(filtered.get("processing", {})) <= {"additionalWindows"},
            "client response excludes administrative configuration",
        )
        denied = request(
            "PATCH",
            f"/api/clients/me/devices/{device}/config",
            token="client",
            json={"desiredConfig": {"processing": {"deferredEnabled": True}}},
        )
        check(
            denied.status_code in (400, 403, 422),
            "client cannot activate deferred processing through real HTTP",
        )
        schedule = [{"weekdays": [1, 2, 3, 4, 5, 6, 7], "start": "00:00", "end": "23:59"}]
        version = current["config"]["configVersion"]
        changed = request(
            "PATCH",
            f"/api/clients/me/devices/{device}/config",
            token="client",
            json={
                "expectedConfigVersion": version,
                "desiredConfig": {"processing": {"additionalWindows": schedule}},
            },
        )
        check(changed.ok, "owner additional-window patch accepted")

        def schedule_applied():
            response = request("GET", f"/admin/devices/{device}/config")
            state = response.json().get("data", {}).get("config", {})
            return (
                state.get("status") == "applied"
                and (state.get("reportedConfig") or {})
                .get("processing", {})
                .get("additionalWindows")
                == schedule
            )

        wait(schedule_applied)
        check(True, "HTTP schedule crosses signed MQTT and confirms edge applied in PostgreSQL")
        conflict = request(
            "PATCH",
            f"/api/clients/me/devices/{device}/config",
            token="client",
            json={
                "expectedConfigVersion": version,
                "desiredConfig": {"processing": {"additionalWindows": []}},
            },
        )
        check(conflict.status_code == 409, "stale frontend config version rejected with HTTP409")
        before = request(
            "PATCH", f"/admin/devices/{device}/env", json={"envContent": "LOCAL=before-sync\n"}
        )
        check(before.status_code == 409, "env update blocked before authenticated recent v2 sync")
        env_sync = request("POST", f"/admin/devices/{device}/env/sync")
        check(env_sync.ok, "env HTTP sync publishes MQTT v2 request")

        def env_reported():
            files = list((out / "env-ledger").glob("*.json"))
            return any(json.loads(file.read_text()).get("type") == "env.request" for file in files)

        wait(env_reported)
        time.sleep(0.4)
        applied = request(
            "PATCH",
            f"/admin/devices/{device}/env",
            json={"envContent": "LOCAL=api-updated\n", "restartAfterApply": True},
        )
        check(applied.ok, "recent env sync authorizes HTTP encrypted update")
        wait(lambda: env_path.read_text() == "LOCAL=api-updated\n")
        check(True, "API MQTT env v2 persisted exact edited content on edge")
        op = request(
            "POST", f"/admin/devices/{device}/operations", json={"type": "restart_container"}
        )
        check(op.ok, "admin command admitted through real HTTP and SQL")
        operation_id = op.json()["data"]["id"]

        def op_failed():
            result = (
                request("GET", f"/admin/devices/{device}/operations/{operation_id}")
                .json()
                .get("data", {})
            )
            return result.get("status") == "failed" and result.get("errorCode") == "host_disabled"

        wait(op_failed)
        check(True, "disabled runner failure persisted in API without false accepted success")
        wait(lambda: not list(dispatcher.outbox.glob("*.json")))
        check(True, "API signed persistence ACK drains real edge command outbox")
        events = OperationalEventService(
            out / "operational", mqtt, lambda suffix: prefix + suffix, identity, api["deviceSecret"]
        )
        events.start()
        events.emit(
            "processing.failed",
            stage="processing",
            code="e2e_local_failure",
            severity="warning",
            incident_id="e2e-api-check",
        )
        wait(lambda: events.last_ack_at is not None)
        check(
            True,
            "PostgreSQL persisted operational event produces signed ACK and drains edge outbox",
        )
        opstate = request("GET", f"/api/clients/me/devices/{device}/operational", token="client")
        check(
            opstate.ok and "e2e_local_failure" in opstate.text,
            "frontend operational history returns persisted real event",
        )
        original = DeferredJobRepository(source / "queue_raw/.deferred")
        jobs = DeferredJobRepository(out / "delivery")
        client = GravaNoisAPIClient()
        gateway = DeferredVideoGateway(jobs, client)
        artifacts = DeferredArtifacts(jobs, [])
        for index, item in enumerate(camera["clips"][: a.clips]):
            old = original.get(item["job_id"])
            job_id = uuid.uuid4().hex
            job = replace(
                old,
                job_id=job_id,
                state=ClipJobState.WATERMARKED,
                remote_clip_id=None,
                next_attempt_at=None,
                retry_from=None,
                details={
                    **old.details,
                    **identity,
                    "policy": {**old.details["policy"], "dev": False, "dev_video": False},
                    "attempts_by_stage": {},
                    "last_error": None,
                },
            )
            for name in ("final.mp4", "thumbnail.jpg"):
                dest = jobs.directory(job_id) / "artifacts" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original.directory(old.job_id) / "artifacts" / name, dest)
            jobs.save(job)
            bound = gateway.for_job(job)
            recovered_registration = False
            lost_finalize = False
            if index == 0:
                registration = bound.register(job, bound.probe(job.artifact_location))
                job = job.with_registration(remote_clip_id=registration.clip_id)
                jobs.save(job)
                jobs = DeferredJobRepository(out / "delivery")
                gateway = DeferredVideoGateway(jobs, client)
                bound = gateway.for_job(job)
                recovered_registration = True
            use = ProcessClipJob(
                jobs,
                DeferredLeases(jobs),
                bound,
                bound,
                artifacts,
                RetryPolicy(3, timedelta(seconds=1), timedelta(seconds=1)),
                UTCClock(),
                "local-integration",
                timedelta(minutes=5),
            )
            real_finalize = client.finalize_clip_uploaded
            if index == 1:

                def lose_response(finalize=real_finalize, **kwargs):
                    finalize(**kwargs)
                    raise requests.ConnectionError(
                        "injected lost finalize response after server commit"
                    )

                client.finalize_clip_uploaded = lose_response
            result = use.execute(job_id)
            if index == 1:
                client.finalize_clip_uploaded = real_finalize
                check(
                    result.state is ClipJobState.RETRY_PENDING,
                    "lost finalize response leaves durable retry checkpoint",
                )
                upload_count = requests.get(
                    infra["s3_endpoint"] + "/__fixture/stats", timeout=5
                ).json()["counts"]["PUT"]
                current_job = jobs.get(job_id)
                jobs.save(replace(current_job, next_attempt_at=datetime.now(UTC)))
                result = use.execute(job_id)
                check(
                    requests.get(infra["s3_endpoint"] + "/__fixture/stats", timeout=5).json()[
                        "counts"
                    ]["PUT"]
                    == upload_count,
                    "finalize retry does not upload object twice",
                )
                lost_finalize = True
            saved = jobs.get(job_id)
            check(
                result.state is ClipJobState.FINALIZED,
                f"camera clip {index + 1} registered uploaded HEAD-validated finalized",
            )
            clip_id = saved.remote_clip_id
            signed = request(
                "GET",
                "/api/videos/sign",
                token="client",
                params={"clip_id": clip_id, "kind": "download"},
            )
            check(signed.ok, f"owner download URL authorized for clip {index + 1}")
            data = signed.json()["data"]
            url = data.get("url") or data.get("signedUrl") or data.get("signed_url")
            if not url:
                raise ValueError("download response lacks URL")
            if urlparse(url).hostname != "127.0.0.1":
                raise ValueError("refusing nonlocal signed object URL")
            downloaded = requests.get(url, timeout=30)
            downloaded.raise_for_status()
            original_bytes = (original.directory(old.job_id) / "artifacts/final.mp4").read_bytes()
            check(
                hashlib.sha256(downloaded.content).digest()
                == hashlib.sha256(original_bytes).digest(),
                f"clip {index + 1} frontend download matches physical camera replay bytes",
            )
            report["clips"].append(
                {
                    "index": index + 1,
                    "clip_id": clip_id,
                    "source_job_id": old.job_id,
                    "profile": item["profile"],
                    "bytes": len(downloaded.content),
                    "registration_recovered": recovered_registration,
                    "finalize_response_lost_recovered": lost_finalize,
                }
            )
            save()
            print(json.dumps({"delivered": index + 1, "profile": item["profile"]}), flush=True)
        library = request(
            "GET",
            "/api/videos/list",
            token="client",
            params={"venueId": api["venueId"], "limit": 100},
        )
        check(library.ok, "frontend video library HTTP query succeeds")
        check(
            all(clip["clip_id"] in library.text for clip in report["clips"]),
            "frontend library contains all finalized physical-camera replays",
        )
        check(
            "storage_path" not in library.text and "upload_url" not in library.text,
            "video listing excludes storage paths and upload signatures",
        )
        report["s3_fixture"] = requests.get(
            infra["s3_endpoint"] + "/__fixture/stats", timeout=5
        ).json()
        report["status"] = "passed"
        report["completed_at"] = datetime.now(UTC).isoformat()
        save()
    except BaseException as error:
        report["status"] = (
            "interrupted_by_user" if isinstance(error, InterruptedError) else "failed"
        )
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        save()
        raise
    finally:
        # One failed disconnect must not prevent other owned clients from closing.
        report["cleanup_errors"] = []
        for name, service in (
            ("events", events),
            ("commands", dispatcher),
            ("env", env_service),
            ("config", config_service),
            ("presence", presence),
            ("mqtt", mqtt),
        ):
            if service is not None:
                try:
                    service.stop()
                except Exception as error:
                    report["cleanup_errors"].append(
                        {"resource": name, "type": type(error).__name__}
                    )
        save()


if __name__ == "__main__":
    main()
