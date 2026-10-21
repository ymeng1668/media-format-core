"""Additional coverage for the standalone privacy entry point."""

import numpy as np
import pytest
import json

from privacy_redaction import PrivacyRedactionProcessor
from core.privacy_tracking import PrivacyDetection
from test_privacy_redaction import _SequenceDetector, _frame
from test_privacy_plate_recognition import _CandidateDetector, _candidate, _meta


def test_preview_defaults_do_not_export_plate_identity():
    processor = PrivacyRedactionProcessor(model_auto_discovery=False)
    try:
        assert processor.automatic_detection_required
        assert not processor.emit_plate_text
        assert not processor.save_recognized_plates
        assert processor.plate_ocr_enabled
        assert processor.mode == "highlight"
        assert not processor.apply_redaction
        assert not processor.emit_plate_candidates
        assert processor.fail_policy == "hold_last"
    finally:
        processor.close()


@pytest.mark.parametrize("mode", ["solid", "pixelate", "blur", "highlight"])
def test_face_processing_and_visible_highlight(mode):
    processor = PrivacyRedactionProcessor(
        mode=mode, targets=["face"], model_auto_discovery=False,
        auto_object_track=False, person_head_fallback=False,
        face_detector=_SequenceDetector([[PrivacyDetection("face", (35, 25, 40, 40), 0.95, "test")]]))
    try:
        source = _frame()
        # Add texture so pixelation/blur have a measurable effect.
        source[25:65, 35:75:2] = (255, 0, 0)
        result = processor.process(source, {"frame_index": 0, "time": 0.0})
        assert result["valid"]
        assert any("face" in event["label"] for event in result["events"])
        assert not np.array_equal(result["frame"], source)
        assert not any(event["label"] == "privacy_frame_blocked" for event in result["events"])
    finally:
        processor.close()


def test_confirmed_plate_mask_changes_only_the_intended_region():
    processor = PrivacyRedactionProcessor(
        mode="solid", targets=["license_plate"], model_auto_discovery=False,
        automatic_detection_required=False, auto_object_track=False,
        plate_candidate_detector=_CandidateDetector([_candidate()]), plate_detect_every=1,
        plate_ocr_enabled=False)
    source = np.full((160, 320, 3), 170, dtype=np.uint8)
    try:
        for index in range(3):
            result = processor.process(source, _meta(index))
        assert np.count_nonzero(result["frame"][110:117, 140:190]) == 0
        assert np.all(result["frame"][0:20, 0:20] == 170)
        assert any(event["kind"] == "mask" for event in result["events"])
    finally:
        processor.close()


def test_missing_required_model_blocks_without_crashing():
    processor = PrivacyRedactionProcessor(
        targets=["face"], model_auto_discovery=False, face_model="missing-model.onnx",
        auto_object_track=False, person_head_fallback=False)
    try:
        result = processor.process(_frame(), {"frame_index": 0, "time": 0.0})
        assert np.count_nonzero(result["frame"]) == 0
        assert any(event["label"] == "privacy_frame_blocked" for event in result["events"])
    finally:
        processor.close()


def test_config_file_and_explicit_overrides_win_over_entrypoint_defaults(tmp_path):
    config = tmp_path / "privacy.json"
    config.write_text(json.dumps({"preset": "strict", "mode": "solid",
                                 "plate_ocr_enabled": False}))
    processor = PrivacyRedactionProcessor(config_path=str(config), model_auto_discovery=False)
    try:
        assert processor.mode == "solid" and processor.apply_redaction
        assert processor.fail_policy == "block_frame"
        assert not processor.plate_ocr_enabled
    finally:
        processor.close()
    processor = PrivacyRedactionProcessor(config_path=str(config), mode="highlight",
                                         plate_ocr_enabled=True, model_auto_discovery=False)
    try:
        assert processor.mode == "highlight" and not processor.apply_redaction
        assert processor.plate_ocr_enabled
    finally:
        processor.close()


def test_preview_has_inside_green_border_and_label_but_does_not_draw_masks():
    from core.privacy_highlight import draw_highlights
    source = np.full((80, 240, 3), 170, dtype=np.uint8)
    frame = source.copy()
    event = {"kind": "box", "label": "face_detected", "confidence": 0.9,
             "bbox": {"x": 30, "y": 30, "w": 100, "h": 40}}
    draw_highlights(frame, [event])
    assert np.all(frame[50, 29] == 170)  # no stroke outside the rectangle
    assert frame[50, 30, 1] > frame[50, 30, 0]  # green outline
    assert np.all(frame[50, 33:127] == 170)  # face interior is untouched
    assert np.any(frame[17:29, 30:200] != source[17:29, 30:200])  # label
    frame = source.copy()
    draw_highlights(frame, [{**event, "kind": "mask"}])
    assert np.array_equal(frame, source)
