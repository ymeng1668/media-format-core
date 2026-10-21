"""Automatic, preset-driven face and vehicle-plate privacy redaction.

V5 keeps V4's false-positive gates and adds a complete automatic deployment
layer: local model discovery, adaptive presets, JSON configuration, truthful
readiness reporting, and fail-closed operation when automatic detection is
required.  Plate proposals remain restricted to stable tracked vehicles.

The supported plate policies are ``recognized_only`` and
``confirmed_detected``.  The legacy name ``all_detected`` is accepted as an
alias for ``confirmed_detected``; neither policy masks an arbitrary one-frame
rectangle.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
import math
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from _common import bbox, event, process_meta, _result
from core.privacy_face_pipeline import (
    PrivacyFacePipeline,
    _event_box,
    _is_vehicle_label,
    _iter_upstream_events,
    _norm_label,
)
from core.contracts import frame_result
from core.geometry import bbox_iou_xywh, clip_bbox_xywh
from core.privacy_detectors import OpenCVYoloV9PlateDetector
from core.privacy_presets import resolve_privacy_configuration
from core.privacy_plate_ocr import (
    PlateCandidate,
    PlateOCRResult,
    TesseractPlateOCR,
    VehiclePlateCandidateDetector,
    VehicleRegion,
    save_recognized_plate,
)
from core.privacy_redaction import apply_privacy_mask, expand_privacy_box
from core.privacy_tracking import PrivacyDetection, PrivacyTracker

PRIVACY_REDACTION_VERSION = "5.0"



@dataclass(slots=True)
class _PlateProposalEvidence:
    relative_box: tuple[float, float, float, float]
    candidate: PlateCandidate
    hits: int = 1
    misses: int = 0
    first_frame: int = -1
    last_frame: int = -1
    score_ema: float = 0.0
    confidence_ema: float = 0.0
    confirmed: bool = False

    def update(
        self,
        candidate: PlateCandidate,
        relative_box: tuple[float, float, float, float],
        *,
        frame_index: int,
        smooth: float = 0.60,
    ) -> None:
        alpha = float(np.clip(smooth, 0.0, 1.0))
        self.relative_box = tuple(
            (1.0 - alpha) * old + alpha * new
            for old, new in zip(self.relative_box, relative_box)
        )  # type: ignore[assignment]
        self.candidate = candidate
        self.hits += 1
        self.misses = 0
        self.last_frame = int(frame_index)
        self.score_ema = 0.65 * self.score_ema + 0.35 * float(candidate.score)
        self.confidence_ema = 0.65 * self.confidence_ema + 0.35 * float(candidate.detector_confidence)


@dataclass(slots=True)
class _VehiclePlateState:
    vehicle_track_id: int
    last_vehicle_box: tuple[float, float, float, float] | None = None
    last_candidate_box: tuple[float, float, float, float] | None = None
    relative_box: tuple[float, float, float, float] | None = None
    last_candidate_score: float = 0.0
    last_seen_frame: int = -1
    last_ocr_frame: int = -1_000_000
    ocr_attempts: int = 0
    best_quality: float = 0.0
    votes: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    confirmed_text: str = ""
    confirmed_confidence: float = 0.0
    confirmed_frame: int = -1
    crop_path: str = ""
    metadata_path: str = ""
    last_ocr_reason: str = ""
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    selected_candidates: list[PlateCandidate] = field(default_factory=list)
    candidate_vehicle_box: tuple[float, float, float, float] | None = None
    last_candidate_frame: int = -1
    saved_text: str = ""
    proposal_evidence: list[_PlateProposalEvidence] = field(default_factory=list)
    ocr_relative_box: tuple[float, float, float, float] | None = None

    def vote(self, result: PlateOCRResult, *, max_votes: int = 12) -> tuple[str, float, int]:
        if not result.recognizable or not result.text:
            return "", 0.0, 0
        bucket = self.votes.setdefault(result.text, [])
        bucket.append(float(result.confidence))
        if len(bucket) > max_votes:
            del bucket[: len(bucket) - max_votes]
        if len(self.votes) > max_votes:
            oldest = next(key for key in self.votes if key != result.text)
            del self.votes[oldest]
        text, scores = max(
            self.votes.items(),
            key=lambda item: (len(item[1]), sum(item[1]) / max(len(item[1]), 1)),
        )
        return text, float(sum(scores) / max(len(scores), 1)), len(scores)


@dataclass(slots=True)
class _PendingOCR:
    future: Future[tuple[PlateCandidate | None, PlateOCRResult]]
    vehicle_track_id: int
    frame_index: int
    pts: float
    stream_id: str
    vehicle_box: tuple[float, float, float, float]


def _candidate_quality(frame_rgb: np.ndarray, box: tuple[float, float, float, float]) -> float:
    height, width = frame_rgb.shape[:2]
    clipped = clip_bbox_xywh(box, width, height, min_size=2.0)
    if clipped is None:
        return 0.0
    x, y, w, h = (int(round(value)) for value in clipped)
    crop = frame_rgb[y:y + h, x:x + w]
    if crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_32F).var())
    contrast = float(gray.std())
    area = float(w * h)
    return math.log1p(area) * (1.0 + min(sharpness, 500.0) / 500.0) * (1.0 + min(contrast, 80.0) / 160.0)


def _relative_box(
    plate_box: tuple[float, float, float, float],
    vehicle_box: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    px, py, pw, ph = plate_box
    vx, vy, vw, vh = vehicle_box
    if vw <= 1.0 or vh <= 1.0:
        return None
    return (
        (px - vx) / vw,
        (py - vy) / vh,
        pw / vw,
        ph / vh,
    )


def _project_relative(
    relative: tuple[float, float, float, float],
    vehicle_box: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[float, float, float, float] | None:
    rx, ry, rw, rh = relative
    vx, vy, vw, vh = vehicle_box
    return clip_bbox_xywh((vx + rx * vw, vy + ry * vh, rw * vw, rh * vh), width, height, min_size=2.0)


class PrivacyRedactionProcessor:
    """Canonical media-format face/head and vehicle-plate privacy transform."""

    def __init__(
        self,
        *,
        preset: str = "auto",
        config_path: str = "",
        model_auto_discovery: bool | None = None,
        automatic_detection_required: bool | None = None,
        source_width: int = 0,
        source_height: int = 0,
        source_fps: float = 0.0,
        offline: bool = False,
        **options: Any,
    ) -> None:
        # Detector/recognizer injections are intentionally outside JSON/preset
        # configuration.  They are used by tests and embedded hosts.
        face_detector = options.pop("face_detector", None)
        person_detector = options.pop("person_detector", None)
        plate_detector = options.pop("plate_detector", None)
        plate_candidate_detector = options.pop("plate_candidate_detector", None)
        plate_ocr = options.pop("plate_ocr", None)
        if automatic_detection_required is not None:
            options["automatic_detection_required"] = bool(automatic_detection_required)
        if model_auto_discovery is not None:
            options["model_auto_discovery"] = bool(model_auto_discovery)
        self._resolved_privacy = resolve_privacy_configuration(
            preset=preset,
            config_path=config_path,
            explicit=options,
            package_dir=Path(__file__).resolve().parent,
            model_auto_discovery=True if model_auto_discovery is None else bool(model_auto_discovery),
            source_width=int(source_width or 0),
            source_height=int(source_height or 0),
            source_fps=float(source_fps or 0.0),
            offline=bool(offline),
        )
        effective = self._resolved_privacy["config"]
        targets = effective['targets']
        mode = effective['mode']
        pixel_block = effective['pixel_block']
        fail_policy = effective['fail_policy']
        block_mode = effective['block_mode']
        apply_redaction = effective['apply_redaction']
        require_face_backend = effective['require_face_backend']
        face_model = effective['face_model']
        face_detect_every = effective['face_detect_every']
        face_full_scan_every = effective['face_full_scan_every']
        face_max_dimension = effective['face_max_dimension']
        face_score_threshold = effective['face_score_threshold']
        face_nms_threshold = effective['face_nms_threshold']
        face_roi_scan = effective['face_roi_scan']
        person_head_fallback = effective['person_head_fallback']
        face_min_confidence = effective['face_min_confidence']
        face_high_confidence = effective['face_high_confidence']
        face_confirm_hits = effective['face_confirm_hits']
        face_confirm_max_missed = effective['face_confirm_max_missed']
        face_min_size_px = effective['face_min_size_px']
        face_max_frame_fraction = effective['face_max_frame_fraction']
        person_model = effective['person_model']
        person_detect_every = effective['person_detect_every']
        person_min_confidence = effective['person_min_confidence']
        person_head_high_confidence = effective['person_head_high_confidence']
        person_head_min_hits = effective['person_head_min_hits']
        person_head_max_missed = effective['person_head_max_missed']
        person_min_height_px = effective['person_min_height_px']
        max_predicted_face_age = effective['max_predicted_face_age']
        face_expand_width = effective['face_expand_width']
        face_expand_height = effective['face_expand_height']
        max_person_rois = effective['max_person_rois']
        plate_model = effective['plate_model']
        plate_detect_every = effective['plate_detect_every']
        plate_require_vehicle = effective['plate_require_vehicle']
        vehicle_require_track_id = effective['vehicle_require_track_id']
        vehicle_min_confidence = effective['vehicle_min_confidence']
        vehicle_min_width_px = effective['vehicle_min_width_px']
        vehicle_min_height_px = effective['vehicle_min_height_px']
        max_plate_candidates_per_vehicle = effective['max_plate_candidates_per_vehicle']
        plate_candidate_min_score = effective['plate_candidate_min_score']
        plate_candidate_confirm_hits = effective['plate_candidate_confirm_hits']
        plate_fallback_confirm_hits = effective['plate_fallback_confirm_hits']
        plate_candidate_high_confidence = effective['plate_candidate_high_confidence']
        plate_candidate_match_iou = effective['plate_candidate_match_iou']
        plate_candidate_center_gate = effective['plate_candidate_center_gate']
        plate_candidate_max_missed_scans = effective['plate_candidate_max_missed_scans']
        plate_candidate_min_width_px = effective['plate_candidate_min_width_px']
        plate_candidate_min_height_px = effective['plate_candidate_min_height_px']
        plate_candidate_aspect_min = effective['plate_candidate_aspect_min']
        plate_candidate_aspect_max = effective['plate_candidate_aspect_max']
        plate_candidate_relative_width_min = effective['plate_candidate_relative_width_min']
        plate_candidate_relative_width_max = effective['plate_candidate_relative_width_max']
        plate_candidate_relative_height_min = effective['plate_candidate_relative_height_min']
        plate_candidate_relative_height_max = effective['plate_candidate_relative_height_max']
        plate_candidate_relative_y_min = effective['plate_candidate_relative_y_min']
        plate_candidate_relative_y_max = effective['plate_candidate_relative_y_max']
        plate_mask_policy = effective['plate_mask_policy']
        plate_fallback_mode = effective['plate_fallback_mode']
        plate_ocr_enabled = effective['plate_ocr_enabled']
        plate_ocr_async = effective['plate_ocr_async']
        plate_ocr_retry_interval = effective['plate_ocr_retry_interval']
        plate_ocr_quality_improvement = effective['plate_ocr_quality_improvement']
        plate_ocr_max_attempts_per_vehicle = effective['plate_ocr_max_attempts_per_vehicle']
        plate_ocr_min_confidence = effective['plate_ocr_min_confidence']
        plate_ocr_high_confidence = effective['plate_ocr_high_confidence']
        plate_confirm_hits = effective['plate_confirm_hits']
        plate_region_profile = effective['plate_region_profile']
        save_recognized_plates = effective['save_recognized_plates']
        plate_output_dir = effective['plate_output_dir']
        emit_plate_text = effective['emit_plate_text']
        emit_plate_candidates = effective['emit_plate_candidates']
        plate_expand_width = effective['plate_expand_width']
        plate_expand_height = effective['plate_expand_height']
        max_predicted_plate_age = effective['max_predicted_plate_age']
        max_plate_tracks = effective['max_plate_tracks']
        max_pending_ocr = effective['max_pending_ocr']
        auto_object_track = effective['auto_object_track']
        object_track_params = effective['object_track_params']
        providers = effective['providers']
        dnn_backend = effective['dnn_backend']
        require_plate_backend = effective['require_plate_backend']
        start_hits = effective['start_hits']
        max_missed_frames = effective['max_missed_frames']
        max_masks = effective['max_masks']
        self.preset = str(self._resolved_privacy["selected_preset"])
        self.requested_preset = str(self._resolved_privacy["requested_preset"])
        self.config_path = str(self._resolved_privacy.get("config_path", ""))
        self.config_digest = str(self._resolved_privacy["config_digest"])
        self.model_auto_discovery = bool(effective["model_auto_discovery"])
        self.automatic_detection_required = bool(effective["automatic_detection_required"])
        self._unused_options = dict(options)
        if isinstance(targets, str):
            values = [part.strip() for part in targets.split(",") if part.strip()]
        else:
            values = [str(part).strip() for part in targets if str(part).strip()]
        self.targets = {_norm_label(item) for item in values}
        unsupported = self.targets.difference({"face", "license_plate"})
        if unsupported:
            raise ValueError(f"unsupported privacy targets: {sorted(unsupported)}")
        if not self.targets:
            raise ValueError("privacy_redaction requires at least one target")
        self.mode = str(mode or "pixelate").lower()
        if self.mode not in {"pixelate", "blur", "solid", "highlight"}:
            raise ValueError("mode must be pixelate, blur, solid, or highlight")
        self.pixel_block = max(4, int(pixel_block))
        self.fail_policy = str(fail_policy or "best_effort").lower()
        if self.fail_policy not in {"best_effort", "hold_last", "block_frame"}:
            raise ValueError("fail_policy must be best_effort, hold_last, or block_frame")
        self.block_mode = str(block_mode or "solid").lower()
        if self.block_mode not in {"pixelate", "blur", "solid"}:
            raise ValueError("block_mode must be pixelate, blur, or solid")
        self.apply_redaction = bool(apply_redaction)
        self.plate_detect_every = max(1, int(plate_detect_every))
        self.plate_require_vehicle = bool(plate_require_vehicle)
        self.vehicle_require_track_id = bool(vehicle_require_track_id)
        self.vehicle_min_confidence = float(np.clip(vehicle_min_confidence, 0.0, 1.0))
        self.vehicle_min_width_px = max(8.0, float(vehicle_min_width_px))
        self.vehicle_min_height_px = max(8.0, float(vehicle_min_height_px))
        self.max_plate_candidates_per_vehicle = max(1, min(2, int(max_plate_candidates_per_vehicle)))
        self.plate_candidate_min_score = float(plate_candidate_min_score)
        self.plate_candidate_confirm_hits = max(1, int(plate_candidate_confirm_hits))
        self.plate_fallback_confirm_hits = max(self.plate_candidate_confirm_hits, int(plate_fallback_confirm_hits))
        self.plate_candidate_high_confidence = float(np.clip(plate_candidate_high_confidence, 0.0, 1.0))
        self.plate_candidate_match_iou = float(np.clip(plate_candidate_match_iou, 0.01, 0.95))
        self.plate_candidate_center_gate = max(0.05, float(plate_candidate_center_gate))
        self.plate_candidate_max_missed_scans = max(0, int(plate_candidate_max_missed_scans))
        self.plate_candidate_min_width_px = max(6.0, float(plate_candidate_min_width_px))
        self.plate_candidate_min_height_px = max(3.0, float(plate_candidate_min_height_px))
        self.plate_candidate_aspect_min = max(1.1, float(plate_candidate_aspect_min))
        self.plate_candidate_aspect_max = max(self.plate_candidate_aspect_min, float(plate_candidate_aspect_max))
        self.plate_candidate_relative_width_min = float(np.clip(plate_candidate_relative_width_min, 0.01, 0.90))
        self.plate_candidate_relative_width_max = float(np.clip(max(plate_candidate_relative_width_max, self.plate_candidate_relative_width_min), 0.02, 0.95))
        self.plate_candidate_relative_height_min = float(np.clip(plate_candidate_relative_height_min, 0.005, 0.80))
        self.plate_candidate_relative_height_max = float(np.clip(max(plate_candidate_relative_height_max, self.plate_candidate_relative_height_min), 0.01, 0.90))
        self.plate_candidate_relative_y_min = float(np.clip(plate_candidate_relative_y_min, 0.0, 1.0))
        self.plate_candidate_relative_y_max = float(np.clip(max(plate_candidate_relative_y_max, self.plate_candidate_relative_y_min), 0.0, 1.20))
        self.plate_mask_policy = str(plate_mask_policy or "confirmed_detected").lower()
        if self.plate_mask_policy not in {"recognized_only", "confirmed_detected"}:
            raise ValueError("plate_mask_policy must be recognized_only or confirmed_detected")
        self.plate_fallback_mode = str(plate_fallback_mode or "when_model_unavailable").lower()
        if self.plate_fallback_mode not in {"disabled", "when_model_unavailable", "augment"}:
            raise ValueError("plate_fallback_mode must be disabled, when_model_unavailable, or augment")
        self.plate_ocr_enabled = bool(plate_ocr_enabled)
        self.plate_ocr_async = bool(plate_ocr_async)
        self.plate_ocr_retry_interval = max(1, int(plate_ocr_retry_interval))
        self.plate_ocr_quality_improvement = max(0.0, float(plate_ocr_quality_improvement))
        self.plate_ocr_max_attempts_per_vehicle = max(1, int(plate_ocr_max_attempts_per_vehicle))
        self.plate_ocr_high_confidence = float(np.clip(plate_ocr_high_confidence, 0.0, 1.0))
        self.plate_confirm_hits = max(1, int(plate_confirm_hits))
        self.save_recognized_plates = bool(save_recognized_plates)
        self.plate_output_dir = str(plate_output_dir)
        self.emit_plate_text = bool(emit_plate_text)
        self.emit_plate_candidates = bool(emit_plate_candidates)
        self.plate_expand_width = max(1.0, float(plate_expand_width))
        self.plate_expand_height = max(1.0, float(plate_expand_height))
        self.max_predicted_plate_age = max(0, int(max_predicted_plate_age))
        self.require_plate_backend = bool(require_plate_backend)
        self.max_masks = max(1, int(max_masks))
        self.max_plate_tracks = max(1, int(max_plate_tracks))
        self.max_pending_ocr = max(1, min(int(max_pending_ocr), self.max_plate_tracks))
        self.auto_object_track = bool(auto_object_track)
        self.object_track_params = dict(object_track_params or {})
        self._object_tracker: Any = None
        self._object_executor: ThreadPoolExecutor | None = None
        self._object_tracker_error = ""
        self._last_context: tuple[str, int, float, int, int] | None = None
        self._ocr_errors: list[str] = []
        self._closed = False
        self._last_stream_id = ""
        self._object_track_dependency_valid = False
        self._automatic_backends_probed = False

        # Use the face-only pipeline; plate handling remains in this module.
        self._face_processor: PrivacyFacePipeline | None = None
        if "face" in self.targets:
            self._face_processor = PrivacyFacePipeline(
                targets=("face",),
                mode=self.mode,
                pixel_block=self.pixel_block,
                fail_policy=self.fail_policy,
                block_mode=self.block_mode,
                require_face_backend=bool(require_face_backend),
                face_model=face_model,
                face_detect_every=face_detect_every,
                face_full_scan_every=face_full_scan_every,
                face_max_dimension=face_max_dimension,
                face_score_threshold=face_score_threshold,
                face_nms_threshold=face_nms_threshold,
                face_roi_scan=face_roi_scan,
                person_head_fallback=person_head_fallback,
                face_min_confidence=face_min_confidence,
                face_high_confidence=face_high_confidence,
                face_confirm_hits=face_confirm_hits,
                face_confirm_max_missed=face_confirm_max_missed,
                face_min_size_px=face_min_size_px,
                face_max_frame_fraction=face_max_frame_fraction,
                person_model=person_model,
                person_detect_every=person_detect_every,
                person_min_confidence=person_min_confidence,
                person_head_high_confidence=person_head_high_confidence,
                person_head_min_hits=person_head_min_hits,
                person_head_max_missed=person_head_max_missed,
                person_min_height_px=person_min_height_px,
                max_predicted_face_age=max_predicted_face_age,
                face_expand_width=face_expand_width,
                face_expand_height=face_expand_height,
                max_person_rois=max_person_rois,
                max_masks=max_masks,
                apply_redaction=apply_redaction,
                face_detector=face_detector,
                person_detector=person_detector,
            )

        self._external_plate_detector = plate_detector
        if self._external_plate_detector is None and str(plate_model).strip():
            self._external_plate_detector = OpenCVYoloV9PlateDetector(
                str(plate_model),
                conf_threshold=0.20,
                input_size=512,
                dnn_backend=str(dnn_backend or "cpu"),
            )
        self._fallback_plate_detector = plate_candidate_detector or VehiclePlateCandidateDetector(
            max_candidates_per_vehicle=self.max_plate_candidates_per_vehicle,
            min_score=self.plate_candidate_min_score,
            min_width=int(round(self.plate_candidate_min_width_px)),
            min_height=int(round(self.plate_candidate_min_height_px)),
            aspect_min=self.plate_candidate_aspect_min,
            aspect_max=self.plate_candidate_aspect_max,
        )
        self._ocr = plate_ocr or TesseractPlateOCR(
            region_profile=plate_region_profile,
            min_confidence=float(plate_ocr_min_confidence),
        )
        self._states: dict[int, _VehiclePlateState] = {}
        self._pending: dict[int, _PendingOCR] = {}
        self._retired_ocr: list[Future] = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-format-plate-ocr") if self.plate_ocr_async and self.plate_ocr_enabled and "license_plate" in self.targets else None
        self._plate_tracker = PrivacyTracker(
            start_hits=max(1, int(start_hits)),
            max_missed=max(0, int(max_missed_frames)),
            match_iou=0.18,
            center_gate=0.75,
            smooth=0.72,
            max_tracks=max(8, int(max_plate_tracks)),
        )

    def _object_metadata(self, frame_rgb: np.ndarray, meta: dict[str, Any]) -> dict[str, Any]:
        # CoreAIGraph supplies the dependency. Direct CoreAIProcessor hosts call
        # the same registered producer here when no upstream result is present.
        upstream = meta.get("upstream", {})
        result = upstream.get("object_track") if isinstance(upstream, Mapping) else None
        if isinstance(result, Mapping):
            self._object_track_dependency_valid = bool(result.get("valid"))
            self._object_tracker_error = "" if self._object_track_dependency_valid else str(result.get("reason", "invalid_object_tracks"))
            return meta
        if any(isinstance(meta.get(key), list) for key in ("object_tracks", "tracks")):
            self._object_track_dependency_valid = True
            self._object_tracker_error = ""
            return meta
        if not self.auto_object_track:
            self._object_track_dependency_valid = False
            self._object_tracker_error = "object_track_missing"
            return meta
        if self._object_tracker is None and self._object_tracker_error:
            return meta
        try:
            # Torch tensor destruction must run on a Python-created thread.
            # Embedded C++ route threads may otherwise lose their active
            # CPython thread state through Torch's pybind11 GIL guard.
            if self._object_executor is None:
                self._object_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-format-privacy-objects")
            result = self._object_executor.submit(self._infer_objects, frame_rgb, meta).result()
            if not result.get("valid"):
                raise RuntimeError(str(result.get("reason", "invalid_object_tracks")))
            self._object_track_dependency_valid = True
            self._object_tracker_error = ""
            return {**meta, "upstream": {**dict(upstream), "object_track": result}}
        except Exception as exc:
            self._object_track_dependency_valid = False
            self._object_tracker_error = f"{type(exc).__name__}: {exc}"
            return meta

    def _infer_objects(self, frame_rgb: np.ndarray, meta: dict[str, Any]) -> dict[str, Any]:
        if self._object_tracker is None:
            from core.model_paths import resolve_model_ref
            from ai_object_track import ObjectTrackProcessor

            params = {"model": "yolov8n.pt", "tracker": "bytetrack", "device": "cpu",
                      "imgsz": 640, "conf": 0.25, "low_conf": 0.1,
                      "preprocess_mode": "native", "quality_probe_interval": 3,
                      "cleanup_interval": 0, **self.object_track_params}
            model_path = Path(resolve_model_ref(params.get("model", "yolov8n.pt")))
            if not model_path.is_file():
                raise FileNotFoundError(f"object_track model is not local: {model_path}")
            params["model"] = str(model_path)
            self._object_tracker = ObjectTrackProcessor(**params)
        return self._object_tracker.process(frame_rgb, meta)

    def _state(self, vehicle_id: int) -> _VehiclePlateState | None:
        state = self._states.get(vehicle_id)
        if state is None and len(self._states) < self.max_plate_tracks:
            state = _VehiclePlateState(vehicle_track_id=vehicle_id)
            self._states[vehicle_id] = state
        return state

    @staticmethod
    def describe() -> dict[str, Any]:
        return {
            "schema": "media_format.ai.processor.v2",
            "algorithm": "privacy_redaction",
            "task": "dashcam_privacy_redaction",
            "media": ["video"],
            "input": {"pixel_format": "rgb24"},
            "outputs": ["media_format.ai.frame_result.v1"],
            "can_modify_media": True,
            "media_mode": "transform",
            "state_scope": "per_stream",
            "threading": "serialized",
            "privacy": {
                "raw_biometric": False,
                "plate_text_optional": True,
                "plate_crop_persistence_optional": True,
            },
            "params": {
                "targets": ["face", "license_plate"],
                "plate_require_vehicle": True,
                "vehicle_require_track_id": True,
                "max_plate_candidates_per_vehicle": 1,
                "plate_candidate_confirm_hits": 2,
                "plate_fallback_confirm_hits": 3,
                "preset": "auto",
                "model_auto_discovery": True,
                "automatic_detection_required": False,
                "plate_mask_policy": "confirmed_detected",
                "plate_ocr_enabled": True,
                "emit_plate_candidates": False,
                "face_confirm_hits": 2,
                "person_head_min_hits": 2,
            },
            "implementation": "v5_automatic_preset_vehicle_ocr_privacy_transform",
            "implementation_version": PRIVACY_REDACTION_VERSION,
        }

    @staticmethod
    def _backend_status(detector: Any, default_backend: str) -> dict[str, Any]:
        if detector is None:
            return {"ready": False, "backend": default_backend, "reason": "not_configured"}
        status = getattr(detector, "readiness", None)
        if status is None:
            return {"ready": True, "backend": default_backend, "reason": "injected"}
        return {
            "ready": bool(getattr(status, "ready", False)),
            "backend": str(getattr(status, "backend", default_backend)),
            "reason": str(getattr(status, "reason", "")),
        }

    def automatic_readiness(self) -> dict[str, Any]:
        """Return truth-bearing automatic detection readiness.

        This deliberately distinguishes the neural/model-backed automatic path
        from the classical fallback.  A fallback can keep best-effort operation
        alive, but it cannot make ``automatic_detection_ready`` true.
        """

        face_detector = self._face_processor.face_detector if self._face_processor is not None else None
        person_detector = self._face_processor.person_detector if self._face_processor is not None else None
        face = self._backend_status(face_detector, "face")
        person = self._backend_status(person_detector, "person")
        plate = self._backend_status(self._external_plate_detector, "license_plate")
        ocr_ready = bool(self.plate_ocr_enabled and getattr(self._ocr, "ready", False))
        dependency_expected = bool(self._resolved_privacy.get("object_track_expected", True))
        object_track_ready = self._object_track_dependency_valid
        missing: list[str] = []
        if "face" in self.targets and not face["ready"]:
            missing.append("face_model")
        if "face" in self.targets and self._face_processor is not None and self._face_processor.person_head_fallback:
            if not (person["ready"] or object_track_ready):
                missing.append("person_context")
        if "license_plate" in self.targets and not plate["ready"]:
            missing.append("plate_model")
        if "license_plate" in self.targets and self.plate_require_vehicle and not object_track_ready:
            missing.append("object_track")
        if (
            "license_plate" in self.targets
            and self.plate_mask_policy == "recognized_only"
            and not ocr_ready
        ):
            missing.append("plate_ocr")
        return {
            "ready": not missing,
            "required": self.automatic_detection_required,
            "requested_preset": self.requested_preset,
            "selected_preset": self.preset,
            "config_digest": self.config_digest,
            "missing": list(dict.fromkeys(missing)),
            "face": face,
            "person": person,
            "plate": plate,
            "plate_ocr_ready": ocr_ready,
            "object_track_expected": dependency_expected,
            "object_track_ready": object_track_ready,
            "fallback_mode": self.plate_fallback_mode,
            "mask_policy": self.plate_mask_policy,
        }

    def set_dependency_readiness(self, name: str, ready: bool, reason: str = "") -> None:
        """Receive host/graph dependency readiness before the first frame.

        CoreAIGraph owns required-node construction.  Passing its result here
        prevents a configured-but-failed ``object_track`` node from being
        reported as automatic-ready merely because the registry declares it.
        """

        if str(name).strip() != "object_track":
            return
        self._object_track_dependency_valid = bool(ready)
        self._object_tracker_error = "" if ready else str(reason or "object_track_unavailable")

    def probe_automatic_backends(self) -> dict[str, Any]:
        """Load and smoke the configured detector backends before processing.

        This is intended for deployment checks and ``main.py --privacy-require-automatic``.
        It uses blank frames only; it validates model loading/inference contracts,
        not detection accuracy.
        """

        if self._face_processor is not None:
            face_detector = self._face_processor.face_detector
            face_status = self._backend_status(face_detector, "face")
            if face_status["ready"] and face_status["reason"] == "lazy":
                try:
                    face_detector.detect(np.zeros((320, 320, 3), dtype=np.uint8))
                except Exception:
                    pass
            person_detector = self._face_processor.person_detector
            person_status = self._backend_status(person_detector, "person")
            if person_status["ready"] and person_status["reason"] == "lazy":
                try:
                    person_detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))
                except Exception:
                    pass
        plate_status = self._backend_status(self._external_plate_detector, "license_plate")
        if plate_status["ready"] and plate_status["reason"] == "lazy":
            try:
                self._external_plate_detector.detect(np.zeros((512, 512, 3), dtype=np.uint8))
            except Exception:
                pass
        self._automatic_backends_probed = True
        return self.automatic_readiness()

    def get_effective_config(self) -> dict[str, Any]:
        """Return the resolved preset/config/model report for CLI and hosts."""

        report = dict(self._resolved_privacy)
        report["runtime_readiness"] = self.automatic_readiness()
        return report

    @staticmethod
    def _vehicle_id(item: Mapping[str, Any]) -> int | None:
        track = item.get("track")
        if isinstance(track, Mapping):
            try:
                value = int(track.get("id"))
                if value >= 0:
                    return value
            except Exception:
                pass
        attrs = item.get("attributes")
        if isinstance(attrs, Mapping):
            for key in ("track_id", "vehicle_track_id", "object_track_id"):
                try:
                    if key in attrs:
                        value = int(attrs[key])
                        if value >= 0:
                            return value
                except Exception:
                    pass
        return None

    def _vehicles_from_meta(self, meta: Mapping[str, Any], width: int, height: int) -> list[VehicleRegion]:
        vehicles: list[VehicleRegion] = []
        seen: set[int] = set()
        fallback_id = 1_000_000
        for item in _iter_upstream_events(meta):
            event_kind = _norm_label(item.get("kind", ""))
            if event_kind and event_kind not in {"track", "box", "detection"}:
                continue
            attrs = item.get("attributes") if isinstance(item.get("attributes"), Mapping) else {}
            label = _norm_label(
                item.get("label")
                or item.get("class")
                or item.get("class_name")
                or attrs.get("scene_kind")
            )
            if not _is_vehicle_label(label):
                continue
            confidence = float(item.get("confidence", 0.0) or 0.0)
            if confidence < self.vehicle_min_confidence:
                continue
            track_info = item.get("track") if isinstance(item.get("track"), Mapping) else {}
            track_state = _norm_label(track_info.get("state", attrs.get("track_state", "")))
            if track_state in {"lost", "removed", "inactive", "dead"}:
                continue
            try:
                missed = int(track_info.get("missed", attrs.get("missed", 0)) or 0)
            except Exception:
                missed = 0
            if track_state == "predicted" and missed > 2:
                continue
            box = _event_box(item, width, height)
            if box is None:
                continue
            _x, _y, box_w, box_h = box
            if box_w < self.vehicle_min_width_px or box_h < self.vehicle_min_height_px:
                continue
            aspect = box_w / max(box_h, 1e-6)
            if aspect < 0.20 or aspect > 5.50:
                continue
            if box_w * box_h > width * height * 0.88:
                continue
            vehicle_id = self._vehicle_id(item)
            if vehicle_id is None:
                if self.vehicle_require_track_id:
                    continue
                vehicle_id = fallback_id
                fallback_id += 1
            if vehicle_id in seen:
                continue
            seen.add(vehicle_id)
            vehicles.append(
                VehicleRegion(
                    track_id=vehicle_id,
                    xywh=box,
                    confidence=confidence,
                    label=label or "vehicle",
                    attributes={
                        "source": "object_track",
                        "track_state": track_state or "unknown",
                        "track_missed": missed,
                        **dict(attrs),
                    },
                )
            )
        return vehicles

    @staticmethod
    def _candidate_location_score(candidate: PlateCandidate, vehicle: VehicleRegion) -> float:
        px, py, pw, ph = candidate.xywh
        vx, vy, vw, vh = vehicle.xywh
        if vw <= 1 or vh <= 1:
            return 0.0
        cx = (px + 0.5 * pw - vx) / vw
        cy = (py + 0.5 * ph - vy) / vh
        return math.exp(-((cx - 0.50) / 0.38) ** 2 - 0.5 * ((cy - 0.68) / 0.33) ** 2)

    @staticmethod
    def _relative_center_distance(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> float:
        ax, ay, aw, ah = first
        bx, by, bw, bh = second
        acx, acy = ax + 0.5 * aw, ay + 0.5 * ah
        bcx, bcy = bx + 0.5 * bw, by + 0.5 * bh
        distance = math.hypot(acx - bcx, acy - bcy)
        scale = max(math.hypot(aw, ah), math.hypot(bw, bh), 1e-3)
        return distance / scale

    def _candidate_geometry_ok(
        self,
        candidate: PlateCandidate,
        vehicle: VehicleRegion,
        width: int,
        height: int,
    ) -> PlateCandidate | None:
        clipped = clip_bbox_xywh(candidate.xywh, width, height, min_size=2.0)
        if clipped is None:
            return None
        px, py, pw, ph = clipped
        vx, vy, vw, vh = vehicle.xywh
        if vw <= 1.0 or vh <= 1.0:
            return None
        if pw < self.plate_candidate_min_width_px or ph < self.plate_candidate_min_height_px:
            return None
        aspect = pw / max(ph, 1e-6)
        if not (self.plate_candidate_aspect_min <= aspect <= self.plate_candidate_aspect_max):
            return None
        rx = (px - vx) / vw
        ry = (py - vy) / vh
        rw = pw / vw
        rh = ph / vh
        center_x = rx + 0.5 * rw
        center_y = ry + 0.5 * rh
        if not (self.plate_candidate_relative_width_min <= rw <= self.plate_candidate_relative_width_max):
            return None
        if not (self.plate_candidate_relative_height_min <= rh <= self.plate_candidate_relative_height_max):
            return None
        if not (0.08 <= center_x <= 0.92):
            return None
        if not (self.plate_candidate_relative_y_min <= center_y <= self.plate_candidate_relative_y_max):
            return None
        # Require most of the candidate to lie inside its associated vehicle.
        ix0, iy0 = max(px, vx), max(py, vy)
        ix1, iy1 = min(px + pw, vx + vw), min(py + ph, vy + vh)
        intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        containment = intersection / max(pw * ph, 1e-6)
        if containment < 0.88:
            return None
        try:
            detector_confidence = float(candidate.detector_confidence)
        except Exception:
            detector_confidence = 0.0
        try:
            candidate_score = float(candidate.score)
        except Exception:
            candidate_score = 0.0
        model_backed = bool(candidate.attributes.get("model_backed", False))
        if model_backed:
            if detector_confidence < 0.20:
                return None
        elif candidate_score < self.plate_candidate_min_score:
            return None
        attributes = {
            **dict(candidate.attributes),
            "relative_box": [rx, ry, rw, rh],
            "relative_center": [center_x, center_y],
            "aspect": aspect,
            "vehicle_containment": containment,
            "model_backed": model_backed,
        }
        return replace(
            candidate,
            xywh=clipped,
            score=candidate_score,
            source=str(candidate.source),
            detector_confidence=detector_confidence,
            attributes=attributes,
        )

    def _update_proposal_evidence(
        self,
        state: _VehiclePlateState,
        vehicle: VehicleRegion,
        candidates: Sequence[PlateCandidate],
        *,
        frame_index: int,
    ) -> list[PlateCandidate]:
        for evidence in state.proposal_evidence:
            evidence.misses += 1
        unmatched = set(range(len(state.proposal_evidence)))
        current: list[PlateCandidate] = []
        for candidate in candidates:
            relative = _relative_box(candidate.xywh, vehicle.xywh)
            if relative is None:
                continue
            best_index: int | None = None
            best_score = -1.0
            for index in list(unmatched):
                evidence = state.proposal_evidence[index]
                overlap = bbox_iou_xywh(relative, evidence.relative_box)
                center = self._relative_center_distance(relative, evidence.relative_box)
                if overlap < self.plate_candidate_match_iou and center > self.plate_candidate_center_gate:
                    continue
                score = 2.0 * overlap + max(0.0, 1.0 - center)
                if score > best_score:
                    best_index, best_score = index, score
            if best_index is None:
                evidence = _PlateProposalEvidence(
                    relative_box=relative,
                    candidate=candidate,
                    first_frame=frame_index,
                    last_frame=frame_index,
                    score_ema=float(candidate.score),
                    confidence_ema=float(candidate.detector_confidence),
                )
                state.proposal_evidence.append(evidence)
            else:
                evidence = state.proposal_evidence[best_index]
                unmatched.discard(best_index)
                evidence.update(candidate, relative, frame_index=frame_index)
            model_backed = bool(candidate.attributes.get("model_backed", False))
            required_hits = (
                self.plate_candidate_confirm_hits
                if model_backed
                else self.plate_fallback_confirm_hits
            )
            if evidence.hits >= required_hits:
                evidence.confirmed = True
            elif model_backed and candidate.detector_confidence >= self.plate_candidate_high_confidence:
                evidence.confirmed = True
            candidate.attributes.update({
                "proposal_hits": evidence.hits,
                "proposal_misses": evidence.misses,
                "proposal_confirmed": evidence.confirmed,
            })
            current.append(candidate)

        state.proposal_evidence = [
            evidence
            for evidence in state.proposal_evidence
            if evidence.misses <= self.plate_candidate_max_missed_scans
        ]
        state.proposal_evidence.sort(
            key=lambda evidence: (
                evidence.confirmed,
                evidence.hits,
                evidence.score_ema,
                evidence.confidence_ema,
            ),
            reverse=True,
        )
        if len(state.proposal_evidence) > self.max_plate_candidates_per_vehicle:
            state.proposal_evidence = state.proposal_evidence[: self.max_plate_candidates_per_vehicle]
        return current

    def _confirmed_candidate_detections(
        self,
        vehicles: Sequence[VehicleRegion],
        width: int,
        height: int,
        frame_index: int,
    ) -> list[PrivacyDetection]:
        vehicle_map = {int(vehicle.track_id): vehicle for vehicle in vehicles}
        output: list[PrivacyDetection] = []
        for vehicle_id, state in self._states.items():
            if state.confirmed_text:
                continue
            vehicle = vehicle_map.get(vehicle_id)
            if vehicle is None:
                continue
            for evidence in state.proposal_evidence[: self.max_plate_candidates_per_vehicle]:
                if not evidence.confirmed or evidence.misses > self.plate_candidate_max_missed_scans:
                    continue
                box = _project_relative(evidence.relative_box, vehicle.xywh, width, height)
                if box is None:
                    continue
                output.append(
                    PrivacyDetection(
                        kind="license_plate",
                        xywh=box,
                        confidence=float(max(0.30, evidence.confidence_ema)),
                        source=evidence.candidate.source,
                        attributes={
                            "recognized": False,
                            "vehicle_track_id": vehicle_id,
                            "candidate_score": evidence.score_ema,
                            "candidate_hits": evidence.hits,
                            "candidate_confirmed": True,
                            "candidate_last_frame": evidence.last_frame,
                        },
                    )
                )
        return output

    def _external_candidates(self, frame_rgb: np.ndarray, vehicles: Sequence[VehicleRegion]) -> list[PlateCandidate]:
        detector = self._external_plate_detector
        if detector is None:
            return []
        output: list[PlateCandidate] = []
        for vehicle in vehicles:
            try:
                try:
                    values = detector.detect(frame_rgb, rois=[vehicle.xywh])
                except TypeError:
                    values = detector.detect(frame_rgb)
            except Exception as exc:
                self._ocr_errors.append(f"external_plate_detector_error:{type(exc).__name__}:{exc}")
                continue
            for value in values or []:
                if isinstance(value, PrivacyDetection):
                    box = value.xywh
                    confidence = value.confidence
                    source = value.source
                    attributes = dict(value.attributes)
                elif isinstance(value, Mapping):
                    raw_box = value.get("xywh", value.get("bbox"))
                    if isinstance(raw_box, Mapping):
                        raw_box = [raw_box.get(key, 0.0) for key in ("x", "y", "w", "h")]
                    try:
                        box = tuple(float(item) for item in raw_box[:4])
                    except Exception:
                        continue
                    confidence = float(value.get("confidence", 0.0))
                    source = str(value.get("source", "external_plate_detector"))
                    attributes = dict(value.get("attributes", {}))
                else:
                    continue
                vx, vy, vw, vh = vehicle.xywh
                px, py, pw, ph = box
                center_x, center_y = px + 0.5 * pw, py + 0.5 * ph
                if not (vx <= center_x <= vx + vw and vy <= center_y <= vy + vh):
                    continue
                candidate = PlateCandidate(
                    vehicle_track_id=vehicle.track_id,
                    xywh=box,
                    score=1.25 + 4.0 * confidence,
                    detector_confidence=confidence,
                    source=source,
                    attributes={
                        **attributes,
                        "vehicle_label": vehicle.label,
                        "model_backed": True,
                    },
                )
                candidate.score += self._candidate_location_score(candidate, vehicle)
                filtered = self._candidate_geometry_ok(
                    candidate, vehicle, frame_rgb.shape[1], frame_rgb.shape[0]
                )
                if filtered is not None:
                    output.append(filtered)
        return output

    def _select_candidates(self, candidates: Sequence[PlateCandidate]) -> list[PlateCandidate]:
        grouped: dict[int, list[PlateCandidate]] = defaultdict(list)
        for candidate in candidates:
            grouped[int(candidate.vehicle_track_id)].append(candidate)
        output: list[PlateCandidate] = []
        for rows in grouped.values():
            rows.sort(
                key=lambda item: (
                    bool(item.attributes.get("model_backed", False)),
                    item.score,
                    item.detector_confidence,
                ),
                reverse=True,
            )
            kept: list[PlateCandidate] = []
            for row in rows:
                if any(bbox_iou_xywh(row.xywh, old.xywh) >= 0.40 for old in kept):
                    continue
                kept.append(row)
                if len(kept) >= self.max_plate_candidates_per_vehicle:
                    break
            output.extend(kept)
        return output

    def _recognize_list(
        self,
        jobs: Sequence[tuple[np.ndarray, PlateCandidate, PlateCandidate]],
    ) -> tuple[PlateCandidate | None, PlateOCRResult]:
        best_candidate: PlateCandidate | None = None
        best_result = PlateOCRResult(reason="no_candidate")
        for crop_rgb, local_candidate, candidate in jobs:
            result = self._ocr.recognize(crop_rgb, local_candidate)
            if result.recognizable and result.confidence > best_result.confidence:
                best_candidate, best_result = candidate, result
                if result.confidence >= self.plate_ocr_high_confidence:
                    break
            elif best_candidate is None and result.confidence > best_result.confidence:
                best_candidate, best_result = candidate, result
        return best_candidate, best_result

    def _apply_ocr_result(
        self,
        *,
        vehicle_id: int,
        vehicle_box: tuple[float, float, float, float],
        candidate: PlateCandidate | None,
        result: PlateOCRResult,
        frame_index: int,
        pts: float,
        stream_id: str,
    ) -> bool:
        state = self._state(vehicle_id)
        if state is None:
            return False
        state.last_ocr_reason = result.reason
        state.alternatives = list(result.alternatives[:12])
        if candidate is None:
            return False
        state.last_candidate_box = candidate.xywh
        state.last_candidate_score = candidate.score
        state.last_vehicle_box = vehicle_box
        candidate_relative = _relative_box(candidate.xywh, vehicle_box)
        state.relative_box = candidate_relative
        if candidate_relative is not None:
            if state.ocr_relative_box is not None and bbox_iou_xywh(candidate_relative, state.ocr_relative_box) < self.plate_candidate_match_iou:
                # Do not combine OCR votes from two unrelated rectangles on the
                # same vehicle (for example, a logo and the actual plate).
                state.votes.clear()
            state.ocr_relative_box = candidate_relative
        text, confidence, hits = state.vote(result)
        if not text:
            return False
        if confidence >= self.plate_ocr_high_confidence or hits >= self.plate_confirm_hits:
            newly_confirmed = not state.confirmed_text
            state.confirmed_text = text
            state.confirmed_confidence = confidence
            state.confirmed_frame = frame_index
            if candidate_relative is not None:
                for evidence in state.proposal_evidence:
                    if bbox_iou_xywh(candidate_relative, evidence.relative_box) >= self.plate_candidate_match_iou:
                        evidence.confirmed = True
                        evidence.hits = max(evidence.hits, self.plate_candidate_confirm_hits)
                        break
            if self.save_recognized_plates and result.rectified_bgr is not None:
                if state.saved_text != text:
                    try:
                        image_path, metadata_path = save_recognized_plate(
                            result,
                            output_dir=self.plate_output_dir,
                            stream_id=stream_id,
                            vehicle_track_id=vehicle_id,
                            frame_index=frame_index,
                            time_seconds=pts,
                            candidate=candidate,
                        )
                        state.crop_path = image_path
                        state.metadata_path = metadata_path
                        if image_path:
                            state.saved_text = text
                    except Exception as exc:
                        self._ocr_errors.append(f"plate_save_error:{type(exc).__name__}:{exc}")
            return newly_confirmed
        return False

    def _poll_pending(self) -> int:
        self._retired_ocr = [future for future in self._retired_ocr if not future.done()]
        completed = 0
        for vehicle_id, pending in list(self._pending.items()):
            if not pending.future.done():
                continue
            del self._pending[vehicle_id]
            completed += 1
            try:
                candidate, result = pending.future.result()
            except Exception as exc:
                state = self._states.get(vehicle_id)
                error = f"ocr_worker_error:{type(exc).__name__}:{exc}"
                if state is not None:
                    state.last_ocr_reason = error
                self._ocr_errors.append(error)
                continue
            if pending.stream_id != self._last_stream_id or vehicle_id not in self._states:
                continue
            self._apply_ocr_result(
                vehicle_id=vehicle_id,
                vehicle_box=pending.vehicle_box,
                candidate=candidate,
                result=result,
                frame_index=pending.frame_index,
                pts=pending.pts,
                stream_id=pending.stream_id,
            )
        return completed

    def _submit_ocr(
        self,
        *,
        frame_rgb: np.ndarray,
        vehicle: VehicleRegion,
        candidates: Sequence[PlateCandidate],
        frame_index: int,
        pts: float,
        stream_id: str,
    ) -> bool:
        if not self.plate_ocr_enabled or not self._ocr.ready or not candidates:
            return False
        if self._executor is not None and len(self._pending) + len(self._retired_ocr) >= self.max_pending_ocr:
            return False
        vehicle_id = int(vehicle.track_id)
        state = self._state(vehicle_id)
        if state is None:
            return False
        if state.confirmed_text:
            return False
        if vehicle_id in self._pending:
            return False
        if state.ocr_attempts >= self.plate_ocr_max_attempts_per_vehicle:
            return False
        if frame_index - state.last_ocr_frame < self.plate_ocr_retry_interval:
            return False
        quality = max(_candidate_quality(frame_rgb, item.xywh) for item in candidates)
        if state.best_quality > 0 and quality < state.best_quality * (1.0 + self.plate_ocr_quality_improvement):
            return False
        state.best_quality = max(state.best_quality, quality)
        state.last_ocr_frame = frame_index
        state.ocr_attempts += 1
        # Retain only padded candidate ROIs while OCR runs off-thread. A full
        # resolution frame per queued vehicle would multiply retained memory.
        height, width = frame_rgb.shape[:2]
        jobs = []
        for candidate in candidates:
            x, y, w, h = candidate.xywh
            px, py = max(5, math.ceil(w * 0.45)), max(4, math.ceil(h * 0.65))
            x0, y0 = max(0, math.floor(x) - px), max(0, math.floor(y) - py)
            x1, y1 = min(width, math.ceil(x + w) + px), min(height, math.ceil(y + h) + py)
            crop = frame_rgb[y0:y1, x0:x1].copy()
            if crop.size:
                local = replace(candidate, xywh=(x - x0, y - y0, w, h))
                jobs.append((crop, local, candidate))
        if self._executor is None:
            try:
                candidate, result = self._recognize_list(jobs)
            except Exception as exc:
                error = f"ocr_worker_error:{type(exc).__name__}:{exc}"
                self._ocr_errors.append(error)
                state.last_ocr_reason = error
                return False
            self._apply_ocr_result(
                vehicle_id=vehicle_id,
                vehicle_box=vehicle.xywh,
                candidate=candidate,
                result=result,
                frame_index=frame_index,
                pts=pts,
                stream_id=stream_id,
            )
        else:
            future = self._executor.submit(self._recognize_list, jobs)
            self._pending[vehicle_id] = _PendingOCR(
                future=future,
                vehicle_track_id=vehicle_id,
                frame_index=frame_index,
                pts=pts,
                stream_id=stream_id,
                vehicle_box=vehicle.xywh,
            )
        return True

    def _plate_detections_from_confirmed(
        self,
        vehicles: Sequence[VehicleRegion],
        width: int,
        height: int,
        frame_index: int,
    ) -> list[PrivacyDetection]:
        vehicle_map = {int(vehicle.track_id): vehicle for vehicle in vehicles}
        detections: list[PrivacyDetection] = []
        for vehicle_id, state in list(self._states.items()):
            vehicle = vehicle_map.get(vehicle_id)
            if vehicle is not None:
                state.last_seen_frame = frame_index
                state.last_vehicle_box = vehicle.xywh
            if not state.confirmed_text or vehicle is None:
                continue
            box = None
            if vehicle is not None and state.relative_box is not None:
                box = _project_relative(state.relative_box, vehicle.xywh, width, height)
            if box is None:
                continue
            detections.append(
                PrivacyDetection(
                    kind="license_plate",
                    xywh=box,
                    confidence=float(max(0.55, state.confirmed_confidence)),
                    source="vehicle_ocr_confirmed",
                    attributes={
                        "recognized": True,
                        "plate_text": state.confirmed_text,
                        "ocr_confidence": state.confirmed_confidence,
                        "vehicle_track_id": vehicle_id,
                        "plate_crop_path": state.crop_path,
                        "ocr_attempts": state.ocr_attempts,
                    },
                )
            )
        return detections

    def _block_output(self, frame_rgb: np.ndarray) -> np.ndarray:
        output = frame_rgb.copy()
        height, width = output.shape[:2]
        apply_privacy_mask(output, (0, 0, width, height), mode=self.block_mode, pixel_block=max(16, self.pixel_block))
        return output

    def process(self, frame_rgb: np.ndarray, meta_or_pts: Any = None) -> dict[str, Any]:
        started = time.perf_counter()
        pts, frame_index, meta = process_meta(meta_or_pts)
        if self._closed:
            return _result("privacy_redaction", pts, frame_index, [], valid=False, reason="processor_closed")
        if frame_rgb is None or not isinstance(frame_rgb, np.ndarray) or frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
            return _result("privacy_redaction", pts, frame_index, [], valid=False, reason="missing_frame")
        height, width = frame_rgb.shape[:2]
        self._ocr_errors.clear()
        stream_id = str(meta.get("stream_id", "default"))
        context = (str(meta.get("segment_id", "0")), int(meta.get("discontinuity_seq", 0)), float(pts), width, height)
        discontinuity = self._last_context is not None and (
            context[:2] != self._last_context[:2]
            or context[2] < self._last_context[2]
            or context[3:] != self._last_context[3:]
        )
        if (self._last_stream_id and stream_id != self._last_stream_id) or discontinuity:
            self.reset({"stream_id": stream_id}, "discontinuity")
        self._last_stream_id = stream_id
        self._last_context = context
        meta = self._object_metadata(frame_rgb, meta)
        if self.automatic_detection_required and not self._automatic_backends_probed:
            self.probe_automatic_backends()
        automatic_status = self.automatic_readiness()
        ocr_completed = self._poll_pending()

        face_events: list[dict[str, Any]] = []
        face_metrics: dict[str, Any] = {}
        output_frame = None
        face_degraded = False
        face_reason = ""
        if self._face_processor is not None:
            face_result = self._face_processor.process(frame_rgb, meta)
            output_frame = np.asarray(face_result.get("frame", frame_rgb))
            face_events = list(face_result.get("events", []))
            face_metrics = dict(face_result.get("metrics", {}))
            face_degraded = bool(face_result.get("degraded", False))
            face_reason = str(face_result.get("reason", ""))
            if any(str(item.get("label", "")) == "privacy_frame_blocked" for item in face_events):
                return frame_result(
                    algorithm="privacy_redaction",
                    time=pts,
                    frame_index=frame_index,
                    processed=True,
                    valid=True,
                    degraded=True,
                    reason=face_reason or "privacy_fail_closed:face",
                    events=face_events,
                    metrics={
                        "face": face_metrics,
                        "plate": {"skipped": "frame_already_blocked"},
                        "configuration": {
                            "requested_preset": self.requested_preset,
                            "selected_preset": self.preset,
                            "config_digest": self.config_digest,
                        },
                        "automatic_detection": automatic_status,
                    },
                    extra={"frame": output_frame},
                )
        if output_frame is None:
            output_frame = frame_rgb.copy()

        vehicles = self._vehicles_from_meta(meta, width, height)
        vehicle_map = {int(vehicle.track_id): vehicle for vehicle in vehicles}
        stale_after = max(15, self.plate_ocr_retry_interval * 2)
        reassignment_gap = max(6, self.plate_detect_every * 2)
        for vehicle_id, state in list(self._states.items()):
            current_vehicle = vehicle_map.get(vehicle_id)
            if current_vehicle is not None:
                gap = frame_index - state.last_seen_frame if state.last_seen_frame >= 0 else 0
                if (
                    gap > reassignment_gap
                    and state.last_vehicle_box is not None
                    and bbox_iou_xywh(state.last_vehicle_box, current_vehicle.xywh) < 0.02
                ):
                    pending = self._pending.pop(vehicle_id, None)
                    if pending is not None and not pending.future.cancel() and not pending.future.done():
                        self._retired_ocr.append(pending.future)
                    del self._states[vehicle_id]
                    continue
                state.last_seen_frame = frame_index
            elif frame_index - state.last_seen_frame > stale_after and vehicle_id not in self._pending:
                del self._states[vehicle_id]
        plate_due = frame_index % self.plate_detect_every == 0
        candidates: list[PlateCandidate] = []
        raw_candidate_count = 0
        geometry_rejected = 0
        ocr_submitted = 0
        plate_started = time.perf_counter()
        plate_backend_status = self._backend_status(self._external_plate_detector, "license_plate")
        fallback_used = False
        if "license_plate" in self.targets and plate_due and vehicles:
            # Once a vehicle has an OCR-confirmed plate, its relative plate box
            # follows the object track and expensive proposal/OCR work stops.
            # Vehicles that exhausted their configured OCR attempts are also
            # skipped until they leave and re-enter as a new track.
            scan_vehicles = []
            for vehicle in vehicles:
                state = self._states.get(int(vehicle.track_id))
                if state is not None and state.confirmed_text:
                    continue
                if state is None and len(self._states) >= self.max_plate_tracks:
                    continue
                if self.plate_mask_policy == "recognized_only" and state is not None and state.ocr_attempts >= self.plate_ocr_max_attempts_per_vehicle:
                    continue
                scan_vehicles.append(vehicle)
            plate_backend_status = self._backend_status(self._external_plate_detector, "license_plate")
            errors_before_model = len(self._ocr_errors)
            candidates.extend(self._external_candidates(frame_rgb, scan_vehicles))
            model_runtime_failed = len(self._ocr_errors) > errors_before_model
            use_fallback = (
                self.plate_fallback_mode == "augment"
                or (
                    self.plate_fallback_mode == "when_model_unavailable"
                    and (not plate_backend_status["ready"] or model_runtime_failed)
                )
            )
            fallback_used = False
            # The classical fallback remains vehicle-restricted and temporally
            # gated.  It is not mixed with a healthy neural detector unless the
            # diagnostic ``augment`` preset explicitly requests that behavior.
            if use_fallback and self._fallback_plate_detector is not None:
                fallback_used = True
                try:
                    candidates.extend(self._fallback_plate_detector.detect(frame_rgb, scan_vehicles))
                except Exception as exc:
                    self._ocr_errors.append(f"plate_fallback_error:{type(exc).__name__}:{exc}")
            raw_candidate_count = len(candidates)
            vehicle_map = {int(vehicle.track_id): vehicle for vehicle in vehicles}
            filtered_candidates: list[PlateCandidate] = []
            for candidate in candidates:
                vehicle = vehicle_map.get(int(candidate.vehicle_track_id))
                if vehicle is None:
                    continue
                filtered = self._candidate_geometry_ok(candidate, vehicle, width, height)
                if filtered is not None:
                    filtered_candidates.append(filtered)
            geometry_rejected = max(0, raw_candidate_count - len(filtered_candidates))
            candidates = self._select_candidates(filtered_candidates)
            by_vehicle: dict[int, list[PlateCandidate]] = defaultdict(list)
            for candidate in candidates:
                by_vehicle[int(candidate.vehicle_track_id)].append(candidate)

            for vehicle in scan_vehicles:
                vehicle_id = int(vehicle.track_id)
                rows = by_vehicle.get(vehicle_id, [])
                state = self._states.get(vehicle_id)
                if state is None and not rows:
                    continue
                state = self._state(vehicle_id)
                if state is None:
                    continue
                state.last_seen_frame = frame_index
                state.last_vehicle_box = vehicle.xywh
                rows = self._update_proposal_evidence(
                    state, vehicle, rows, frame_index=frame_index
                )
                state.selected_candidates = list(rows)
                state.candidate_vehicle_box = vehicle.xywh
                if rows:
                    state.last_candidate_frame = frame_index
                    state.last_candidate_box = rows[0].xywh
                    state.last_candidate_score = rows[0].score
                    ocr_submitted += int(
                        self._submit_ocr(
                            frame_rgb=frame_rgb,
                            vehicle=vehicle,
                            candidates=rows,
                            frame_index=frame_index,
                            pts=pts,
                            stream_id=stream_id,
                        )
                    )
        plate_detector_ms = (time.perf_counter() - plate_started) * 1000.0

        capacity_limited = len(vehicles) > self.max_plate_tracks
        if self.plate_require_vehicle and not vehicles:
            candidate_reason = "no_vehicle_tracks"
        elif not plate_due:
            candidate_reason = "plate_scan_not_due"
        elif not candidates:
            candidate_reason = "no_plate_candidates"
        else:
            candidate_reason = ""

        recognized_detections = self._plate_detections_from_confirmed(vehicles, width, height, frame_index)
        if self.plate_mask_policy == "confirmed_detected":
            # ``all_detected`` now means all *temporally confirmed* selected
            # candidates.  A single bright rectangle is never masked solely
            # because it ranked first on one frame.
            recognized_detections.extend(
                self._confirmed_candidate_detections(vehicles, width, height, frame_index)
            )
        candidate_events: list[dict[str, Any]] = []
        if self.emit_plate_candidates:
            for candidate in candidates:
                cx, cy, cw, ch = candidate.xywh
                candidate_events.append(
                    event(
                        kind="box",
                        label="license_plate_candidate",
                        confidence=float(candidate.detector_confidence),
                        time=pts,
                        bbox=bbox(cx, cy, cw, ch),
                        attributes={
                            "privacy_target": "license_plate",
                            "recognized": False,
                            "vehicle_track_id": int(candidate.vehicle_track_id),
                            "candidate_score": float(candidate.score),
                            "candidate_source": candidate.source,
                            "proposal_hits": int(candidate.attributes.get("proposal_hits", 1)),
                            "proposal_confirmed": bool(candidate.attributes.get("proposal_confirmed", False)),
                            "model_backed": bool(candidate.attributes.get("model_backed", False)),
                        },
                    )
                )

        plate_tracks = self._plate_tracker.update(
            recognized_detections,
            frame_index=frame_index,
            width=width,
            height=height,
        )
        plate_tracks = [track for track in plate_tracks if track.missed <= self.max_predicted_plate_age]
        # Old alternative boxes must not accumulate as extra masks while a
        # vehicle's selected candidates move between detector frames.
        by_vehicle_count: dict[int, int] = defaultdict(int)
        limited_tracks = []
        for track in sorted(plate_tracks, key=lambda item: (item.missed, -item.hits, -item.confidence)):
            vehicle_id = int(track.attributes.get("vehicle_track_id", -1))
            if by_vehicle_count[vehicle_id] < self.max_plate_candidates_per_vehicle:
                by_vehicle_count[vehicle_id] += 1
                limited_tracks.append(track)
        plate_tracks = limited_tracks

        plate_events: list[dict[str, Any]] = []
        plate_text_events: list[dict[str, Any]] = []
        plate_region_count = 0
        highlight = self.mode == "highlight"
        for track in plate_tracks[:max(0, self.max_masks - len(face_events))]:
            recognized = bool(track.attributes.get("recognized", False))
            if self.plate_mask_policy == "recognized_only" and not recognized:
                continue
            rect = expand_privacy_box(
                track.xywh,
                kind="license_plate",
                width=width,
                height=height,
                plate_expand_w=self.plate_expand_width,
                plate_expand_h=self.plate_expand_height,
            )
            if rect is None:
                continue
            x, y, w, h = rect
            if self.apply_redaction and not highlight:
                apply_privacy_mask(output_frame, rect, mode=self.mode, pixel_block=self.pixel_block)
            plate_region_count += 1
            track_attributes = dict(track.attributes)
            if not self.emit_plate_text:
                track_attributes.pop("plate_text", None)
            attrs = {
                "action": self.mode,
                "privacy_target": "license_plate",
                "source": track.source,
                "predicted": track.predicted,
                **track_attributes,
            }
            label = "license_plate" if recognized else "license_plate_candidate"
            label += "_detected" if highlight else "_redacted"
            plate_events.append(
                event(
                    kind="box" if highlight else "mask",
                    label=label,
                    confidence=track.confidence,
                    time=pts,
                    bbox=bbox(x, y, w, h),
                    track={
                        "id": int(track.track_id),
                        "age": int(track.age),
                        "missed": int(track.missed),
                        "state": "predicted" if track.predicted else "detected",
                    },
                    attributes=attrs,
                )
            )
            text_value = str(track.attributes.get("plate_text", ""))
            if recognized and text_value and self.emit_plate_text:
                plate_text_events.append(
                    event(
                        kind="text",
                        label=text_value,
                        confidence=float(track.attributes.get("ocr_confidence", track.confidence)),
                        time=pts,
                        bbox=bbox(x, y, w, h),
                        text={"value": text_value, "normalized": text_value},
                        track={"id": int(track.track_id), "state": "recognized_plate"},
                        attributes={
                            "vehicle_track_id": track.attributes.get("vehicle_track_id"),
                            "plate_crop_path": track.attributes.get("plate_crop_path", ""),
                            "privacy_sensitive": True,
                        },
                    )
                )

        plate_model_ready = bool(plate_backend_status["ready"])
        fallback_ready = bool(self._fallback_plate_detector is not None and self.plate_fallback_mode != "disabled")
        plate_operational_ready = plate_model_ready or fallback_ready
        ocr_ready = bool(self.plate_ocr_enabled and self._ocr.ready)
        automatic_status = self.automatic_readiness()
        missing_required: list[str] = []
        if "license_plate" in self.targets:
            needs_ocr = self.plate_mask_policy == "recognized_only"
            if self.require_plate_backend and (not plate_model_ready or (needs_ocr and not ocr_ready)):
                missing_required.append("license_plate")
            if self.plate_require_vehicle and self._object_tracker_error:
                missing_required.append("object_track")
            if any(error.startswith("external_plate_detector_error:") for error in self._ocr_errors):
                missing_required.append("license_plate")
            if not plate_model_ready and any(
                error.startswith("plate_fallback_error:") for error in self._ocr_errors
            ):
                missing_required.append("license_plate")
            if capacity_limited:
                missing_required.append("plate_track_capacity")
        if self.automatic_detection_required:
            missing_required.extend(str(item) for item in automatic_status["missing"])
        missing_required = list(dict.fromkeys(missing_required))
        if missing_required and (self.fail_policy == "block_frame" or self.automatic_detection_required):
            output_frame = self._block_output(frame_rgb)
            block_event = event(
                kind="mask",
                label="privacy_frame_blocked",
                confidence=1.0,
                time=pts,
                bbox=bbox(0, 0, width, height),
                attributes={"missing_targets": missing_required, "privacy_fail_closed": True},
            )
            return frame_result(
                algorithm="privacy_redaction",
                time=pts,
                frame_index=frame_index,
                processed=True,
                valid=True,
                degraded=True,
                reason="privacy_fail_closed:" + ",".join(missing_required),
                events=face_events + [block_event],
                metrics={
                    "face": face_metrics,
                    "plate": {
                        "missing_required": missing_required,
                        "model_ready": plate_model_ready,
                        "operational_ready": plate_operational_ready,
                        "fallback_ready": fallback_ready,
                    },
                    "configuration": {
                        "requested_preset": self.requested_preset,
                        "selected_preset": self.preset,
                        "config_digest": self.config_digest,
                    },
                    "automatic_detection": automatic_status,
                },
                extra={"frame": output_frame},
            )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        recognized_states = [state for state in self._states.values() if state.confirmed_text]
        plate_metrics = {
            "vehicle_tracks": len(vehicles),
            "plate_scan_due": plate_due,
            "candidate_reason": candidate_reason,
            "plate_candidates": len(candidates),
            "raw_plate_candidates": raw_candidate_count,
            "geometry_rejected_candidates": geometry_rejected,
            "confirmed_unreadable_candidates": sum(
                1
                for state in self._states.values()
                for evidence in state.proposal_evidence
                if evidence.confirmed and not state.confirmed_text
            ),
            "proposal_states": sum(len(state.proposal_evidence) for state in self._states.values()),
            "max_candidates_per_vehicle": self.max_plate_candidates_per_vehicle,
            "candidate_confirm_hits": self.plate_candidate_confirm_hits,
            "fallback_confirm_hits": self.plate_fallback_confirm_hits,
            "candidate_high_confidence": self.plate_candidate_high_confidence,
            "ocr_ready": ocr_ready,
            "ocr_async": self.plate_ocr_async,
            "ocr_submitted": ocr_submitted,
            "ocr_completed_this_frame": ocr_completed,
            "ocr_pending": len(self._pending),
            "ocr_queue_capacity": self.max_pending_ocr,
            "ocr_retired": len(self._retired_ocr),
            "plate_track_capacity": self.max_plate_tracks,
            "capacity_limited": capacity_limited,
            "backend_errors": list(self._ocr_errors),
            "object_track_error": self._object_tracker_error,
            "ocr_attempts_total": sum(state.ocr_attempts for state in self._states.values()),
            "recognized_vehicle_plates": len(recognized_states),
            "recognized_texts": [state.confirmed_text for state in recognized_states] if self.emit_plate_text else [],
            "saved_plate_crops": sum(1 for state in recognized_states if state.crop_path),
            "masked_plates": 0 if highlight else plate_region_count,
            "highlighted_plates": plate_region_count if highlight else 0,
            "mask_policy": self.plate_mask_policy,
            "plate_detector_ms": plate_detector_ms,
            "plate_model_ready": plate_model_ready,
            "plate_model_backend": plate_backend_status["backend"],
            "plate_model_reason": plate_backend_status["reason"],
            "plate_operational_ready": plate_operational_ready,
            "fallback_mode": self.plate_fallback_mode,
            "fallback_used": fallback_used,
            "fallback_vehicle_only": True,
            "vehicle_require_track_id": self.vehicle_require_track_id,
            "vehicle_min_confidence": self.vehicle_min_confidence,
        }
        reason_parts: list[str] = []
        if face_reason:
            reason_parts.append(face_reason)
        reason_parts.extend(missing_required)
        if "license_plate" in self.targets and not vehicles:
            reason_parts.append("no_vehicle_tracks")
        elif "license_plate" in self.targets and self.plate_mask_policy == "recognized_only" and not recognized_states:
            reason_parts.append("no_recognizable_license_plate")
        if not automatic_status["ready"]:
            reason_parts.append("automatic_detection_not_ready:" + ",".join(automatic_status["missing"]))
        degraded = face_degraded or bool(missing_required) or bool(self._ocr_errors) or not automatic_status["ready"]
        reason_parts.extend(self._ocr_errors)
        if "license_plate" in self.targets and self.plate_ocr_enabled and not ocr_ready:
            degraded = True
            reason_parts.append("plate_ocr_unavailable")
        return frame_result(
            algorithm="privacy_redaction",
            time=pts,
            frame_index=frame_index,
            processed=True,
            valid=True,
            degraded=degraded,
            reason=";".join(dict.fromkeys(part for part in reason_parts if part)),
            events=face_events + candidate_events + plate_events + plate_text_events,
            metrics={
                "face": face_metrics,
                "plate": plate_metrics,
                "configuration": {
                    "requested_preset": self.requested_preset,
                    "selected_preset": self.preset,
                    "config_path": self.config_path,
                    "config_digest": self.config_digest,
                    "model_auto_discovery": self.model_auto_discovery,
                },
                "automatic_detection": automatic_status,
                "latency_ms": elapsed_ms,
            },
            extra={"frame": output_frame},
        )

    def reset(self, _context: Mapping[str, Any] | str | None = None, _reason: str = "reset") -> None:
        if self._face_processor is not None:
            self._face_processor.reset(_context, _reason)
        for pending in self._pending.values():
            if not pending.future.cancel() and not pending.future.done():
                self._retired_ocr.append(pending.future)
        self._pending.clear()
        self._states.clear()
        self._plate_tracker.reset()
        self._last_stream_id = ""
        self._last_context = None
        self._object_tracker_error = ""
        self._object_track_dependency_valid = False
        if self._object_tracker is not None and self._object_executor is not None:
            self._object_executor.submit(self._object_tracker.reset, None, _reason).result()

    def flush(self, _context: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        # Bounded drain: completed OCR updates are retained for the caller's
        # final metrics, but no synthetic video frame is emitted here.
        deadline = time.monotonic() + 1.5
        while self._pending and time.monotonic() < deadline:
            self._poll_pending()
            if self._pending:
                time.sleep(0.01)
        if self._face_processor is not None:
            self._face_processor.flush(_context)
        return []

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.flush()
        if self._executor is not None:
            # Embedded hosts may finalize Python after close(). Drain the one
            # active OCR job (the built-in backend has a bounded deadline).
            self._executor.shutdown(wait=True, cancel_futures=True)
        if self._face_processor is not None:
            self._face_processor.close()
        if self._object_tracker is not None and self._object_executor is not None:
            self._object_executor.submit(self._object_tracker.close).result()
            self._object_tracker = None
        if self._object_executor is not None:
            self._object_executor.shutdown(wait=True, cancel_futures=True)
            self._object_executor = None
        self.reset()
