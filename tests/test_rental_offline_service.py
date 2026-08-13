from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from src.services.rental_offline_service import RentalOfflineService, sign_rental_message

RENTAL_ID = "11111111-1111-4111-8111-111111111111"


class _Mqtt:
    is_connected = False

    def __init__(self) -> None:
        self.subscriptions = {}
        self.published = []

    def subscribe(self, topic, handler, **_kwargs):
        self.subscriptions[topic] = handler
        return True

    def add_on_connect_listener(self, _callback):
        return None

    def publish_json(self, topic, payload, **_kwargs):
        self.published.append((topic, payload))
        return True


class RentalOfflineServiceTests(unittest.TestCase):
    def make_service(self, root: Path) -> RentalOfflineService:
        return RentalOfflineService(
            mqtt_client=_Mqtt(),
            device_id="device-1",
            device_secret="secret",
            topic_for=lambda suffix: f"grn/devices/device-1/{suffix}",
            root_dir=root,
            upload_callback=lambda _rental_id: {"processed": 1, "uploaded": 1, "failed": 0},
            logger=MagicMock(),
            quarantine_ttl_hours=48,
        )

    def test_destination_uses_manifest_or_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(Path(tmp))
            captured = datetime.now(timezone.utc)
            service._manifest = {
                "rentals": [{
                    "rental_id": RENTAL_ID,
                    "starts_at": (captured - timedelta(hours=1)).isoformat(),
                    "ends_at": (captured + timedelta(hours=1)).isoformat(),
                    "upload_grace_until": (captured + timedelta(hours=7)).isoformat(),
                }]
            }
            destination, patch = service.destination_for({"created_at": captured.isoformat()})
            self.assertEqual(destination.name, RENTAL_ID)
            self.assertEqual(patch["rental_id"], RENTAL_ID)

            destination, patch = service.destination_for({
                "created_at": (captured - timedelta(days=2)).isoformat()
            })
            self.assertEqual(destination.name, "quarantine")
            self.assertIsNone(patch["rental_id"])

    def test_inventory_does_not_expose_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / RENTAL_ID
            directory.mkdir(parents=True)
            video = directory / "clip.mp4"
            video.write_bytes(b"video")
            video.with_suffix(".json").write_text(json.dumps({
                "created_at": datetime.now(timezone.utc).isoformat(),
                "delete_after": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }))
            inventory = self.make_service(root).inventory(RENTAL_ID)
            self.assertEqual(inventory["pending_count"], 1)
            self.assertNotIn(str(root), json.dumps(inventory))

    def test_cleanup_removes_expired_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / RENTAL_ID
            directory.mkdir(parents=True)
            video = directory / "clip.mp4"
            sidecar = video.with_suffix(".json")
            video.write_bytes(b"video")
            sidecar.write_text(json.dumps({
                "delete_after": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            }))
            self.assertEqual(self.make_service(root).cleanup_expired(), 1)
            self.assertFalse(video.exists())
            self.assertFalse(sidecar.exists())

    def test_signature_is_stable_and_excludes_signature(self) -> None:
        payload = {"type": "rental.clips.request", "device_id": "device-1", "action": "inventory"}
        signature = sign_rental_message(payload, "secret")
        self.assertEqual(signature, sign_rental_message({**payload, "signature": "ignored"}, "secret"))

    def test_inventory_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self.make_service(Path(tmp)).inventory("../../etc")

    def test_cancelled_schedule_deletes_local_rental_clips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / RENTAL_ID
            directory.mkdir(parents=True)
            (directory / "clip.mp4").write_bytes(b"video")
            (directory / "clip.json").write_text("{}")
            service = self.make_service(root)
            payload = {
                "type": "rental.schedule.desired",
                "device_id": "device-1",
                "request_id": "request-1",
                "rentals": [],
                "cancelled_rental_ids": [RENTAL_ID],
                "signature_version": "hmac-sha256-v1",
            }
            payload["signature"] = sign_rental_message(payload, "secret")
            service._handle_schedule("topic", json.dumps(payload).encode())
            self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
