from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from typing import List, Tuple
from unittest import mock

from dotenv import load_dotenv

from src.config.settings import CaptureConfig, load_capture_configs
from src.video.buffer import SegmentBuffer, clear_buffer
from src.video.capture import start_ffmpeg
from src.video.processor import build_highlight, enqueue_clip, ffprobe_metadata
from src.workers.processing_worker import ProcessingWorker

load_dotenv()


class CameraWatermarkIntegrationTests(unittest.TestCase):
    """Teste real de captura + highlight + watermark com as cameras do .env."""

    def _run_base_dir(self, repo_base: Path) -> Path:
        raw_out = (os.getenv("GN_CAMERA_INTEGRATION_OUTPUT_DIR") or "").strip()
        if raw_out:
            run_base = Path(raw_out).expanduser().resolve()
        else:
            run_base = (repo_base / "artifacts" / "camera_watermark_test").resolve()
        run_base.mkdir(parents=True, exist_ok=True)
        return run_base

    def _wait_for_segments(
        self,
        cfg: CaptureConfig,
        proc,
        minimum_segments: int,
        timeout_sec: float = 25.0,
    ) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"FFmpeg encerrou prematuramente para {cfg.camera_id} com codigo {proc.returncode}"
                )
            count = len(list(cfg.buffer_dir.glob("buffer*.ts")))
            if count >= minimum_segments:
                return
            time.sleep(0.5)
        raise TimeoutError(
            f"Timeout aguardando segmentos suficientes para {cfg.camera_id} em {cfg.buffer_dir}"
        )

    def _repo_logo_paths(self, repo_base: Path) -> tuple[Path, Path | None]:
        primary = repo_base / "files" / "replay_grava_nois_wm.png"
        if not primary.exists():
            primary = repo_base / "files" / "replay_grava_nois.png"

        secondary = repo_base / "files" / "client_logo_wm.png"
        if not secondary.exists():
            secondary = repo_base / "files" / "client_logo.png"
        if not secondary.exists():
            secondary = None
        return primary, secondary

    def test_generates_final_mp4_with_real_camera_input(self) -> None:
        if os.getenv("GN_RUN_CAMERA_INTEGRATION", "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }:
            self.skipTest("Defina GN_RUN_CAMERA_INTEGRATION=1 para rodar com camera real")

        repo_base = Path(__file__).resolve().parents[1]
        primary_logo, secondary_logo = self._repo_logo_paths(repo_base)
        self.assertTrue(primary_logo.exists(), "Logo principal nao encontrada em files/")
        run_base = self._run_base_dir(repo_base)
        buffer_root = run_base / "buffer"
        out_wm_dir = run_base / "highlights_wm"

        env = {
            "DEV": "1",
            "GN_LIGHT_MODE": "0",
            "VERTICAL_FORMAT": "1",
            "GN_BUFFER_DIR": str(buffer_root),
            "GN_RTSP_MAX_RETRIES": "1",
            "GN_RTSP_TIMEOUT": "3",
            "GN_WM_REL_WIDTH": os.getenv("GN_WM_REL_WIDTH", "0.19"),
        }

        runtimes: List[Tuple[CaptureConfig, object, SegmentBuffer]] = []
        processed_outputs: list[Path] = []

        with mock.patch.dict(os.environ, env, clear=False):
            configs = load_capture_configs(base=run_base, seg_time=1)
            self.assertGreater(len(configs), 0, "Nenhuma camera foi carregada do .env")

            try:
                for cfg in configs:
                    cfg.pre_segments = 2
                    cfg.post_segments = 1
                    cfg.pre_seconds = 2
                    cfg.post_seconds = 1
                    cfg.max_buffer_seconds = 8
                    cfg.scan_interval = 0.2
                    clear_buffer(cfg)
                    cfg.ensure_dirs()
                    proc = start_ffmpeg(cfg)
                    segbuf = SegmentBuffer(cfg)
                    segbuf.start()
                    runtimes.append((cfg, proc, segbuf))

                for cfg, proc, _segbuf in runtimes:
                    self._wait_for_segments(
                        cfg,
                        proc,
                        minimum_segments=(cfg.pre_segments or 2) + (cfg.post_segments or 1) + 1,
                    )

                for cfg, _proc, segbuf in runtimes:
                    clip = build_highlight(cfg, segbuf)
                    self.assertIsNotNone(clip, f"Nao foi possivel gerar highlight para {cfg.camera_id}")
                    self.assertTrue(clip.exists(), f"Highlight ausente para {cfg.camera_id}")

                    queued_mp4 = enqueue_clip(cfg, clip)
                    meta_path = cfg.queue_dir / f"{queued_mp4.stem}.json"
                    self.assertTrue(meta_path.exists(), f"Sidecar ausente para {cfg.camera_id}")

                    worker = ProcessingWorker(
                        queue_dir=cfg.queue_dir,
                        out_wm_dir=out_wm_dir,
                        failed_dir_highlight=cfg.failed_dir_highlight,
                        watermark_path=primary_logo,
                        client_watermark_path=secondary_logo,
                        scan_interval=0,
                        max_attempts=1,
                        wm_margin=24,
                        wm_opacity=0.8,
                        wm_rel_width=float(env["GN_WM_REL_WIDTH"]),
                        light_mode=False,
                        retry_failed=False,
                    )
                    worker._process_one(queued_mp4, meta_path)

                    final_mp4 = out_wm_dir / queued_mp4.name
                    self.assertTrue(final_mp4.exists(), f"MP4 final nao gerado para {cfg.camera_id}")
                    self.assertGreater(final_mp4.stat().st_size, 0, "Arquivo final gerado vazio")

                    final_meta = ffprobe_metadata(final_mp4)
                    final_w = final_meta.get("width") or 0
                    final_h = final_meta.get("height") or 0
                    self.assertGreater(final_w, 0, "Largura do vídeo final deve ser > 0")
                    self.assertGreater(final_h, 0, "Altura do vídeo final deve ser > 0")
                    # Vertical format: proporção deve ser aproximadamente 9:16
                    if env.get("VERTICAL_FORMAT") == "1":
                        ratio = final_w / final_h
                        self.assertAlmostEqual(ratio, 9 / 16, delta=0.02, msg="Proporção final deve ser ~9:16")
                    processed_outputs.append(final_mp4)
            finally:
                for _cfg, proc, segbuf in runtimes:
                    try:
                        segbuf.stop(join_timeout=1.0)
                    except Exception:
                        pass
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                            proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

        self.assertGreater(
            len(processed_outputs), 0, "O teste deve gerar pelo menos um mp4 final"
        )


if __name__ == "__main__":
    unittest.main()
