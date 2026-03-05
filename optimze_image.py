#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


DEFAULT_WIDTH = 360
DEFAULT_JOBS = [
    ("files/replay_grava_nois.png", "files/replay_grava_nois_wm.png"),
    ("files/client_logo.png", "files/client_logo_wm.png"),
]


def ffprobe_image(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt,width,height",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    return streams[0] if streams else {}


def optimize_image(input_path: Path, output_path: Path, width: int) -> None:
    if not input_path.exists():
        raise FileNotFoundError(f"Input inexistente: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale={int(width)}:-1:flags=lanczos,format=rgba"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vf",
        vf,
        "-frames:v",
        "1",
        str(output_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def run_jobs(jobs: list[tuple[Path, Path]], width: int) -> int:
    for inp, out in jobs:
        optimize_image(inp, out, width)
        meta = ffprobe_image(out)
        size_kb = out.stat().st_size / 1024.0
        print(
            f"[ok] {inp} -> {out} | "
            f"{meta.get('codec_name')} {meta.get('pix_fmt')} "
            f"{meta.get('width')}x{meta.get('height')} | {size_kb:.1f} KB"
        )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Otimiza logos para watermark (PNG RGBA redimensionado)."
    )
    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_WIDTH,
        help=f"Largura alvo (default: {DEFAULT_WIDTH})",
    )
    parser.add_argument("--input", type=str, help="Imagem de entrada")
    parser.add_argument("--output", type=str, help="Imagem de saída")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if (args.input and not args.output) or (args.output and not args.input):
        raise SystemExit("Use --input e --output juntos, ou nenhum deles.")

    if args.input and args.output:
        jobs = [(Path(args.input), Path(args.output))]
    else:
        jobs = [(Path(i), Path(o)) for i, o in DEFAULT_JOBS]

    return run_jobs(jobs, width=max(1, int(args.width)))


if __name__ == "__main__":
    raise SystemExit(main())
