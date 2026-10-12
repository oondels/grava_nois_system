import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src.video.watermark_assets import WatermarkAssets


class WatermarkAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bottom = self.base / 'client_logo_wm.png'; self.bottom.write_bytes(b'legacy')
        self.resolver = WatermarkAssets(self.base, self.bottom, None)
        self.patch = patch('src.config.settings.load_client_watermark_enabled', return_value=True)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def install(self, content):
        digest = hashlib.sha256(content).hexdigest()
        directory = self.base / 'client-versions'; directory.mkdir(exist_ok=True)
        path = directory / (digest + '.png'); path.write_bytes(content)
        (self.base / 'client-watermarks.json').write_text(json.dumps({'version': 1, 'slots': {'top': digest}}))
        return path

    def test_legacy_without_top_and_live_atomic_replacement(self):
        self.assertEqual(self.resolver.resolve(), (self.bottom, None))
        first = self.install(b'first')
        old_selection = self.resolver.resolve()
        second = self.install(b'second')
        self.assertEqual(old_selection, (self.bottom, first))
        self.assertEqual(self.resolver.resolve(), (self.bottom, second))
        self.assertEqual(first.read_bytes(), b'first')

    def test_invalid_manifest_keeps_last_valid_selection(self):
        first = self.install(b'first'); self.resolver.resolve()
        (self.base / 'client-watermarks.json').write_text('{partial')
        self.assertEqual(self.resolver.resolve(), (self.bottom, first))

    def test_traversal_missing_corrupt_and_symlink_assets_are_rejected(self):
        manifest = self.base / 'client-watermarks.json'
        for digest in ('../../.env', 'a'*64):
            manifest.write_text(json.dumps({'version': 1, 'slots': {'top': digest}}))
            self.assertEqual(self.resolver.resolve(), (self.bottom, None))
        asset = self.install(b'valid'); asset.write_bytes(b'corrupt')
        self.assertEqual(self.resolver.resolve(), (self.bottom, None))
        asset.unlink(); asset.symlink_to(self.bottom)
        self.assertEqual(self.resolver.resolve(), (self.bottom, None))

    def test_disabled_client_logos_ignore_manifest(self):
        self.install(b'valid')
        with patch('src.config.settings.load_client_watermark_enabled', return_value=False):
            self.assertEqual(self.resolver.resolve(), (None, None))
