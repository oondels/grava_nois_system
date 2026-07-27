"""Unit tests for the transport-neutral domain and application models."""

from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime

from src.application import ApplicationError, ConflictError, NotFoundError
from src.application.configuration import SystemSnapshot
from src.application.dto import (
    BufferSnapshot,
    CaptureStatus,
    ClipJobSnapshot,
    RemoteClipRegistration,
    UploadReceipt,
)
from src.domain import DomainError, InvalidStateTransition, InvariantViolation
from src.domain.capture import (
    BufferHealth,
    CameraId,
    CameraState,
    CaptureSegment,
)
from src.domain.configuration import (
    CameraSnapshot,
    CapturePolicy,
    DeviceIdentity,
    GpioSnapshot,
    MqttSnapshot,
    OperationalConfigSnapshot,
    OperationWindowSnapshot,
    PicoSnapshot,
    ProcessingPolicy,
    RtspSnapshot,
    TriggerSnapshot,
    V4l2Snapshot,
    WatermarkSnapshot,
)
from src.domain.delivery import ClipJob, ClipJobState, RetryDecision
from src.domain.replay import ReplayRequest, ReplayWindow, TriggerSource

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

ALLOWED_TRANSITIONS = {
    ClipJobState.QUEUED: {ClipJobState.PROCESSING, ClipJobState.DISCARDED},
    ClipJobState.PROCESSING: {
        ClipJobState.WATERMARKED,
        ClipJobState.RETRY_PENDING,
        ClipJobState.FAILED,
        ClipJobState.DEV_PRESERVED,
    },
    ClipJobState.WATERMARKED: {
        ClipJobState.REGISTERED,
        ClipJobState.RETRY_PENDING,
        ClipJobState.FAILED,
        ClipJobState.DEV_PRESERVED,
    },
    ClipJobState.REGISTERED: {
        ClipJobState.UPLOADED,
        ClipJobState.RETRY_PENDING,
        ClipJobState.FAILED,
    },
    ClipJobState.UPLOADED: {
        ClipJobState.FINALIZED,
        ClipJobState.RETRY_PENDING,
        ClipJobState.FAILED,
    },
    ClipJobState.RETRY_PENDING: {
        ClipJobState.PROCESSING,
        ClipJobState.FAILED,
        ClipJobState.DISCARDED,
    },
    ClipJobState.FAILED: {
        ClipJobState.RETRY_PENDING,
        ClipJobState.DISCARDED,
    },
    ClipJobState.FINALIZED: set(),
    ClipJobState.DISCARDED: set(),
    ClipJobState.DEV_PRESERVED: set(),
}


def make_job(
    *,
    state: ClipJobState = ClipJobState.QUEUED,
    attempts: int = 0,
) -> ClipJob:
    return ClipJob(
        job_id="job-1",
        camera_id=CameraId("camera-1"),
        source_location="/queue/job-1.mp4",
        created_at=NOW,
        state=state,
        attempts=attempts,
    )


