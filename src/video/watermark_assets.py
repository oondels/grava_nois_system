"""Resolve atomic, immutable client logo revisions without restarting capture."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from threading import RLock


class WatermarkAssets:
    def __init__(self, directory: Path, bottom: Path | None, top: Path | None):
        self.directory = directory
        self.fallback = (bottom, top)
        self.current = self.fallback
        self.revision = None
        self.lock = RLock()

    def resolve(self) -> tuple[Path | None, Path | None]:
        # Disabled client logos stay disabled regardless of a remotely installed manifest.
        from src.config.settings import load_client_watermark_enabled
        if not load_client_watermark_enabled():
            return None, None
        with self.lock:
            try:
                manifest = self.directory / "client-watermarks.json"
                if manifest.is_symlink() or manifest.stat().st_size > 4096:
                    return self.current
                raw = manifest.read_bytes()
                if raw == self.revision:
                    return self.current
                data = json.loads(raw)
                if set(data) != {"version", "slots"} or data["version"] != 1:
                    raise ValueError("invalid manifest")
                if not isinstance(data["slots"], dict) or set(data["slots"]) - {"bottom", "top"}:
                    raise ValueError("invalid slots")
                selected = list(self.fallback)
                for i, slot in enumerate(("bottom", "top")):
                    digest = data["slots"].get(slot)
                    if digest is None:
                        continue
                    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                        raise ValueError("invalid digest")
                    image = self.directory / "client-versions" / (digest + ".png")
                    if image.is_symlink() or image.parent.is_symlink() or image.stat().st_size > 2 * 1024 * 1024:
                        raise ValueError("invalid asset")
                    if hashlib.sha256(image.read_bytes()).hexdigest() != digest:
                        raise ValueError("asset integrity")
                    selected[i] = image
                self.current = tuple(selected)
                self.revision = raw
            except (OSError, ValueError, TypeError, KeyError):
                pass  # Keep the last valid selection, including legacy files on first boot.
            return self.current
