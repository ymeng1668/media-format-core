"""V5 configuration and privacy safety contracts, not detector accuracy claims."""

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ai_privacy_redaction import PRIVACY_REDACTION_VERSION, PrivacyRedactionProcessor
from core.privacy_presets import discover_privacy_models, resolve_privacy_configuration
from core.privacy_detectors import OpenCVYoloV9PlateDetector
from core.privacy_tracking import PrivacyDetection, PrivacyTracker
from test_privacy_plate_recognition import _CandidateDetector, _candidate, _frame, _meta
from test_privacy_redaction import _SequenceDetector


@pytest.fixture
def empty_models(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_FORMAT_AI_MODEL_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MEDIA_FORMAT_PRIVACY_MODEL_DIR", str(tmp_path / "privacy"))
    for key in ("FACE", "PERSON", "PLATE"):
        monkeypatch.delenv(f"MEDIA_FORMAT_PRIVACY_{key}_MODEL", raising=False)
    monkeypatch.delenv("MEDIA_FORMAT_PRIVACY_PRESET", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_canonical_v5_processor_has_no_obsolete_base_dependency():
    import ai_privacy_redaction as module

    assert PRIVACY_REDACTION_VERSION == "5.0"
    assert not Path(module.__file__).with_name("ai_privacy_redaction_v2_base.py").exists()
    assert PrivacyRedactionProcessor.describe()["implementation_version"] == "5.0"


@pytest.mark.parametrize("width,height,fps,offline,expected", [
    (1920, 1080, 30, False, "balanced"),
    (3840, 2160, 30, False, "fast"),
    (1920, 1080, 60, False, "fast"),
    (1920, 1080, 30, True, "archive"),
])
def test_automatic_preset_selection(empty_models, width, height, fps, offline, expected):
    resolved = resolve_privacy_configuration(
        source_width=width, source_height=height, source_fps=fps, offline=offline,
        model_auto_discovery=False,
    )
    assert resolved["selected_preset"] == expected


def test_config_and_explicit_option_precedence(empty_models):
    config = empty_models / "privacy.json"
    config.write_text(json.dumps({
        "preset": "fast", "output": {"mode": "solid"},
        "plate": {"mask_policy": "all_detected", "fallback_confirm_hits": 5},
    }))
    resolved = resolve_privacy_configuration(
        config_path=config, model_auto_discovery=False,
        explicit={"mode": "highlight", "face_detect_every": 7},
    )
    assert resolved["selected_preset"] == "fast"
    assert resolved["config"]["mode"] == "highlight"
    assert resolved["config"]["face_detect_every"] == 7
    assert resolved["config"]["plate_mask_policy"] == "confirmed_detected"
    assert resolved["config"]["plate_fallback_confirm_hits"] == 5
    assert resolved == resolve_privacy_configuration(
        config_path=config, model_auto_discovery=False,
        explicit={"mode": "highlight", "face_detect_every": 7},
    )


@pytest.mark.parametrize("values", [
    {"preset": "unknown"},
    {"explicit": {"plate_mask_policy": "unknown"}},
    {"explicit": {"plate_fallback_mode": "unknown"}},
])
def test_invalid_configuration_is_rejected(empty_models, values):
    with pytest.raises(ValueError):
        resolve_privacy_configuration(model_auto_discovery=False, **values)


def test_cache_discovery_and_explicit_package_relative_paths(empty_models):
    cache = empty_models / "cache"
    cache.mkdir()
    face = cache / "face_detection_yunet_2023mar.onnx"
    face.write_bytes(b"x" * 120_000)
    result = discover_privacy_models(package_dir=empty_models / "package")
    assert result["face_model"].usable
    assert Path(result["face_model"].path) == face
    resolved = resolve_privacy_configuration(
        package_dir=empty_models / "package",
        explicit={"face_model": "models/face_detection_yunet_2023mar.onnx"},
    )
    assert resolved["config"]["face_model"] == str(face)
    assert not resolved["automatic_detection_ready_static"]  # no plate model


def test_lfs_pointer_and_small_models_are_not_discovered(empty_models):
    models = empty_models / "package" / "models"
    models.mkdir(parents=True)
    face = models / "face_detection_yunet_2023mar.onnx"
    face.write_text("version https://git-lfs.github.com/spec/v1\n")
    plate = models / "yolo-v9-t-512-license-plates-end2end.onnx"
    plate.write_bytes(b"not a model")
    result = discover_privacy_models(package_dir=models.parent)
    assert not result["face_model"].usable
    assert not result["plate_model"].usable


def test_explicit_missing_model_does_not_fall_back_to_different_model(empty_models):
    cache = empty_models / "cache"
    cache.mkdir()
    (cache / "face_detection_yunet_2023mar.onnx").write_bytes(b"x" * 120_000)
    result = discover_privacy_models(configured={"face_model": "missing.onnx"})
    assert not result["face_model"].usable
    assert result["face_model"].source == "explicit"


def test_false_plate_requires_three_stable_fallback_observations():
    detector = _CandidateDetector([_candidate()])
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False, targets=["license_plate"], mode="solid",
        plate_ocr_enabled=False, plate_detect_every=1, plate_candidate_detector=detector,
    )
    try:
        for index in range(3):
            result = processor.process(_frame(), _meta(index))
            masks = [row for row in result["events"] if row["kind"] == "mask"]
            assert bool(masks) == (index == 2)
        assert masks[0]["attributes"]["candidate_hits"] == 3
        assert not masks[0]["attributes"]["recognized"]
    finally:
        processor.close()


def test_unstable_candidates_do_not_become_masks():
    detector = _CandidateDetector([])
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False, targets=["license_plate"],
        plate_ocr_enabled=False, plate_detect_every=1, plate_candidate_detector=detector,
    )
    try:
        for index in range(12):
            detector.rows = [replace(_candidate(),
                                     xywh=((85.0, 135.0, 200.0)[index % 3], 105.0, 25.0, 10.0))]
            result = processor.process(_frame(), _meta(index))
            assert not any(row["kind"] == "mask" for row in result["events"])
    finally:
        processor.close()


