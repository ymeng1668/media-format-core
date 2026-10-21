from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ai_privacy_redaction import PrivacyRedactionProcessor
from core import validate_frame_result
from core.privacy_plate_ocr import PlateCandidate, PlateOCRResult


class _CandidateDetector:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = 0
        self.last_vehicle_count = 0

    def detect(self, _frame, vehicles):
        self.calls += 1
        self.last_vehicle_count = len(vehicles)
        return list(self.rows)


class _OCR:
    ready = True

    def __init__(self, *, text: str = "", confidence: float = 0.0):
        self.text = text
        self.confidence = confidence
        self.calls = 0

    def recognize(self, _frame, candidate):
        self.calls += 1
        recognizable = bool(self.text)
        crop = np.full((80, 240, 3), 200, dtype=np.uint8)
        return PlateOCRResult(
            text=self.text if recognizable else "",
            normalized=self.text,
            confidence=self.confidence,
            recognizable=recognizable,
            crop_bgr=crop,
            rectified_bgr=crop,
            reason="" if recognizable else "not_readable",
            alternatives=[{"text": self.text, "candidate_score": candidate.score}] if recognizable else [],
        )


def _frame():
    image = np.zeros((160, 320, 3), dtype=np.uint8)
    image[50:130, 60:260] = (90, 100, 110)
    image[105:122, 135:195] = (240, 240, 240)
    return image


def _meta(frame_index=0):
    return {
        "frame_index": frame_index,
        "time": frame_index / 15.0,
        "stream_id": "test",
        "object_tracks": [
            {
                "kind": "track",
                "label": "car",
                "confidence": 0.96,
                "bbox": {"x": 60, "y": 50, "w": 200, "h": 80},
                "track": {"id": 7, "state": "confirmed"},
            }
        ],
    }


def _candidate(score=7.0, vehicle_id=7):
    return PlateCandidate(
        vehicle_track_id=vehicle_id,
        xywh=(135.0, 105.0, 60.0, 17.0),
        score=score,
        detector_confidence=0.9,
        source="fake_vehicle_plate",
    )


def test_no_vehicle_track_means_no_plate_search_or_mask():
    detector = _CandidateDetector([_candidate()])
    ocr = _OCR(text="ABC1234", confidence=0.9)
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("license_plate",),
        plate_candidate_detector=detector,
        plate_ocr=ocr,
        plate_ocr_async=False,
        plate_detect_every=1,
    )
    result = processor.process(_frame(), {"frame_index": 0, "time": 0.0})
    ok, reason = validate_frame_result(result, strict=True)
    assert ok, reason
    assert detector.calls == 0
    assert ocr.calls == 0
    assert not any(event["kind"] == "mask" for event in result["events"])
    assert result["metrics"]["plate"]["candidate_reason"] == "no_vehicle_tracks"


def test_unrecognized_plate_is_not_masked():
    detector = _CandidateDetector([_candidate()])
    ocr = _OCR(text="", confidence=0.0)
    source = _frame()
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("license_plate",),
        plate_candidate_detector=detector,
        plate_ocr=ocr,
        plate_ocr_async=False,
        plate_detect_every=1,
        emit_plate_candidates=True,
        plate_mask_policy="recognized_only",
    )
    result = processor.process(source, _meta())
    assert detector.last_vehicle_count == 1
    assert ocr.calls == 1
    assert any(event["label"] == "license_plate_candidate" for event in result["events"])
    assert not any(event["kind"] == "mask" for event in result["events"])
    assert np.array_equal(source, result["frame"])



def test_unrecognized_plate_is_masked_when_policy_is_all_detected():
    detector = _CandidateDetector([_candidate()])
    ocr = _OCR(text="", confidence=0.0)
    source = _frame()
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("license_plate",),
        mode="solid",
        plate_candidate_detector=detector,
        plate_ocr=ocr,
        plate_ocr_async=False,
        plate_detect_every=1,
        max_plate_candidates_per_vehicle=1,
        plate_mask_policy="all_detected",
    )
    for index in range(3):
        result = processor.process(source, _meta(index))
        if index < 2:
            assert not any(item["kind"] == "mask" for item in result["events"])
    ok, reason = validate_frame_result(result, strict=True)
    assert ok, reason
    masks = [event for event in result["events"] if event["kind"] == "mask"]
    assert len(masks) == 1
    assert masks[0]["label"] == "license_plate_candidate_redacted"
    assert masks[0]["attributes"]["recognized"] is False
    assert masks[0]["attributes"]["vehicle_track_id"] == 7
    assert not np.array_equal(source, result["frame"])

