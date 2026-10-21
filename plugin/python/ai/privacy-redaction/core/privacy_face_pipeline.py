"""False-positive-resistant face/head privacy pipeline for dashcam video.

This helper is used by :mod:`ai_privacy_redaction`.
The public processor is
``ai_privacy_redaction.PrivacyRedactionProcessor``.

The pipeline uses exact label matching, geometry checks, confidence gates and
source-specific temporal confirmation before a region is allowed to become a
privacy mask.  High-confidence direct face detections can still be masked on
the first frame; low-confidence faces and person-derived head fallbacks require
repeat evidence.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import re
import time
from typing import Any

import cv2
import numpy as np

from _common import bbox, event, process_meta, _result
from .contracts import frame_result
from .geometry import bbox_iou_xywh, clip_bbox_xywh, nms_xywh
from .privacy_detectors import OpenCVYoloXContextDetector, OpenImageModelDetector, YuNetFaceDetector
from .privacy_redaction import apply_privacy_mask, expand_privacy_box
from .privacy_tracking import PrivacyDetection, PrivacyTrack, PrivacyTracker

_PERSON_LABELS = {"person", "pedestrian", "human"}
_VEHICLE_LABELS = {
    "car", "truck", "bus", "van", "vehicle", "motorcycle", "motorbike",
    "suv", "pickup", "automobile",
}
_FACE_LABELS = {"face", "head", "human_face", "pedestrian_face"}
_PLATE_LABELS = {"license_plate", "licence_plate", "plate", "number_plate"}
_EVENT_BOX_KINDS = {"box", "track", "detection"}


def _norm_label(value: Any) -> str:
    return re.sub(r"_+", "_", str(value or "").strip().lower().replace("-", "_").replace(" ", "_"))


def _label_tokens(value: Any) -> set[str]:
    label = _norm_label(value)
    return {token for token in label.split("_") if token}


def _is_face_label(value: Any) -> bool:
    label = _norm_label(value)
    tokens = _label_tokens(label)
    return label in _FACE_LABELS or "face" in tokens or label.endswith("_head")


def _is_person_label(value: Any) -> bool:
    label = _norm_label(value)
    tokens = _label_tokens(label)
    return label in _PERSON_LABELS or bool(tokens.intersection({"person", "pedestrian"}))


def _is_vehicle_label(value: Any) -> bool:
    label = _norm_label(value)
    tokens = _label_tokens(label)
    return label in _VEHICLE_LABELS or bool(tokens.intersection(_VEHICLE_LABELS))


def _is_plate_label(value: Any) -> bool:
    label = _norm_label(value)
    tokens = _label_tokens(label)
    return label in _PLATE_LABELS or ({"license", "plate"} <= tokens) or ({"number", "plate"} <= tokens)


def _event_box(item: Mapping[str, Any], width: int, height: int) -> tuple[float, float, float, float] | None:
    raw = item.get("bbox") or item.get("box") or item.get("rect") or item.get("xywh")
    if raw is None and all(key in item for key in ("x", "y", "w", "h")):
        raw = item
    try:
        if isinstance(raw, Mapping):
            if all(key in raw for key in ("x", "y", "w", "h")):
                x, y, w, h = (float(raw[key]) for key in ("x", "y", "w", "h"))
                normalized = bool(raw.get("normalized", False))
            elif all(key in raw for key in ("x1", "y1", "x2", "y2")):
                x1, y1, x2, y2 = (float(raw[key]) for key in ("x1", "y1", "x2", "y2"))
                x, y, w, h = x1, y1, x2 - x1, y2 - y1
                normalized = bool(raw.get("normalized", False))
            else:
                return None
        else:
            values = [float(value) for value in list(raw)[:4]]
            if len(values) != 4:
                return None
            x, y, w, h = values
            normalized = max(abs(value) for value in values) <= 1.5
        if normalized:
            x, w = x * width, w * width
            y, h = y * height, h * height
    except Exception:
        return None
    return clip_bbox_xywh((x, y, w, h), width, height, min_size=1.0)


def _iter_upstream_events(meta: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    upstream = meta.get("upstream")
    if isinstance(upstream, Mapping):
        for result in upstream.values():
            if not isinstance(result, Mapping):
                continue
            rows = result.get("events")
            if isinstance(rows, list):
                for item in rows:
                    if isinstance(item, Mapping):
                        yield item
    for key in (
        "face_events", "faces", "face_detections", "object_tracks", "tracks",
        "license_plate_events", "license_plates", "detections",
    ):
        values = meta.get(key)
        if isinstance(values, list):
            for item in values:
                if isinstance(item, Mapping):
                    yield item


def _deduplicate(detections: Sequence[PrivacyDetection], iou_threshold: float = 0.55) -> list[PrivacyDetection]:
    if not detections:
        return []
    by_kind: dict[str, list[PrivacyDetection]] = {}
    for detection in detections:
        by_kind.setdefault(detection.kind, []).append(detection)
    output: list[PrivacyDetection] = []
    for rows in by_kind.values():
        boxes = np.asarray([row.xywh for row in rows], dtype=np.float32)
        scores = np.asarray([row.confidence for row in rows], dtype=np.float32)
        for index in nms_xywh(boxes, scores, iou_threshold):
            output.append(rows[index])
    return output


def _head_from_person(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x, y, w, h = box
    head_w = w * 0.62
    head_h = min(h * 0.34, max(head_w * 1.20, h * 0.22))
    return x + (w - head_w) * 0.5, y + h * 0.015, head_w, head_h


def _face_geometry_ok(
    box: tuple[float, float, float, float],
    width: int,
    height: int,
    *,
    min_size_px: float,
    max_frame_fraction: float,
    aspect_min: float,
    aspect_max: float,
) -> bool:
    _x, _y, w, h = box
    if w < min_size_px or h < min_size_px:
        return False
    aspect = w / max(h, 1e-6)
    if not (aspect_min <= aspect <= aspect_max):
        return False
    if w * h > width * height * max_frame_fraction:
        return False
    return True


def _person_geometry_ok(
    box: tuple[float, float, float, float],
    *,
    min_height_px: float,
    aspect_min: float,
    aspect_max: float,
) -> bool:
    _x, _y, w, h = box
    if h < min_height_px or w < 6.0:
        return False
    aspect = w / max(h, 1e-6)
    return aspect_min <= aspect <= aspect_max


class PrivacyFacePipeline:
    """Face/head masking pipeline with false-positive gates."""

    def __init__(
        self,
        *,
        targets: Sequence[str] | str = ("face",),
        mode: str = "pixelate",
        pixel_block: int = 12,
        fail_policy: str = "best_effort",
        block_mode: str = "solid",
        require_face_backend: bool = False,
        face_model: str = "",
        face_detect_every: int = 2,
        face_full_scan_every: int = 4,
        face_max_dimension: int = 960,
        face_score_threshold: float = 0.55,
        face_nms_threshold: float = 0.30,
        face_roi_scan: bool = True,
        person_head_fallback: bool = True,
        person_model: str = "models/object_detection_yolox_2022nov.onnx",
        person_detect_every: int = 4,
        max_predicted_face_age: int = 5,
        face_expand_width: float = 1.28,
        face_expand_height: float = 1.45,
        max_person_rois: int = 8,
        max_masks: int = 48,
        apply_redaction: bool = True,
        face_detector: Any = None,
        person_detector: Any = None,
        # False-positive controls.
        face_min_confidence: float = 0.55,
        face_high_confidence: float = 0.82,
        face_confirm_hits: int = 2,
        face_confirm_max_missed: int = 2,
        face_min_size_px: float = 12.0,
        face_max_frame_fraction: float = 0.20,
        face_aspect_min: float = 0.55,
        face_aspect_max: float = 1.65,
        person_min_confidence: float = 0.55,
        person_head_high_confidence: float = 0.82,
        person_head_min_hits: int = 2,
        person_head_max_missed: int = 1,
        person_min_height_px: float = 48.0,
        person_aspect_min: float = 0.14,
        person_aspect_max: float = 1.30,
        start_hits: int = 1,
        max_missed_frames: int = 5,
        providers: Sequence[str] | str = (),
        dnn_backend: str = "cpu",
        **_unused: Any,
    ) -> None:
        if isinstance(targets, str):
            parsed = [part.strip() for part in targets.split(",") if part.strip()]
        else:
            parsed = [str(part).strip() for part in targets if str(part).strip()]
        normalized = {_norm_label(item) for item in parsed}
        if normalized and normalized != {"face"}:
            raise ValueError("PrivacyFacePipeline only accepts the face target")
        self.mode = str(mode or "pixelate").lower()
        if self.mode not in {"pixelate", "blur", "solid", "highlight"}:
            raise ValueError("privacy mode must be pixelate, blur, solid, or highlight")
        self.pixel_block = max(4, int(pixel_block))
        self.fail_policy = str(fail_policy or "best_effort").lower()
        if self.fail_policy not in {"best_effort", "hold_last", "block_frame"}:
            raise ValueError("fail_policy must be best_effort, hold_last, or block_frame")
        self.block_mode = str(block_mode or "solid").lower()
        if self.block_mode not in {"pixelate", "blur", "solid"}:
            raise ValueError("block_mode must be pixelate, blur, or solid")
        self.require_face_backend = bool(require_face_backend)
        self.face_detect_every = max(1, int(face_detect_every))
        self.face_full_scan_every = max(self.face_detect_every, int(face_full_scan_every))
        self.person_detect_every = max(1, int(person_detect_every))
        self.face_roi_scan = bool(face_roi_scan)
        self.person_head_fallback = bool(person_head_fallback)
        self.max_predicted_face_age = max(0, int(max_predicted_face_age))
        self.face_expand_width = max(1.0, float(face_expand_width))
        self.face_expand_height = max(1.0, float(face_expand_height))
        self.max_person_rois = max(1, int(max_person_rois))
        self.max_masks = max(1, int(max_masks))
        self.apply_redaction = bool(apply_redaction)

        self.face_min_confidence = float(np.clip(face_min_confidence, 0.0, 1.0))
        self.face_high_confidence = float(np.clip(max(face_high_confidence, self.face_min_confidence), 0.0, 1.0))
        self.face_confirm_hits = max(1, int(face_confirm_hits))
        self.face_confirm_max_missed = max(self.face_detect_every, int(face_confirm_max_missed))
        self.face_min_size_px = max(3.0, float(face_min_size_px))
        self.face_max_frame_fraction = float(np.clip(face_max_frame_fraction, 0.01, 0.80))
        self.face_aspect_min = max(0.2, float(face_aspect_min))
        self.face_aspect_max = max(self.face_aspect_min, float(face_aspect_max))
        self.person_min_confidence = float(np.clip(person_min_confidence, 0.0, 1.0))
        self.person_head_high_confidence = float(np.clip(max(person_head_high_confidence, self.person_min_confidence), 0.0, 1.0))
        self.person_head_min_hits = max(1, int(person_head_min_hits))
        self.person_head_max_missed = max(0, int(person_head_max_missed))
        self.person_min_height_px = max(8.0, float(person_min_height_px))
        self.person_aspect_min = max(0.05, float(person_aspect_min))
        self.person_aspect_max = max(self.person_aspect_min, float(person_aspect_max))

        if isinstance(providers, str):
            providers_value = [item.strip() for item in providers.split(",") if item.strip()]
        else:
            providers_value = [str(item) for item in providers]

        self.face_detector = face_detector or YuNetFaceDetector(
            face_model,
            score_threshold=max(float(face_score_threshold), self.face_min_confidence),
            nms_threshold=face_nms_threshold,
            max_dimension=face_max_dimension,
        )
        self.person_detector = person_detector
        if self.person_detector is None and str(person_model).strip():
            if str(person_model).strip().lower().endswith(".onnx"):
                self.person_detector = OpenCVYoloXContextDetector(
                    str(person_model),
                    conf_threshold=self.person_min_confidence,
                    dnn_backend=str(dnn_backend or "cpu"),
                    max_detections=64,
                )
            else:
                self.person_detector = OpenImageModelDetector(
                    str(person_model),
                    kind="object",
                    conf_threshold=self.person_min_confidence,
                    allowed_labels=tuple(sorted(_PERSON_LABELS)),
                    providers=providers_value,
                    batch_size=1,
                )

        self._context_tracker = PrivacyTracker(
            start_hits=1,
            max_missed=max(max_missed_frames, self.person_detect_every + 1),
            match_iou=0.16,
            center_gate=0.85,
            smooth=0.58,
            max_tracks=64,
        )
        self._face_evidence_tracker = PrivacyTracker(
            start_hits=self.face_confirm_hits,
            max_missed=self.face_confirm_max_missed,
            match_iou=0.18,
            center_gate=0.70,
            smooth=0.65,
            max_tracks=max_masks,
        )
        self._privacy_tracker = PrivacyTracker(
            start_hits=max(1, int(start_hits)),
            max_missed=max(0, int(max_missed_frames)),
            match_iou=0.18,
            center_gate=0.75,
            smooth=0.66,
            max_tracks=max_masks,
        )
        self._closed = False
        self._last_stream_id = ""
        self._last_backend_errors: dict[str, str] = {}

    @staticmethod
    def describe() -> dict[str, Any]:
        return {
            "schema": "media_format.ai.processor.v2",
            "algorithm": "privacy_redaction",
            "task": "dashcam_face_privacy",
            "media": ["video"],
            "input": {"pixel_format": "rgb24"},
            "outputs": ["media_format.ai.frame_result.v1"],
            "can_modify_media": True,
            "media_mode": "transform",
            "state_scope": "per_stream",
            "threading": "serialized",
        }

    def _call_detector(
        self,
        detector: Any,
        frame_rgb: np.ndarray,
        rois: Sequence[tuple[float, float, float, float]] | None = None,
        *,
        role: str,
    ) -> list[PrivacyDetection]:
        if detector is None:
            self._last_backend_errors[role] = "not_configured"
            return []
        try:
            try:
                values = detector.detect(frame_rgb, rois=rois) if hasattr(detector, "detect") else detector(frame_rgb, rois=rois)
            except TypeError:
                values = detector.detect(frame_rgb) if hasattr(detector, "detect") else detector(frame_rgb)
        except Exception as exc:
            self._last_backend_errors[role] = f"{type(exc).__name__}: {exc}"
            return []
        self._last_backend_errors.pop(role, None)
        output: list[PrivacyDetection] = []
        for value in values or []:
            if isinstance(value, PrivacyDetection):
                output.append(value)
                continue
            if not isinstance(value, Mapping):
                continue
            try:
                raw_box = value.get("xywh", value.get("bbox"))
                if isinstance(raw_box, Mapping):
                    raw_box = [raw_box[key] for key in ("x", "y", "w", "h")]
                coords = tuple(float(item) for item in raw_box)
                if len(coords) < 4:
                    continue
                output.append(
                    PrivacyDetection(
                        kind=_norm_label(value.get("kind", value.get("label", ""))),
                        xywh=coords[:4],
                        confidence=float(value.get("confidence", 1.0)),
                        source=str(value.get("source", "injected")),
                        attributes=dict(value.get("attributes", {})),
                    )
                )
            except Exception:
                continue
        return output

    def _upstream(self, meta: Mapping[str, Any], width: int, height: int) -> tuple[list[PrivacyDetection], list[PrivacyDetection]]:
        faces: list[PrivacyDetection] = []
        persons: list[PrivacyDetection] = []
        seen: set[tuple[str, int, int, int, int]] = set()
        for item in _iter_upstream_events(meta):
            kind = _norm_label(item.get("kind", ""))
            if kind and kind not in _EVENT_BOX_KINDS:
                continue
            box = _event_box(item, width, height)
            if box is None:
                continue
            attrs = item.get("attributes") if isinstance(item.get("attributes"), Mapping) else {}
            label = _norm_label(
                item.get("label")
                or item.get("class")
                or item.get("class_name")
                or attrs.get("scene_kind")
                or attrs.get("component")
            )
            if not label and isinstance(item.get("track"), Mapping):
                label = _norm_label(item["track"].get("label"))
            confidence = float(item.get("confidence", 0.0) or 0.0)
            key_box = tuple(int(round(value)) for value in box)
            if _is_face_label(label):
                target_kind = "face"
                target = faces
            elif _is_person_label(label):
                target_kind = "person"
                target = persons
            else:
                continue
            key = (target_kind, *key_box)
            if key in seen:
                continue
            seen.add(key)
            target.append(PrivacyDetection(target_kind, box, confidence, "upstream", {"upstream_label": label}))
        return faces, persons

    @staticmethod
    def _readiness(detector: Any, default_backend: str) -> tuple[bool, str, str]:
        if detector is None:
            return False, default_backend, "not_configured"
        status = getattr(detector, "readiness", None)
        if status is None:
            return True, default_backend, "injected"
        return bool(status.ready), str(status.backend), str(status.reason)

    def _filter_faces(self, detections: Sequence[PrivacyDetection], width: int, height: int) -> list[PrivacyDetection]:
        output: list[PrivacyDetection] = []
        for detection in detections:
            if detection.kind != "face" or detection.confidence < self.face_min_confidence:
                continue
            clipped = clip_bbox_xywh(detection.xywh, width, height, min_size=self.face_min_size_px)
            if clipped is None:
                continue
            if not _face_geometry_ok(
                clipped,
                width,
                height,
                min_size_px=self.face_min_size_px,
                max_frame_fraction=self.face_max_frame_fraction,
                aspect_min=self.face_aspect_min,
                aspect_max=self.face_aspect_max,
            ):
                continue
            output.append(PrivacyDetection("face", clipped, detection.confidence, detection.source, dict(detection.attributes)))
        return output

    def _filter_persons(self, detections: Sequence[PrivacyDetection], width: int, height: int) -> list[PrivacyDetection]:
        output: list[PrivacyDetection] = []
        for detection in detections:
            if detection.kind != "person" or detection.confidence < self.person_min_confidence:
                continue
            clipped = clip_bbox_xywh(detection.xywh, width, height, min_size=6.0)
            if clipped is None or not _person_geometry_ok(
                clipped,
                min_height_px=self.person_min_height_px,
                aspect_min=self.person_aspect_min,
                aspect_max=self.person_aspect_max,
            ):
                continue
            output.append(PrivacyDetection("person", clipped, detection.confidence, detection.source, dict(detection.attributes)))
        return output

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
        stream_id = str(meta.get("stream_id", "default"))
        if self._last_stream_id and stream_id != self._last_stream_id:
            self.reset({"stream_id": stream_id}, "stream_switch")
        self._last_stream_id = stream_id

        upstream_faces, upstream_persons = self._upstream(meta, width, height)
        context_detections = list(upstream_persons)
        person_due = frame_index % self.person_detect_every == 0
        person_time_ms = 0.0
        if self.person_detector is not None and person_due:
            person_started = time.perf_counter()
            context_detections.extend(self._call_detector(self.person_detector, frame_rgb, role="person"))
            person_time_ms = (time.perf_counter() - person_started) * 1000.0
        context_detections = self._filter_persons(context_detections, width, height)
        person_tracks = self._context_tracker.update(
            context_detections,
            frame_index=frame_index,
            width=width,
            height=height,
        )
        person_tracks = [
            track for track in person_tracks
            if track.kind == "person"
            and track.confidence >= self.person_min_confidence
            and track.missed <= self.person_head_max_missed
            and _person_geometry_ok(
                track.xywh,
                min_height_px=self.person_min_height_px,
                aspect_min=self.person_aspect_min,
                aspect_max=self.person_aspect_max,
            )
        ]

        raw_faces = list(upstream_faces)
        face_due = frame_index % self.face_detect_every == 0
        face_full_due = (frame_index % self.face_full_scan_every == 0) or not person_tracks
        face_time_ms = 0.0
        if face_due:
            face_started = time.perf_counter()
            if face_full_due:
                raw_faces.extend(self._call_detector(self.face_detector, frame_rgb, role="face"))
            if self.face_roi_scan and person_tracks:
                rois = [_head_from_person(track.xywh) for track in person_tracks[: self.max_person_rois]]
                raw_faces.extend(self._call_detector(self.face_detector, frame_rgb, rois=rois, role="face"))
            face_time_ms = (time.perf_counter() - face_started) * 1000.0
        raw_faces = self._filter_faces(raw_faces, width, height)

        immediate_faces = [det for det in raw_faces if det.confidence >= self.face_high_confidence]
        evidence_faces = [det for det in raw_faces if det.confidence < self.face_high_confidence]
        confirmed_low_tracks = self._face_evidence_tracker.update(
            evidence_faces,
            frame_index=frame_index,
            width=width,
            height=height,
        )
        gated_faces = list(immediate_faces)
        for track in confirmed_low_tracks:
            if track.missed > self.face_confirm_max_missed:
                continue
            gated_faces.append(
                PrivacyDetection(
                    "face",
                    track.xywh,
                    track.confidence,
                    "temporal_face_confirmed",
                    {"confirmation_hits": track.hits, "original_source": track.source},
                )
            )

        # Person-head fallback is intentionally stricter than direct face
        # detection.  A single low-confidence person box is not enough.
        if self.person_head_fallback:
            face_boxes = [det.xywh for det in gated_faces]
            for track in person_tracks[: self.max_person_rois]:
                stable = track.hits >= self.person_head_min_hits
                immediate = track.hits >= 1 and track.confidence >= self.person_head_high_confidence and track.missed == 0
                if not (stable or immediate):
                    continue
                head = _head_from_person(track.xywh)
                if not _face_geometry_ok(
                    head,
                    width,
                    height,
                    min_size_px=self.face_min_size_px,
                    max_frame_fraction=self.face_max_frame_fraction,
                    aspect_min=0.45,
                    aspect_max=1.80,
                ):
                    continue
                if any(bbox_iou_xywh(head, candidate) >= 0.12 for candidate in face_boxes):
                    continue
                gated_faces.append(
                    PrivacyDetection(
                        "face",
                        head,
                        max(0.45, min(0.88, track.confidence * 0.80)),
                        "person_head_fallback",
                        {
                            "fallback": True,
                            "person_track_id": track.track_id,
                            "person_hits": track.hits,
                            "person_missed": track.missed,
                        },
                    )
                )

        gated_faces = _deduplicate(gated_faces, 0.52)
        active_tracks = self._privacy_tracker.update(
            gated_faces,
            frame_index=frame_index,
            width=width,
            height=height,
        )
        active_tracks = [
            track for track in active_tracks
            if track.kind == "face" and track.missed <= self.max_predicted_face_age
        ][: self.max_masks]

        face_ready, face_backend, face_reason = self._readiness(self.face_detector, "face")
        person_ready, person_backend, person_reason = self._readiness(self.person_detector, "person")
        if self._last_backend_errors.get("face"):
            face_ready, face_reason = False, self._last_backend_errors["face"]
        if self._last_backend_errors.get("person"):
            person_ready, person_reason = False, self._last_backend_errors["person"]
        upstream_ready = bool(upstream_faces or upstream_persons)
        tracked_ready = bool(active_tracks)
        effective_ready = face_ready or person_ready or upstream_ready
        if self.fail_policy == "hold_last":
            effective_ready = effective_ready or tracked_ready
        missing_required = ["face"] if self.require_face_backend and not effective_ready else []

        if missing_required and self.fail_policy == "block_frame":
            output = self._block_output(frame_rgb)
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
                reason="privacy_fail_closed:face",
                events=[block_event],
                metrics={
                    "blocked": True,
                    "face_backend": face_backend,
                    "face_backend_reason": face_reason,
                    "person_backend": person_backend,
                    "person_backend_reason": person_reason,
                },
                extra={"frame": output},
            )

        output = frame_rgb.copy()
        events: list[dict[str, Any]] = []
        mask_pixels = 0
        highlight = self.mode == "highlight"
        for track in active_tracks:
            rect = expand_privacy_box(
                track.xywh,
                kind="face",
                width=width,
                height=height,
                face_expand_w=self.face_expand_width,
                face_expand_h=self.face_expand_height,
            )
            if rect is None:
                continue
            x, y, w, h = rect
            if self.apply_redaction and not highlight:
                apply_privacy_mask(output, rect, mode=self.mode, pixel_block=self.pixel_block)
                mask_pixels += w * h
            events.append(
                event(
                    kind="box" if highlight else "mask",
                    label="face_detected" if highlight else "face_redacted",
                    confidence=track.confidence,
                    time=pts,
                    bbox=bbox(x, y, w, h),
                    track={
                        "id": track.track_id,
                        "age": track.age,
                        "missed": track.missed,
                        "state": "predicted" if track.predicted else "detected",
                    },
                    attributes={
                        "action": self.mode,
                        "privacy_target": "face",
                        "source": track.source,
                        "predicted": track.predicted,
                        **dict(track.attributes),
                    },
                )
            )

        degraded = not effective_ready or bool(self._last_backend_errors)
        reason = ""
        if not effective_ready:
            reason = "privacy_backend_unavailable:face"
        elif not events:
            reason = "no_privacy_regions"
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return frame_result(
            algorithm="privacy_redaction",
            time=pts,
            frame_index=frame_index,
            processed=True,
            valid=True,
            degraded=degraded,
            reason=reason,
            events=events,
            metrics={
                "mode": self.mode,
                "face_masks": 0 if highlight else len(events),
                "highlighted_faces": len(events) if highlight else 0,
                "active_masks": 0 if highlight else len(events),
                "highlighted_regions": len(events) if highlight else 0,
                "mask_pixels_sum": mask_pixels,
                "raw_face_detections": len(raw_faces),
                "immediate_face_detections": len(immediate_faces),
                "temporally_confirmed_faces": len(confirmed_low_tracks),
                "person_tracks": len(person_tracks),
                "face_detector_due": face_due,
                "face_full_scan_due": face_full_due,
                "person_detector_due": person_due,
                "person_detector_ms": person_time_ms,
                "face_detector_ms": face_time_ms,
                "latency_ms": elapsed_ms,
                "face_backend": face_backend,
                "face_backend_ready": effective_ready,
                "face_backend_reason": face_reason,
                "person_backend": person_backend,
                "person_backend_reason": person_reason,
                "false_positive_gates": {
                    "face_min_confidence": self.face_min_confidence,
                    "face_high_confidence": self.face_high_confidence,
                    "face_confirm_hits": self.face_confirm_hits,
                    "person_min_confidence": self.person_min_confidence,
                    "person_head_min_hits": self.person_head_min_hits,
                },
            },
            extra={"frame": output},
        )

    def reset(self, _context: Mapping[str, Any] | str | None = None, _reason: str = "reset") -> None:
        self._context_tracker.reset()
        self._face_evidence_tracker.reset()
        self._privacy_tracker.reset()
        self._last_stream_id = ""
        self._last_backend_errors.clear()

    def flush(self, _context: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        return []

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.reset()
