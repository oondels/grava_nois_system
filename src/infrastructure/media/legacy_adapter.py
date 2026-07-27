"""Compatibility adapter for legacy media callables during incremental migration."""

from collections.abc import Callable, Sequence


class LegacyMediaToolAdapter:
    """Expose existing FFmpeg helpers through the new ``MediaTool`` port."""

    def __init__(
        self,
        *,
        concatenate: Callable[[Sequence[str], str], None],
        apply_watermark: Callable[[str, str], None],
        probe: Callable[[str], dict[str, object]],
    ) -> None:
        self._concatenate = concatenate
        self._apply_watermark = apply_watermark
        self._probe = probe

    def concatenate(self, inputs: Sequence[str], output: str) -> None:
        self._concatenate(inputs, output)

    def apply_watermark(self, source: str, output: str) -> None:
        self._apply_watermark(source, output)

    def probe(self, source: str) -> dict[str, object]:
        return self._probe(source)
