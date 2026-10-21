from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ai_privacy_redaction import PrivacyRedactionProcessor
from core import validate_frame_result
from core.privacy_tracking import PrivacyDetection


@dataclass
class _Ready:
    ready: bool = True
    backend: str = "fake"
    reason: str = ""


class _SequenceDetector:
    def __init__(self, frames, *, ready: bool = True):
        self.frames = list(frames)
        self.calls = 0
        self.readiness = _Ready(ready=ready, reason="" if ready else "missing")

    def detect(self, _frame, *, rois=None):
        del rois
        index = self.calls
        self.calls += 1
        return list(self.frames[index]) if index < len(self.frames) else []


class _FailingDetector:
    readiness = _Ready(ready=True)

    def detect(self, _frame, *, rois=None):
        del rois
        raise RuntimeError("backend failed")


def _frame() -> np.ndarray:
    image = np.zeros((120, 200, 3), dtype=np.uint8)
    image[20:80, 30:100] = (210, 180, 120)
    image[82:102, 105:175] = (230, 230, 230)
    return image


def test_face_mask_modifies_pixels_and_contract():
    detector = _SequenceDetector([[PrivacyDetection("face", (35, 25, 40, 40), 0.95, "fake")]])
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("face",), mode="solid", fail_policy="best_effort",
        require_face_backend=True, face_detector=detector, person_head_fallback=False,
    )
    source = _frame()
    result = processor.process(source, {"frame_index": 0, "time": 0.0})
    ok, reason = validate_frame_result(result, strict=True)
    assert ok, reason
    assert any(event["label"] == "face_redacted" for event in result["events"])
    assert not np.array_equal(source, result["frame"])


def test_face_track_masks_detector_dropout():
    detector = _SequenceDetector([
        [PrivacyDetection("face", (35, 25, 40, 40), 0.95, "fake")], [],
    ])
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("face",), mode="pixelate", fail_policy="best_effort",
        require_face_backend=True, face_detector=detector, face_detect_every=1,
        max_missed_frames=3, person_head_fallback=False,
    )
    first = processor.process(_frame(), {"frame_index": 0, "time": 0.0})
    second = processor.process(_frame(), {"frame_index": 1, "time": 1 / 15})
    assert any(event["label"] == "face_redacted" for event in first["events"])
    event = next(event for event in second["events"] if event["label"] == "face_redacted")
    assert event["track"]["state"] == "predicted"


def test_person_head_fallback_masks_profile_pedestrian():
    detector = _SequenceDetector([[]])
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("face",), mode="solid", fail_policy="best_effort",
        require_face_backend=False, face_detector=detector, person_head_fallback=True,
    )
    result = processor.process(
        _frame(),
        {
            "frame_index": 0, "time": 0.0,
            "object_tracks": [
                {"label": "person", "confidence": 0.9,
                 "bbox": {"x": 20, "y": 10, "w": 70, "h": 100},
                 "track": {"id": 4}},
            ],
        },
    )
    event = next(event for event in result["events"] if event["label"] == "face_redacted")
    assert event["attributes"]["source"] == "person_head_fallback"


def test_fail_closed_blocks_complete_frame_when_face_backend_missing():
    unavailable = _SequenceDetector([[]], ready=False)
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("face",), fail_policy="block_frame", block_mode="solid",
        require_face_backend=True, face_detector=unavailable, person_head_fallback=False,
    )
    result = processor.process(_frame(), {"frame_index": 0, "time": 0.0})
    assert result["events"][0]["label"] == "privacy_frame_blocked"
    assert np.count_nonzero(result["frame"]) == 0


def test_stream_switch_clears_face_tracks():
    detector = _SequenceDetector([
        [PrivacyDetection("face", (35, 25, 40, 40), 0.95, "fake")], [],
    ])
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("face",), fail_policy="best_effort", require_face_backend=True,
        face_detector=detector, max_missed_frames=5, person_head_fallback=False,
    )
    first = processor.process(_frame(), {"frame_index": 0, "time": 0.0, "stream_id": "a"})
    second = processor.process(_frame(), {"frame_index": 0, "time": 0.0, "stream_id": "b"})
    assert any(event["label"] == "face_redacted" for event in first["events"])
    assert not any(event.get("label") == "face_redacted" for event in second["events"])


def test_runtime_face_backend_failure_blocks_frame():
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("face",), fail_policy="block_frame", require_face_backend=True,
        face_detector=_FailingDetector(), person_head_fallback=False,
    )
    result = processor.process(_frame(), {"frame_index": 0, "time": 0.0})
    assert result["events"][0]["label"] == "privacy_frame_blocked"