@pytest.mark.parametrize("label", ["carpet", "scar", "face_redacted", "traffic_sign"])
def test_non_vehicle_labels_do_not_create_vehicle_regions(label):
    detector = _CandidateDetector([_candidate()])
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False, targets=["license_plate"],
        plate_ocr_enabled=False, plate_candidate_detector=detector,
    )
    try:
        meta = _meta()
        meta["object_tracks"][0]["label"] = label
        result = processor.process(_frame(), meta)
        assert result["metrics"]["plate"]["vehicle_tracks"] == 0
        assert detector.calls == 0
    finally:
        processor.close()


def test_plate_track_cannot_transfer_between_overlapping_vehicles():
    tracker = PrivacyTracker(start_hits=1, max_missed=1)
    first = PrivacyDetection("license_plate", (135, 105, 60, 17), 0.9, "test",
                             {"vehicle_track_id": 1})
    second = replace(first, attributes={"vehicle_track_id": 2})
    before = tracker.update([first], frame_index=0, width=320, height=160)
    after = tracker.update([second], frame_index=1, width=320, height=160)
    observed = next(track for track in after if not track.predicted)
    assert observed.track_id != before[0].track_id
    assert observed.attributes["vehicle_track_id"] == 2


def test_diagnostic_mode_still_blocks_when_automatic_detection_is_required():
    processor = PrivacyRedactionProcessor(
        preset="diagnostic", model_auto_discovery=False,
        automatic_detection_required=True, targets=["face"],
        face_detector=_SequenceDetector([], ready=False), person_head_fallback=False,
    )
    try:
        result = processor.process(_frame(), {"frame_index": 0, "time": 0.0})
        assert any(row["label"] == "privacy_frame_blocked" for row in result["events"])
        assert not np.any(result["frame"])
    finally:
        processor.close()


