"""Media tooling port; FFmpeg and ffprobe belong behind this boundary."""

from collections.abc import Sequence
from typing import Protocol


class MediaTool(Protocol):
    def concatenate(self, inputs: Sequence[str], output: str) -> None: ...

    def apply_watermark(self, source: str, output: str) -> None: ...

    def probe(self, source: str) -> dict[str, object]: ...
