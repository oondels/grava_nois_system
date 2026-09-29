"""Exercise converter arguments with synthetic files, never the local .env."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "env_to_config.sh"
ENV_CONTENT = (
    'GN_CAMERAS_JSON=[{"id":"notebook","enabled":true,"sourceType":"v4l2"}]\n'
    "GN_V4L2_DEVICE=/dev/video0\n"
    "GN_INPUT_FRAMERATE=30\n"
    "GN_VIDEO_SIZE=640x480\n"
    "GN_DEFERRED_PROCESSING_ENABLED=1\n"
    "GN_PROCESSING_WINDOWS_JSON=[]\n"
)


class EnvToConfigCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gn-converter-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / ".env").write_text(ENV_CONTENT)

    def run_converter(self, *arguments):
        return subprocess.run(
            ["bash", str(SCRIPT), *arguments],
            cwd=self.root,
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            timeout=10,
        )

    def assert_webcam(self, target):
        config = json.loads(target.read_text())
        self.assertEqual(config["cameras"][0]["id"], "notebook")
        self.assertEqual(config["cameras"][0]["sourceType"], "v4l2")
        self.assertTrue(config["cameras"][0]["enabled"])
        self.assertEqual(config["capture"]["v4l2"]["videoSize"], "640x480")
        self.assertTrue(config["processing"]["deferredEnabled"])

    def test_explicit_dotenv_writes_only_requested_destination_and_backup(self):
        output = self.root / "runtime_config" / "config.json"
        output.parent.mkdir()
        output.write_text('{"existing":true}\n')
        default_output = self.root / "config.json"
        default_output.write_text("keep-root-config\n")
        result = self.run_converter(".env", "runtime_config/config.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_webcam(output)
        self.assertEqual(output.with_suffix(".json.bak").read_text(), '{"existing":true}\n')
        self.assertEqual(default_output.read_text(), "keep-root-config\n")
        self.assertEqual((self.root / ".env").read_text(), ENV_CONTENT)
        self.assertIn(f"Fonte : {self.root / '.env'}", result.stdout)
        self.assertIn(f"Saída : {output}", result.stdout)

    def test_no_arguments_use_local_defaults(self):
        result = self.run_converter()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_webcam(self.root / "config.json")

    def test_single_source_named_like_default_output_is_still_input(self):
        (self.root / ".env").unlink()
        (self.root / "config.json").write_text(ENV_CONTENT)
        # Dry-run avoids writing to the same path while proving positional parsing.
        result = self.run_converter("config.json", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"sourceType": "v4l2"', result.stdout)
        self.assertEqual((self.root / "config.json").read_text(), ENV_CONTENT)

    def test_dry_run_accepts_flags_before_paths_and_never_writes(self):
        output = self.root / "runtime_config" / "config.json"
        result = self.run_converter("--dry-run", ".env", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"sourceType": "v4l2"', result.stdout)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "config.json").exists())

    def test_absolute_source_and_destination_with_spaces(self):
        source = self.root / "local camera.env"
        source.write_text(ENV_CONTENT)
        output = self.root / "runtime local" / "config.json"
        result = self.run_converter(str(source), str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_webcam(output)

    def test_extra_positional_argument_is_rejected_without_writes(self):
        (self.root / "local.env").write_text(ENV_CONTENT)
        result = self.run_converter("local.env", "output.json", "unexpected.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "output.json").exists())
        self.assertFalse((self.root / "config.json").exists())

    def test_empty_camera_config_does_not_promise_v4l2_fallback(self):
        (self.root / ".env").write_text("GN_V4L2_DEVICE=/dev/video0\n")
        result = self.run_converter(".env", "output.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / "output.json").read_text())["cameras"], [])
        self.assertNotIn("V4L2 local como fallback", result.stdout)
        self.assertIn("sourceType=v4l2", result.stdout)


if __name__ == "__main__":
    unittest.main()