def test_recognized_plate_is_masked_and_text_emitted():
    detector = _CandidateDetector([_candidate()])
    ocr = _OCR(text="ABC1234", confidence=0.91)
    source = _frame()
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("license_plate",),
        mode="solid",
        plate_candidate_detector=detector,
        plate_ocr=ocr,
        plate_ocr_async=False,
        plate_detect_every=1,
        plate_confirm_hits=1,
        plate_ocr_high_confidence=0.8,
        plate_mask_policy="recognized_only",
    )
    result = processor.process(source, _meta())
    ok, reason = validate_frame_result(result, strict=True)
    assert ok, reason
    masks = [event for event in result["events"] if event["kind"] == "mask"]
    texts = [event for event in result["events"] if event["kind"] == "text"]
    assert len(masks) == 1
    assert masks[0]["attributes"]["vehicle_track_id"] == 7
    assert masks[0]["attributes"]["plate_text"] == "ABC1234"
    assert texts[0]["text"]["value"] == "ABC1234"
    assert not np.array_equal(source, result["frame"])


def test_only_configured_top_candidates_are_sent_to_ocr():
    candidates = [
        PlateCandidate(7, (130.0 + i * 3, 105.0, 60.0, 17.0), 10.0 - i, 0.9, f"fake{i}")
        for i in range(4)
    ]
    detector = _CandidateDetector(candidates)
    ocr = _OCR(text="", confidence=0.0)
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("license_plate",),
        plate_candidate_detector=detector,
        plate_ocr=ocr,
        plate_ocr_async=False,
        plate_detect_every=1,
        max_plate_candidates_per_vehicle=2,
    )
    processor.process(_frame(), _meta())
    # Overlapping candidates are NMS-filtered before OCR and never exceed K.
    assert 0 < ocr.calls <= 2


def test_recognized_plate_crop_is_saved(tmp_path: Path):
    detector = _CandidateDetector([_candidate()])
    ocr = _OCR(text="ABC1234", confidence=0.95)
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("license_plate",),
        plate_candidate_detector=detector,
        plate_ocr=ocr,
        plate_ocr_async=False,
        plate_detect_every=1,
        plate_confirm_hits=1,
        plate_ocr_high_confidence=0.8,
        save_recognized_plates=True,
        plate_output_dir=str(tmp_path),
    )
    result = processor.process(_frame(), _meta())
    mask = next(event for event in result["events"] if event["kind"] == "mask")
    image_path = Path(mask["attributes"]["plate_crop_path"])
    assert image_path.is_file()
    assert image_path.suffix.lower() == ".jpg"
    assert (tmp_path / "recognized_plates.jsonl").is_file()
    saved = cv2.imread(str(image_path))
    assert saved is not None and saved.size > 0


def test_async_ocr_result_masks_on_later_frame():
    import time

    detector = _CandidateDetector([_candidate()])
    ocr = _OCR(text="ABC1234", confidence=0.95)
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False,
        targets=("license_plate",),
        plate_candidate_detector=detector,
        plate_ocr=ocr,
        plate_ocr_async=True,
        plate_detect_every=1,
        plate_confirm_hits=1,
        plate_ocr_high_confidence=0.8,
    )
    first = processor.process(_frame(), _meta(0))
    assert not any(event["kind"] == "mask" for event in first["events"])
    masked = False
    for index in range(1, 10):
        time.sleep(0.01)
        result = processor.process(_frame(), _meta(index))
        if any(event["kind"] == "mask" for event in result["events"]):
            masked = True
            break
    processor.close()
    assert masked