class CaptureModelTests(unittest.TestCase):
    def test_camera_id_rejects_empty_and_whitespace_values(self) -> None:
        for value in ("", " ", "\t\n"):
            with self.subTest(value=value), self.assertRaises(InvariantViolation):
                CameraId(value)

    def test_camera_id_is_immutable(self) -> None:
        camera_id = CameraId("camera-1")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            camera_id.value = "camera-2"  # type: ignore[misc]

    def test_capture_segment_accepts_a_valid_segment(self) -> None:
        segment = CaptureSegment("segment-1", CameraId("camera-1"), "/buffer/1.ts", NOW, 2.0)
        self.assertEqual("segment-1", segment.segment_id)
        self.assertEqual(2.0, segment.duration_seconds)

    def test_capture_segment_rejects_invalid_business_values(self) -> None:
        valid = {
            "segment_id": "segment-1",
            "camera_id": CameraId("camera-1"),
            "location": "/buffer/1.ts",
            "started_at": NOW,
            "duration_seconds": 2.0,
        }
        for field, value in (
            ("segment_id", " "),
            ("location", ""),
            ("duration_seconds", 0),
            ("duration_seconds", -0.1),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(InvariantViolation):
                CaptureSegment(**(valid | {field: value}))  # type: ignore[arg-type]

    def test_capture_enums_expose_stable_wire_values(self) -> None:
        self.assertEqual("OK", CameraState.OK.value)
        self.assertEqual("STALE", BufferHealth.STALE.value)


class ConfigurationModelTests(unittest.TestCase):
    def test_capture_policy_accepts_valid_durations(self) -> None:
        policy = CapturePolicy(2.0, 40.0, 8.0)
        self.assertEqual(40.0, policy.buffer_seconds)

    def test_capture_policy_rejects_non_positive_durations(self) -> None:
        for values in ((0, 40, 8), (2, 0, 8), (2, 40, 0), (-1, 40, 8)):
            with self.subTest(values=values), self.assertRaises(InvariantViolation):
                CapturePolicy(*values)

    def test_capture_policy_requires_space_for_one_segment(self) -> None:
        with self.assertRaisesRegex(InvariantViolation, "at least one segment"):
            CapturePolicy(segment_seconds=3, buffer_seconds=2, stale_after_seconds=8)

    def test_capture_policy_requires_positive_pre_and_post_windows(self) -> None:
        for pre_segments, post_segments in ((0, 1), (1, 0), (-1, 1), (1, -1)):
            with (
                self.subTest(
                    pre_segments=pre_segments,
                    post_segments=post_segments,
                ),
                self.assertRaisesRegex(InvariantViolation, "windows must be positive"),
            ):
                CapturePolicy(
                    segment_seconds=2,
                    buffer_seconds=40,
                    stale_after_seconds=8,
                    pre_segments=pre_segments,
                    post_segments=post_segments,
                )

    def test_processing_policy_defaults_and_validates_attempts(self) -> None:
        self.assertEqual(5, ProcessingPolicy().max_attempts)
        self.assertFalse(ProcessingPolicy().light_mode)
        for max_attempts in (0, -1):
            with self.subTest(max_attempts=max_attempts), self.assertRaises(InvariantViolation):
                ProcessingPolicy(max_attempts=max_attempts)

    def test_device_identity_reports_both_identified_states(self) -> None:
        self.assertTrue(DeviceIdentity("device-1", "client-1", "venue-1").is_identified)
        self.assertFalse(DeviceIdentity("", "client-1", "venue-1").is_identified)

    def test_complete_operational_snapshot_is_immutable_and_composable(self) -> None:
        capture = CapturePolicy(2, 40, 8)
        rtsp = RtspSnapshot(5, 10, 1.5, None, "20", 40, "fast", 23, True, None, True, True)
        v4l2 = V4l2Snapshot("/dev/video0", 20, "1920x1080")
        camera = CameraSnapshot("camera-1", "Main", True, "rtsp", "CAMERA_URL", None, 6, 3)
        pico = PicoSnapshot(None, "global")
        gpio = GpioSnapshot(None, 200, 2)
        triggers = TriggerSnapshot("keyboard", None, pico, gpio)
        processing = ProcessingPolicy()
        watermark = WatermarkSnapshot(0.2, 0.8, 10)
        operation_window = OperationWindowSnapshot("America/Sao_Paulo", "08:00", "23:00")
        mqtt = MqttSnapshot(True, "mqtt.local", 8883, True, 60, 30, "gn", 1, True)
        operational = OperationalConfigSnapshot(
            1,
            None,
            capture,
            rtsp,
            v4l2,
            (camera,),
            triggers,
            processing,
            watermark,
            operation_window,
            mqtt,
        )
        system = SystemSnapshot(
            operational=operational,
            identity=DeviceIdentity("device-1", "client-1", "venue-1"),
        )

        self.assertEqual("camera-1", system.operational.cameras[0].camera_id)
        self.assertEqual("mqtt.local", system.operational.mqtt.host)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            system.operational = operational  # type: ignore[misc]


class ReplayModelTests(unittest.TestCase):
    def test_replay_window_accepts_pre_only_post_only_and_combined(self) -> None:
        for values in ((5, 0), (0, 5), (5, 3)):
            with self.subTest(values=values):
                self.assertEqual(sum(values), sum(dataclasses.astuple(ReplayWindow(*values))))

    def test_replay_window_rejects_negative_or_empty_duration(self) -> None:
        for values in ((-1, 2), (2, -1), (0, 0)):
            with self.subTest(values=values), self.assertRaises(InvariantViolation):
                ReplayWindow(*values)

    def test_replay_request_requires_an_identifier(self) -> None:
        with self.assertRaises(InvariantViolation):
            ReplayRequest(" ", CameraId("camera-1"), NOW, TriggerSource.GPIO, ReplayWindow(5, 2))

    def test_replay_request_preserves_business_context(self) -> None:
        request = ReplayRequest(
            "request-1",
            CameraId("camera-1"),
            NOW,
            TriggerSource.PICO,
            ReplayWindow(5, 2),
        )
        self.assertEqual(TriggerSource.PICO, request.source)
        self.assertEqual("pico", request.source.value)


class ClipJobModelTests(unittest.TestCase):
    def test_job_defaults_to_queued_without_attempts(self) -> None:
        job = make_job()
        self.assertEqual(ClipJobState.QUEUED, job.state)
        self.assertEqual(0, job.attempts)
        self.assertIsNone(job.next_attempt_at)

    def test_job_rejects_invalid_identity_location_and_attempts(self) -> None:
        values = {
            "job_id": "job-1",
            "camera_id": CameraId("camera-1"),
            "source_location": "/queue/job-1.mp4",
            "created_at": NOW,
        }
        for field, value in (("job_id", " "), ("source_location", ""), ("attempts", -1)):
            with self.subTest(field=field), self.assertRaises(InvariantViolation):
                ClipJob(**(values | {field: value}))  # type: ignore[arg-type]

    def test_every_allowed_state_transition_returns_a_new_job(self) -> None:
        observed: set[tuple[ClipJobState, ClipJobState]] = set()
        for current_state, target_states in ALLOWED_TRANSITIONS.items():
            for target_state in target_states:
                with self.subTest(current=current_state, target=target_state):
                    original = make_job(state=current_state, attempts=2)
                    transitioned = original.transition_to(target_state)
                    self.assertIsNot(original, transitioned)
                    self.assertEqual(current_state, original.state)
                    self.assertEqual(target_state, transitioned.state)
                    self.assertEqual(original.job_id, transitioned.job_id)
                    self.assertEqual(2, transitioned.attempts)
                    observed.add((current_state, target_state))
        self.assertEqual(
            sum(len(targets) for targets in ALLOWED_TRANSITIONS.values()),
            len(observed),
        )

    def test_every_unspecified_transition_is_rejected(self) -> None:
        for current_state, allowed_targets in ALLOWED_TRANSITIONS.items():
            for target_state in set(ClipJobState) - allowed_targets:
                with (
                    self.subTest(current=current_state, target=target_state),
                    self.assertRaisesRegex(
                        InvalidStateTransition,
                        f"{current_state.value} -> {target_state.value}",
                    ),
                ):
                    make_job(state=current_state).transition_to(target_state)

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        terminal_states = {
            ClipJobState.FINALIZED,
            ClipJobState.DISCARDED,
            ClipJobState.DEV_PRESERVED,
        }
        self.assertEqual(
            terminal_states,
            {state for state, targets in ALLOWED_TRANSITIONS.items() if not targets},
        )

    def test_retry_decision_represents_retry_and_terminal_outcomes(self) -> None:
        retry = RetryDecision(True, NOW, "temporary backend failure")
        terminal = RetryDecision(False, reason="invalid media")
        self.assertTrue(retry.should_retry)
        self.assertEqual(NOW, retry.next_attempt_at)
        self.assertFalse(terminal.should_retry)
        self.assertIsNone(terminal.next_attempt_at)


class ApplicationModelTests(unittest.TestCase):
    def test_capture_status_and_buffer_snapshot_are_transport_neutral(self) -> None:
        camera_id = CameraId("camera-1")
        buffer = BufferSnapshot(camera_id, BufferHealth.FRESH, 4, NOW)
        status = CaptureStatus(camera_id, CameraState.OK, buffer)
        self.assertEqual(4, status.buffer.segment_count)
        self.assertEqual(CameraState.OK, status.state)

    def test_delivery_dtos_preserve_remote_data(self) -> None:
        snapshot = ClipJobSnapshot("job-1", ClipJobState.UPLOADED, 1, "/wm/job.mp4")
        registration = RemoteClipRegistration("clip-1", "https://upload", {"x-id": "1"})
        receipt = UploadReceipt(200, {"etag": "abc"})
        self.assertEqual(ClipJobState.UPLOADED, snapshot.state)
        self.assertEqual("clip-1", registration.clip_id)
        self.assertEqual(200, receipt.status_code)

    def test_application_exception_hierarchy(self) -> None:
        self.assertTrue(issubclass(NotFoundError, ApplicationError))
        self.assertTrue(issubclass(ConflictError, ApplicationError))

    def test_domain_exception_hierarchy(self) -> None:
        self.assertTrue(issubclass(InvariantViolation, DomainError))
        self.assertTrue(issubclass(InvariantViolation, ValueError))
        self.assertTrue(issubclass(InvalidStateTransition, DomainError))


if __name__ == "__main__":
    unittest.main()
