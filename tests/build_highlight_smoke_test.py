#!/usr/bin/env python3
"""
Smoke test para build_highlight:
- Gera segmentos sintéticos com ffmpeg (lavfi testsrc)
- Usa um SegmentBuffer fake para fornecer os últimos N segmentos
- Chama build_highlight e valida que:
  - O highlight é gerado em clips_dir
  - O buffer_dir permanece intacto (nenhum arquivo removido)
  - A pasta de staging não mantém .mp4 após a execução

Requisitos:
- ffmpeg/ffprobe no PATH

Execução:
  python tests/build_highlight_smoke_test.py
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from video_core import CaptureConfig, build_highlight


def which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


class FakeSegBuf:
    def __init__(self, buffer_dir: Path):
        self.buffer_dir = Path(buffer_dir)

    def snapshot_last(self, n: int):
        files = sorted(
            self.buffer_dir.glob("buffer*.mp4"), key=lambda p: p.stat().st_mtime
        )
        return [str(p) for p in files[-n:]]


def main() -> int:
    if not which("ffmpeg"):
        print("[skip] ffmpeg não encontrado no PATH; teste não executado.")
        return 0

    with tempfile.TemporaryDirectory(prefix="gn_buf_") as buf_dir_str, \
         tempfile.TemporaryDirectory(prefix="gn_clips_") as clips_dir_str, \
         tempfile.TemporaryDirectory(prefix="gn_queue_") as queue_dir_str:

        buf_dir = Path(buf_dir_str)
        clips_dir = Path(clips_dir_str)
        queue_dir = Path(queue_dir_str)

        # Gera ~6 segmentos de 1s
        out_pattern = str(buf_dir / "buffer%06d.mp4")
        gen_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "lavfi",
            "-i", "testsrc=size=320x240:rate=30",
            "-t", "6",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-g", "30",
            "-force_key_frames", "expr:gte(t,n_forced*1)",
            "-f", "segment",
            "-segment_time", "1",
            "-reset_timestamps", "1",
            out_pattern,
        ]
        subprocess.run(gen_cmd, check=True)

        # Conta arquivos no buffer antes
        before_files = sorted(buf_dir.glob("buffer*.mp4"))
        assert len(before_files) >= 3, "Esperava ao menos 3 segmentos gerados"

        cfg = CaptureConfig(
            buffer_dir=buf_dir,
            clips_dir=clips_dir,
            queue_dir=queue_dir,
            seg_time=1,
            pre_seconds=2,
            post_seconds=0,  # evita espera extra; build_highlight espera ~0.7s
            scan_interval=0.2,
            max_buffer_seconds=10,
        )
        cfg.ensure_dirs()

        segbuf = FakeSegBuf(buf_dir)
        out = build_highlight(cfg, segbuf)
        assert out is not None and out.exists(), "Highlight não foi gerado"
        assert out.stat().st_size > 0, "Highlight vazio"

        # Buffer deve permanecer intacto
        after_files = sorted(buf_dir.glob("buffer*.mp4"))
        assert [p.name for p in after_files] == [p.name for p in before_files], (
            "buffer_dir foi modificado; esperado permanecer intacto"
        )

        # Pasta de staging não deve manter .mp4
        staging_dir = Path(__file__).resolve().parent.parent / "buffered_seguiments_post_clique"
        if staging_dir.exists():
            leftovers = list(staging_dir.glob("*.mp4"))
            assert not leftovers, f"Staging deixou arquivos: {[p.name for p in leftovers]}"

        print("[ok] build_highlight gerou highlight e preservou o buffer.")
        print(f"[ok] saída: {out}")
        return 0


if __name__ == "__main__":
    sys.exit(main())