def test_disabled_redaction_cannot_disable_explicit_fail_closed():
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False, targets=["face"], apply_redaction=False,
        fail_policy="block_frame", require_face_backend=True,
        face_detector=_SequenceDetector([], ready=False), person_head_fallback=False,
    )
    try:
        result = processor.process(_frame(), {"frame_index": 0, "time": 0.0})
        assert result["events"][0]["label"] == "privacy_frame_blocked"
        assert not np.any(result["frame"])
    finally:
        processor.close()


def test_readiness_does_not_claim_unvalidated_dependency_is_ready():
    class Model:
        readiness = SimpleNamespace(ready=True, reason="", backend="test")

    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False, targets=["license_plate"], plate_detector=Model(),
    )
    try:
        assert not processor.automatic_readiness()["ready"]
        processor.set_dependency_readiness("object_track", True)
        assert processor.automatic_readiness()["ready"]
        processor.set_dependency_readiness("object_track", False, "missing")
        assert not processor.automatic_readiness()["ready"]
    finally:
        processor.close()


def test_onnx_runtime_fallback_supports_end_to_end_nms(tmp_path, monkeypatch):
    import cv2

    model = tmp_path / "plate.onnx"
    model.write_bytes(b"model fixture")

    def unsupported(_path):
        raise RuntimeError("NonMaxSuppression is unsupported")

    class Session:
        def get_inputs(self):
            return [SimpleNamespace(name="images", shape=[1, 3, 512, 512], type="tensor(float)")]

        def run(self, _outputs, inputs):
            assert inputs["images"].shape == (1, 3, 512, 512)
            assert inputs["images"].dtype == np.float32
            return [np.asarray([[0, 100, 120, 220, 170, 0, 0.91]], dtype=np.float32)]

    calls = []

    def create_session(path, *, sess_options, providers):
        calls.append(path)
        assert providers == ["CPUExecutionProvider"]
        assert sess_options.intra_op_num_threads == 1
        return Session()

    monkeypatch.setattr(cv2.dnn, "readNetFromONNX", unsupported)
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        SessionOptions=SimpleNamespace, InferenceSession=create_session,
    ))
    detector = OpenCVYoloV9PlateDetector(str(model))
    for _ in range(2):
        detections = detector.detect(np.zeros((512, 512, 3), dtype=np.uint8))
        assert len(detections) == 1
        assert detections[0].xywh == (100.0, 120.0, 120.0, 50.0)
        assert detections[0].source == "onnxruntime_cpu_yolov9_plate"
    assert len(calls) == 1
    assert detector.readiness.ready
    assert detector.readiness.backend == "onnxruntime_cpu"


def test_missing_optional_onnx_runtime_does_not_crash(tmp_path, monkeypatch):
    import cv2

    model = tmp_path / "plate.onnx"
    model.write_bytes(b"model fixture")

    def unsupported(_path):
        raise RuntimeError("NonMaxSuppression is unsupported")

    monkeypatch.setattr(cv2.dnn, "readNetFromONNX", unsupported)
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    detector = OpenCVYoloV9PlateDetector(str(model))
    assert detector.detect(np.zeros((512, 512, 3), dtype=np.uint8)) == []
    assert not detector.readiness.ready
    assert "onnxruntime" in detector.readiness.reason


def test_required_lazy_plate_backend_is_checked_even_without_vehicles():
    class BrokenModel:
        readiness = SimpleNamespace(ready=True, reason="lazy", backend="test")

        def __init__(self):
            self.calls = 0

        def detect(self, _frame, **_kwargs):
            self.calls += 1
            self.readiness = SimpleNamespace(ready=False, reason="invalid_model", backend="test")
            return []

    model = BrokenModel()
    processor = PrivacyRedactionProcessor(
        model_auto_discovery=False, targets=["license_plate"],
        automatic_detection_required=True, plate_ocr_enabled=False, plate_detector=model,
    )
    try:
        for index in range(2):
            result = processor.process(_frame(), {"frame_index": index, "object_tracks": []})
            assert any(row["label"] == "privacy_frame_blocked" for row in result["events"])
            assert not np.any(result["frame"])
        assert model.calls == 1
    finally:
        processor.close()
