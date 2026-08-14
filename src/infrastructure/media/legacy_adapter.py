"""Compatibility adapter for legacy media callables during incremental migration."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


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


@dataclass(frozen=True, slots=True)
class WatermarkPolicy:
    """Resolved watermark options passed to the existing FFmpeg helper."""

    image_path: str
    secondary_image_path: str | None = None
    margin: int = 24
    opacity: float = 0.8
    relative_width: float = 0.18
    secondary_relative_width: float | None = None
    codec: str = "libx264"
    crf: int = 18
    preset: str = "medium"
    vertical_format: bool = False

    def __post_init__(self) -> None:
        if not self.image_path.strip():
            raise ValueError("watermark image path must not be empty")
        if self.margin < 0:
            raise ValueError("watermark margin must not be negative")
        if not 0 <= self.opacity <= 1:
            raise ValueError("watermark opacity must be between zero and one")
        if not 0 < self.relative_width <= 1:
            raise ValueError("watermark relative width must be between zero and one")
        if self.secondary_relative_width is not None and not (
            0 < self.secondary_relative_width <= 1
        ):
            raise ValueError("secondary watermark relative width must be between zero and one")


LegacyWatermark = Callable[..., None]
LegacyProbe = Callable[[Path], dict[str, object]]


class ConfiguredLegacyMediaToolAdapter(LegacyMediaToolAdapter):
    """Bind legacy media functions to one explicit immutable policy."""

    def __init__(
        self,
        *,
        concatenate: Callable[[Sequence[str], str], None],
        watermark: LegacyWatermark,
        probe: LegacyProbe,
        policy: WatermarkPolicy,
    ) -> None:
        def apply_watermark(source: str, output: str) -> None:
            watermark(
                source,
                policy.image_path,
                output,
                secondary_watermark_path=policy.secondary_image_path,
                margin=policy.margin,
                opacity=policy.opacity,
                rel_width=policy.relative_width,
                secondary_rel_width=policy.secondary_relative_width,
                codec=policy.codec,
                crf=policy.crf,
                preset=policy.preset,
                vertical_format=policy.vertical_format,
            )

        def run_probe(source: str) -> dict[str, object]:
            return probe(Path(source))

        super().__init__(
            concatenate=concatenate,
            apply_watermark=apply_watermark,
            probe=run_probe,
        )
