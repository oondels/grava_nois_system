import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from src.application.dto import BufferSnapshot, CaptureStatus
from src.application.replay import CaptureReplay, CaptureReplayFailure
from src.domain.capture import (
    BufferHealth,
    CameraId,
    CameraState,
    CaptureSegment,
    ReadinessFailure,
    decide_readiness,
    select_segments,
)
from src.domain.exceptions import InvariantViolation
from src.domain.replay import (
    CameraRoute,
    CooldownPolicy,
    ReplayRequest,
    ReplayWindow,
    TriggerRouter,
    TriggerSource,
    replay_window_from_segments,
)
from src.infrastructure.media import LegacyMediaToolAdapter

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
CAMERA = CameraId("camera-1")


def _segment(
    segment_id: str,
    offset: float,
    *,
    camera: CameraId = CAMERA,
    duration: float = 2,
) -> CaptureSegment:
    return CaptureSegment(
        segment_id,
        camera,
        f"/buffer/{segment_id}.ts",
        NOW + timedelta(seconds=offset),
        duration,
    )


class CapturePolicyTests(unittest.TestCase):
    def test_readiness_reports_each_rejection_and_accepts_ready_camera(self) -> None:
        cases = (
            (
                CameraState.UNAVAILABLE,
                BufferHealth.FRESH,
                3,
                ReadinessFailure.CAMERA_NOT_READY,
            ),
            (
                CameraState.OK,
                BufferHealth.STALE,
                3,
                ReadinessFailure.BUFFER_NOT_FRESH,
            ),
            (
                CameraState.OK,
                BufferHealth.FRESH,
                1,
                ReadinessFailure.INSUFFICIENT_SEGMENTS,
            ),
        )
        for state, health, count, expected in cases:
            with self.subTest(expected=expected):
                decision = decide_readiness(
                    camera_state=state,
                    buffer_health=health,
                    segment_count=count,
                )
                self.assertFalse(decision.accepted)
                self.assertEqual(expected, decision.failure)

        self.assertTrue(
            decide_readiness(
                camera_state=CameraState.OK,
                buffer_health=BufferHealth.FRESH,
                segment_count=2,
            ).accepted
        )

    def test_segment_selection_filters_window_camera_duplicates_and_orders(self) -> None:
        other = CameraId("other")
        duplicate = _segment("middle", -1)
        selected = select_segments(
            [
                _segment("after", 5),
                _segment("end-overlap", 2),
                duplicate,
                duplicate,
                _segment("other", -1, camera=other),
                _segment("before", -5, duration=1),
                _segment("start-overlap", -3, duration=2),
            ],
            camera_id=CAMERA,
            start=NOW - timedelta(seconds=2),
            end=NOW + timedelta(seconds=2),
        )
        self.assertEqual(
            ("start-overlap", "middle", "end-overlap"),
            tuple(item.segment_id for item in selected),
        )


class ReplayPolicyTests(unittest.TestCase):
    def test_window_from_segments_and_invalid_values(self) -> None:
        self.assertEqual(
            ReplayWindow(12, 6),
            replay_window_from_segments(pre_segments=6, post_segments=3, segment_seconds=2),
        )
        for values in ((0, 1, 2), (1, 0, 2), (1, 1, 0)):
            with self.subTest(values=values), self.assertRaises(InvariantViolation):
                replay_window_from_segments(
                    pre_segments=values[0],
                    post_segments=values[1],
                    segment_seconds=values[2],
                )

    def test_cooldown_policy(self) -> None:
        policy = CooldownPolicy(10)
        self.assertTrue(policy.allows(last_triggered_at=None, triggered_at=NOW))
        self.assertFalse(
            policy.allows(last_triggered_at=NOW, triggered_at=NOW + timedelta(seconds=9))
        )
        self.assertTrue(
            policy.allows(last_triggered_at=NOW, triggered_at=NOW + timedelta(seconds=10))
        )
        with self.assertRaises(InvariantViolation):
            CooldownPolicy(-1)

    def test_router_preserves_dedicated_global_fallback_and_disabled_rules(self) -> None:
        router = TriggerRouter(global_token=" btn_replay ")
        cameras = [
            CameraRoute(CameraId("dedicated"), " btn_1 "),
            CameraRoute(CameraId("global")),
            CameraRoute(CameraId("disabled"), None, False),
        ]
        self.assertEqual((CameraId("dedicated"),), router.route("BTN_1", cameras))
        self.assertEqual((CameraId("global"),), router.route("btn_replay", cameras))
        self.assertEqual((), router.route("unknown", cameras))

        all_dedicated = [
            CameraRoute(CameraId("one"), "one"),
            CameraRoute(CameraId("two"), "two"),
        ]
        self.assertEqual(
            (CameraId("one"), CameraId("two")),
            router.route("BTN_REPLAY", all_dedicated),
        )
        self.assertEqual((), router.route("BTN_REPLAY", []))
        with self.assertRaises(InvariantViolation):
            TriggerRouter(global_token=" ")


