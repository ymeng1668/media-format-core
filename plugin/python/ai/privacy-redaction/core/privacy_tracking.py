"""Small deterministic multi-target tracker for media-format privacy transforms.

The tracker is intentionally model-agnostic.  It accepts face/head/plate
observations and keeps conservative masks alive between scheduled detector
frames and short detector dropouts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable

from .geometry import bbox_iou_xywh, clip_bbox_xywh

BBoxXYWH = tuple[float, float, float, float]


@dataclass(slots=True)
class PrivacyDetection:
    kind: str
    xywh: BBoxXYWH
    confidence: float
    source: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PrivacyTrack:
    track_id: int
    kind: str
    xywh: BBoxXYWH
    confidence: float
    source: str
    first_frame: int
    last_frame: int
    hits: int = 1
    missed: int = 0
    age: int = 1
    vx: float = 0.0
    vy: float = 0.0
    vw: float = 0.0
    vh: float = 0.0
    predicted: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)

    def predict_box(self) -> BBoxXYWH:
        x, y, w, h = self.xywh
        return x + self.vx, y + self.vy, max(1.0, w + self.vw), max(1.0, h + self.vh)

    def update(self, detection: PrivacyDetection, frame_index: int, smooth: float) -> None:
        old = self.xywh
        new = detection.xywh
        alpha = min(1.0, max(0.0, float(smooth)))
        blended = tuple((1.0 - alpha) * a + alpha * b for a, b in zip(old, new))
        self.vx = 0.70 * self.vx + 0.30 * (blended[0] - old[0])
        self.vy = 0.70 * self.vy + 0.30 * (blended[1] - old[1])
        self.vw = 0.70 * self.vw + 0.30 * (blended[2] - old[2])
        self.vh = 0.70 * self.vh + 0.30 * (blended[3] - old[3])
        self.xywh = blended  # type: ignore[assignment]
        self.confidence = float(max(0.0, min(1.0, 0.65 * self.confidence + 0.35 * detection.confidence)))
        self.source = detection.source
        self.attributes.update(detection.attributes)
        self.hits += 1
        self.missed = 0
        self.age += 1
        self.last_frame = int(frame_index)
        self.predicted = False

    def miss(self, frame_index: int, width: int, height: int, damping: float) -> None:
        self.age += 1
        self.missed += 1
        self.last_frame = int(frame_index)
        predicted = self.predict_box()
        clipped = clip_bbox_xywh(predicted, width, height, min_size=1.0)
        if clipped is not None:
            self.xywh = clipped
        self.vx *= damping
        self.vy *= damping
        self.vw *= damping
        self.vh *= damping
        self.predicted = True


def _center_distance_ratio(a: BBoxXYWH, b: BBoxXYWH) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    acx, acy = ax + aw * 0.5, ay + ah * 0.5
    bcx, bcy = bx + bw * 0.5, by + bh * 0.5
    distance = math.hypot(acx - bcx, acy - bcy)
    scale = max(math.hypot(aw, ah), math.hypot(bw, bh), 1.0)
    return distance / scale


class PrivacyTracker:
    """Greedy IoU/center tracker optimized for masking, not identity analytics."""

    def __init__(
        self,
        *,
        start_hits: int = 1,
        max_missed: int = 8,
        match_iou: float = 0.18,
        center_gate: float = 0.85,
        smooth: float = 0.60,
        velocity_damping: float = 0.82,
        max_tracks: int = 128,
    ) -> None:
        self.start_hits = max(1, int(start_hits))
        self.max_missed = max(0, int(max_missed))
        self.match_iou = float(max(0.0, min(1.0, match_iou)))
        self.center_gate = max(0.05, float(center_gate))
        self.smooth = float(max(0.05, min(1.0, smooth)))
        self.velocity_damping = float(max(0.0, min(1.0, velocity_damping)))
        self.max_tracks = max(8, int(max_tracks))
        self._tracks: list[PrivacyTrack] = []
        self._next_id = 1

    @property
    def tracks(self) -> tuple[PrivacyTrack, ...]:
        return tuple(self._tracks)

    def reset(self) -> None:
        self._tracks.clear()
        self._next_id = 1

    def update(
        self,
        detections: Iterable[PrivacyDetection],
        *,
        frame_index: int,
        width: int,
        height: int,
    ) -> list[PrivacyTrack]:
        clean: list[PrivacyDetection] = []
        for det in detections:
            clipped = clip_bbox_xywh(det.xywh, width, height, min_size=1.0)
            if clipped is None:
                continue
            clean.append(
                PrivacyDetection(
                    kind=str(det.kind),
                    xywh=clipped,
                    confidence=float(max(0.0, min(1.0, det.confidence))),
                    source=str(det.source),
                    attributes=dict(det.attributes),
                )
            )

        candidates: list[tuple[float, int, int]] = []
        for track_index, track in enumerate(self._tracks):
            predicted = track.predict_box()
            for det_index, det in enumerate(clean):
                if det.kind != track.kind:
                    continue
                track_vehicle = track.attributes.get("vehicle_track_id")
                det_vehicle = det.attributes.get("vehicle_track_id")
                if track_vehicle is not None and det_vehicle is not None and track_vehicle != det_vehicle:
                    continue
                overlap = bbox_iou_xywh(predicted, det.xywh)
                center_ratio = _center_distance_ratio(predicted, det.xywh)
                if overlap < self.match_iou and center_ratio > self.center_gate:
                    continue
                score = 2.0 * overlap + max(0.0, 1.0 - center_ratio) + 0.20 * det.confidence
                candidates.append((score, track_index, det_index))
        candidates.sort(reverse=True)

        used_tracks: set[int] = set()
        used_detections: set[int] = set()
        for _score, track_index, det_index in candidates:
            if track_index in used_tracks or det_index in used_detections:
                continue
            self._tracks[track_index].update(clean[det_index], frame_index, self.smooth)
            used_tracks.add(track_index)
            used_detections.add(det_index)

        for index, track in enumerate(self._tracks):
            if index not in used_tracks:
                track.miss(frame_index, width, height, self.velocity_damping)

        for det_index, det in enumerate(clean):
            if det_index in used_detections:
                continue
            self._tracks.append(
                PrivacyTrack(
                    track_id=self._next_id,
                    kind=det.kind,
                    xywh=det.xywh,
                    confidence=det.confidence,
                    source=det.source,
                    first_frame=int(frame_index),
                    last_frame=int(frame_index),
                    attributes=dict(det.attributes),
                )
            )
            self._next_id += 1

        self._tracks = [track for track in self._tracks if track.missed <= self.max_missed]
        if len(self._tracks) > self.max_tracks:
            self._tracks.sort(
                key=lambda track: (track.missed == 0, track.hits, track.confidence, -track.missed),
                reverse=True,
            )
            self._tracks = self._tracks[: self.max_tracks]

        return [
            track
            for track in self._tracks
            if track.hits >= self.start_hits and track.missed <= self.max_missed
        ]
