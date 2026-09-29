import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.services.runtime_health import RuntimeHealth, healthy


class RuntimeHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"GN_RUNTIME_CONFIG_DIR": self.tmp.name}).start()
        self.health = RuntimeHealth(Path(self.tmp.name), "test-boot")

    def test_liveness_is_distinct_from_camera_readiness(self):
        camera = SimpleNamespace(proc=Mock(), segbuf=Mock())
        camera.proc.poll.return_value = None
        camera.segbuf.diagnostics.return_value.buffer_fresh = False
        self.health.tick([camera])
        self.assertTrue(healthy(self.health.path))
        self.assertFalse(healthy(self.health.path, require_cameras=True))
        camera.segbuf.diagnostics.return_value.buffer_fresh = True
        self.health._last = float("-inf")
        self.health.tick([camera])
        self.assertTrue(healthy(self.health.path, require_cameras=True))

    def test_intentionally_no_cameras_can_be_ready_but_stopped_cannot(self):
        self.health.tick([])
        self.assertTrue(healthy(self.health.path, require_cameras=True))
        self.health.tick([], stopped=True)
        self.assertFalse(healthy(self.health.path))

    def test_stale_wrong_process_and_corrupt_snapshots_fail_closed(self):
        self.health.tick([])
        original = json.loads(self.health.path.read_text())
        for altered in (
            {**original, "updated_monotonic": time.monotonic() - 21},
            {**original, "process_start": "old-process"},
            {**original, "pid": 0},
            [],
            {},
        ):
            self.health.path.write_text(json.dumps(altered))
            self.assertFalse(healthy(self.health.path))