class CaptureReplayTests(unittest.TestCase):
    def _request(self) -> ReplayRequest:
        return ReplayRequest(
            "request-1",
            CAMERA,
            NOW,
            TriggerSource.PICO,
            ReplayWindow(2, 2),
        )

    @staticmethod
    def _status(
        state: CameraState = CameraState.OK,
        health: BufferHealth = BufferHealth.FRESH,
        count: int = 3,
    ) -> CaptureStatus:
        return CaptureStatus(
            CAMERA,
            state,
            BufferSnapshot(CAMERA, health, count, NOW),
        )

    def test_success_queries_exact_window_and_concatenates_in_order(self) -> None:
        repository = Mock()
        repository.status.return_value = self._status()
        repository.list_between.return_value = [
            _segment("second", 0),
            _segment("first", -2),
        ]
        media = Mock()
        use_case = CaptureReplay(segments=repository, media=media)

        result = use_case.execute(self._request(), output_location="/clips/replay.mp4")

        self.assertTrue(result.succeeded)
        self.assertEqual(("first", "second"), result.selected_segment_ids)
        repository.list_between.assert_called_once_with(
            CAMERA, NOW - timedelta(seconds=2), NOW + timedelta(seconds=2)
        )
        media.concatenate.assert_called_once_with(
            ("/buffer/first.ts", "/buffer/second.ts"), "/clips/replay.mp4"
        )

    def test_readiness_rejection_does_not_query_or_invoke_media(self) -> None:
        repository = Mock()
        repository.status.return_value = self._status(
            CameraState.RECONNECTING, BufferHealth.FRESH, 3
        )
        media = Mock()
        result = CaptureReplay(segments=repository, media=media).execute(
            self._request(), output_location="/clips/replay.mp4"
        )
        self.assertEqual(CaptureReplayFailure.CAMERA_NOT_READY, result.failure)
        repository.list_between.assert_not_called()
        media.concatenate.assert_not_called()

    def test_fresh_snapshot_can_still_have_insufficient_matching_segments(self) -> None:
        repository = Mock()
        repository.status.return_value = self._status(count=4)
        repository.list_between.return_value = [_segment("only", 0)]
        media = Mock()
        result = CaptureReplay(segments=repository, media=media).execute(
            self._request(), output_location="/clips/replay.mp4"
        )
        self.assertEqual(CaptureReplayFailure.INSUFFICIENT_SEGMENTS, result.failure)
        self.assertEqual(("only",), result.selected_segment_ids)
        media.concatenate.assert_not_called()

    def test_validates_constructor_and_output(self) -> None:
        with self.assertRaises(ValueError):
            CaptureReplay(segments=Mock(), media=Mock(), minimum_segments=0)
        use_case = CaptureReplay(segments=Mock(), media=Mock())
        with self.assertRaises(ValueError):
            use_case.execute(self._request(), output_location=" ")


class LegacyMediaAdapterTests(unittest.TestCase):
    def test_delegates_every_media_operation(self) -> None:
        concat = Mock()
        watermark = Mock()
        probe = Mock(return_value={"duration": 1})
        adapter = LegacyMediaToolAdapter(concatenate=concat, apply_watermark=watermark, probe=probe)
        adapter.concatenate(("a", "b"), "out")
        adapter.apply_watermark("out", "wm")
        self.assertEqual({"duration": 1}, adapter.probe("wm"))
        concat.assert_called_once_with(("a", "b"), "out")
        watermark.assert_called_once_with("out", "wm")
        probe.assert_called_once_with("wm")


if __name__ == "__main__":
    unittest.main()
