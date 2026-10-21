"""Vehicle-aware license-plate proposal, OCR, and crop persistence.

This module intentionally separates plate *proposal* from plate *recognition*.
A proposal is not privacy-sensitive output by itself.  The caller decides
whether an OCR-confirmed plate is masked, saved, or emitted.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Iterable, Mapping, Sequence

import cv2
import numpy as np

try:
    import pytesseract
except Exception:  # optional runtime dependency
    pytesseract = None  # type: ignore

from .geometry import bbox_iou_xywh, clip_bbox_xywh

BBox = tuple[float, float, float, float]


@dataclass(slots=True)
class VehicleRegion:
    track_id: int
    xywh: BBox
    confidence: float = 1.0
    label: str = "vehicle"
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlateCandidate:
    vehicle_track_id: int
    xywh: BBox
    score: float
    source: str
    detector_confidence: float = 0.0
    quad: tuple[tuple[float, float], ...] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlateOCRResult:
    text: str = ""
    confidence: float = 0.0
    recognizable: bool = False
    normalized: str = ""
    crop_bgr: np.ndarray | None = None
    rectified_bgr: np.ndarray | None = None
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


def _safe_iou(a: BBox, b: BBox) -> float:
    try:
        return float(bbox_iou_xywh(a, b))
    except Exception:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        x1, y1 = max(ax, bx), max(ay, by)
        x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
        inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        union = max(1e-6, aw * ah + bw * bh - inter)
        return inter / union


def _ring_means(gray: np.ndarray, x: int, y: int, w: int, h: int) -> tuple[float, float]:
    height, width = gray.shape[:2]
    pad_x = max(3, int(round(w * 0.30)))
    pad_y = max(3, int(round(h * 0.50)))
    x0, y0 = max(0, x - pad_x), max(0, y - pad_y)
    x1, y1 = min(width, x + w + pad_x), min(height, y + h + pad_y)
    outer = gray[y0:y1, x0:x1]
    inner = gray[y:y + h, x:x + w]
    if inner.size == 0 or outer.size == 0:
        return 0.0, 0.0
    mask = np.ones(outer.shape, dtype=np.uint8)
    mask[y - y0:y - y0 + h, x - x0:x - x0 + w] = 0
    background = outer[mask.astype(bool)]
    return float(inner.mean()), float(background.mean() if background.size else inner.mean())


def _order_quad(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    sums = points.sum(axis=1)
    diffs = np.diff(points, axis=1).ravel()
    return np.asarray(
        [
            points[int(np.argmin(sums))],
            points[int(np.argmin(diffs))],
            points[int(np.argmax(sums))],
            points[int(np.argmax(diffs))],
        ],
        dtype=np.float32,
    )


class VehiclePlateCandidateDetector:
    """Generate and rank plate-shaped regions inside tracked vehicles.

    The fallback is deliberately restricted to vehicle tracks.  It combines
    bright-rectangle and horizontal-character responses, then returns only the
    highest scoring one or two candidates for each vehicle.
    """

    def __init__(
        self,
        *,
        max_candidates_per_vehicle: int = 1,
        min_score: float = 4.25,
        min_width: int = 18,
        min_height: int = 6,
        aspect_min: float = 1.6,
        aspect_max: float = 7.0,
        nms_iou: float = 0.35,
        max_vehicle_dimension: int = 480,
        search_y_min: float = 0.28,
        search_y_max: float = 0.98,
        min_rectangularity: float = 0.30,
        min_edge_density: float = 0.055,
        max_edge_density: float = 0.78,
        min_text_band_score: float = 0.08,
        min_neutrality: float = 0.20,
    ) -> None:
        self.max_candidates_per_vehicle = max(1, min(2, int(max_candidates_per_vehicle)))
        self.min_score = float(min_score)
        self.min_width = max(8, int(min_width))
        self.min_height = max(3, int(min_height))
        self.aspect_min = max(1.1, float(aspect_min))
        self.aspect_max = max(self.aspect_min, float(aspect_max))
        self.nms_iou = float(np.clip(nms_iou, 0.05, 0.95))
        self.max_vehicle_dimension = max(160, int(max_vehicle_dimension))
        self.search_y_min = float(np.clip(search_y_min, 0.0, 0.95))
        self.search_y_max = float(np.clip(max(search_y_max, self.search_y_min + 0.05), 0.05, 1.0))
        self.min_rectangularity = float(np.clip(min_rectangularity, 0.05, 1.0))
        self.min_edge_density = float(np.clip(min_edge_density, 0.0, 1.0))
        self.max_edge_density = float(np.clip(max(max_edge_density, self.min_edge_density), 0.0, 1.0))
        self.min_text_band_score = float(np.clip(min_text_band_score, 0.0, 1.0))
        self.min_neutrality = float(np.clip(min_neutrality, 0.0, 1.0))

    def _score_box(
        self,
        gray: np.ndarray,
        color_bgr: np.ndarray,
        box: tuple[int, int, int, int],
        contour_area: float,
        *,
        vehicle_width: int,
        vehicle_height: int,
        source_bias: float,
    ) -> tuple[float, dict[str, float]]:
        x, y, w, h = box
        aspect = w / max(float(h), 1.0)
        rectangularity = float(contour_area / max(float(w * h), 1.0))
        roi = gray[y:y + h, x:x + w]
        roi_bgr = color_bgr[y:y + h, x:x + w]
        if roi.size == 0 or roi_bgr.size == 0:
            return 0.0, {}
        grad_x = np.abs(cv2.Sobel(roi, cv2.CV_32F, 1, 0, ksize=3))
        edge_map = grad_x > 30.0
        edge_density = float(edge_map.mean())
        # Character-like plates contain multiple vertical stroke groups spread
        # across the width.  One bright reflection or bumper seam does not.
        column_energy = edge_map.mean(axis=0) if edge_map.ndim == 2 else np.zeros((w,), dtype=np.float32)
        active_columns = float((column_energy > 0.12).mean()) if column_energy.size else 0.0
        transitions = float(np.count_nonzero(np.diff((column_energy > 0.12).astype(np.int8)) != 0))
        text_band_score = float(np.clip(0.65 * active_columns + 0.35 * transitions / max(w * 0.25, 1.0), 0.0, 1.0))
        inside, background = _ring_means(gray, x, y, w, h)
        local_contrast = float((inside - background) / 255.0)
        contrast_strength = float(np.clip(abs(local_contrast) * 3.0, 0.0, 1.0))
        hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        mean_saturation = float(hsv[..., 1].mean() / 255.0)
        neutrality = float(np.clip(1.0 - mean_saturation, 0.0, 1.0))
        center_x = (x + 0.5 * w) / max(vehicle_width, 1)
        center_y = (y + 0.5 * h) / max(vehicle_height, 1)
        location = math.exp(-((center_x - 0.50) / 0.34) ** 2 - 0.5 * ((center_y - 0.72) / 0.28) ** 2)
        aspect_score = math.exp(-abs(math.log(max(aspect, 0.01) / 3.3)))
        if rectangularity < self.min_rectangularity:
            return 0.0, {}
        if not (self.min_edge_density <= edge_density <= self.max_edge_density):
            return 0.0, {}
        if text_band_score < self.min_text_band_score:
            return 0.0, {}
        if neutrality < self.min_neutrality and source_bias <= 0.1:
            return 0.0, {}
        score = (
            source_bias
            + 1.35 * rectangularity
            + 1.65 * edge_density
            + 1.35 * text_band_score
            + 0.75 * neutrality
            + 1.95 * location
            + 0.70 * aspect_score
            + 0.65 * contrast_strength
            + 0.003 * min(w, 120)
        )
        return float(score), {
            "rectangularity": rectangularity,
            "edge_density": edge_density,
            "text_band_score": text_band_score,
            "local_contrast": local_contrast,
            "contrast_strength": contrast_strength,
            "neutrality": neutrality,
            "location_prior": location,
            "aspect": aspect,
        }

    def _vehicle_candidates(self, frame_bgr: np.ndarray, vehicle: VehicleRegion) -> list[PlateCandidate]:
        height, width = frame_bgr.shape[:2]
        clipped = clip_bbox_xywh(vehicle.xywh, width, height, min_size=24.0)
        if clipped is None:
            return []
        vx_f, vy_f, vw_f, vh_f = clipped
        vx, vy, vw, vh = int(vx_f), int(vy_f), int(vw_f), int(vh_f)
        crop_source = frame_bgr[vy:vy + vh, vx:vx + vw]
        if crop_source.size == 0:
            return []
        scale = min(1.0, self.max_vehicle_dimension / max(vw, vh, 1))
        if scale < 0.999:
            crop = cv2.resize(crop_source, (max(1, int(round(vw * scale))), max(1, int(round(vh * scale)))), interpolation=cv2.INTER_AREA)
        else:
            crop = crop_source
        scan_h, scan_w = crop.shape[:2]
        gray_source = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray_source)
        search_top = int(round(scan_h * self.search_y_min))
        search_bottom = int(round(scan_h * self.search_y_max))
        rows: list[PlateCandidate] = []

        def add_contours(mask: np.ndarray, source: str, source_bias: float) -> None:
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                area = float(cv2.contourArea(contour))
                x, y, w, h = cv2.boundingRect(contour)
                if w < self.min_width or h < self.min_height:
                    continue
                if w > min(260, int(vw * 0.72)) or h > min(100, int(vh * 0.38)):
                    continue
                aspect = w / max(float(h), 1.0)
                if not (self.aspect_min <= aspect <= self.aspect_max):
                    continue
                if area / max(float(w * h), 1.0) < 0.18:
                    continue
                if y + h < search_top or y > search_bottom:
                    continue
                score, features = self._score_box(
                    gray,
                    crop,
                    (x, y, w, h),
                    area,
                    vehicle_width=scan_w,
                    vehicle_height=scan_h,
                    source_bias=source_bias,
                )
                if score < self.min_score:
                    continue
                rect = cv2.minAreaRect(contour)
                quad_local = cv2.boxPoints(rect).astype(np.float32)
                inv_scale = 1.0 / max(scale, 1e-9)
                quad_global = tuple((float(vx + px * inv_scale), float(vy + py * inv_scale)) for px, py in quad_local)
                rows.append(
                    PlateCandidate(
                        vehicle_track_id=int(vehicle.track_id),
                        xywh=(float(vx + x * inv_scale), float(vy + y * inv_scale), float(w * inv_scale), float(h * inv_scale)),
                        score=float(score),
                        detector_confidence=float(np.clip(score / 6.0, 0.0, 1.0)),
                        source=source,
                        quad=quad_global,
                        attributes={
                            **features,
                            "vehicle_label": vehicle.label,
                            "vehicle_confidence": float(vehicle.confidence),
                        },
                    )
                )

        # Plate backgrounds are often locally bright even when the vehicle is
        # dark. Multiple thresholds make the fallback less sensitive to camera
        # exposure and windshield glare.
        for threshold in (80, 105, 130, 160, 195):
            _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
            mask[:search_top] = 0
            mask[search_bottom:] = 0
            add_contours(mask, f"vehicle_bright_{threshold}", 0.0)

        blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, cv2.getStructuringElement(cv2.MORPH_RECT, (17, 5)))
        grad = np.abs(cv2.Sobel(blackhat, cv2.CV_32F, 1, 0, ksize=3))
        grad = (255.0 * (grad - grad.min()) / (grad.max() - grad.min() + 1e-6)).astype(np.uint8)
        grad = cv2.morphologyEx(grad, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (17, 3)))
        _, char_mask = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        char_mask = cv2.morphologyEx(char_mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3)))
        char_mask[:search_top] = 0
        char_mask[search_bottom:] = 0
        add_contours(char_mask, "vehicle_blackhat", 1.0)

        rows.sort(key=lambda item: (item.score, item.detector_confidence), reverse=True)
        kept: list[PlateCandidate] = []
        for candidate in rows:
            if any(_safe_iou(candidate.xywh, existing.xywh) >= self.nms_iou for existing in kept):
                continue
            kept.append(candidate)
            if len(kept) >= self.max_candidates_per_vehicle:
                break
        return kept

    def detect(self, frame_rgb: np.ndarray, vehicles: Sequence[VehicleRegion]) -> list[PlateCandidate]:
        if frame_rgb is None or frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
            return []
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        output: list[PlateCandidate] = []
        for vehicle in vehicles:
            output.extend(self._vehicle_candidates(frame_bgr, vehicle))
        return output


class TesseractPlateOCR:
    """Small ROI-only OCR backend with plate-format consensus scoring."""

    def __init__(
        self,
        *,
        region_profile: str = "us_generic",
        min_chars: int = 4,
        max_chars: int = 10,
        min_confidence: float = 0.55,
        output_size: tuple[int, int] = (240, 80),
        max_variants: int = 4,
        timeout_s: float = 3.0,
    ) -> None:
        self.region_profile = str(region_profile or "generic").lower()
        self.min_chars = max(2, int(min_chars))
        self.max_chars = max(self.min_chars, int(max_chars))
        self.min_confidence = float(np.clip(min_confidence, 0.0, 1.0))
        self.output_size = (max(96, int(output_size[0])), max(32, int(output_size[1])))
        self.max_variants = max(1, min(8, int(max_variants)))
        self.timeout_s = max(0.1, float(timeout_s))
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return pytesseract is not None and shutil.which(pytesseract.pytesseract.tesseract_cmd) is not None

    @staticmethod
    def normalize(text: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", str(text).upper())

    def _valid(self, text: str) -> bool:
        if not (self.min_chars <= len(text) <= self.max_chars):
            return False
        if not any(char.isalpha() for char in text) or not any(char.isdigit() for char in text):
            return False
        if len(set(text)) <= 2:
            return False
        return True

    def _pattern_score(self, text: str) -> float:
        score = 0.0
        if self._valid(text):
            score += 1.0
        if self.region_profile.startswith("us"):
            if re.fullmatch(r"[A-Z]{2,3}[0-9]{3,5}", text):
                score += 1.25
            elif re.fullmatch(r"[A-Z0-9]{5,8}", text):
                score += 0.45
        else:
            if re.fullmatch(r"[A-Z0-9]{4,10}", text):
                score += 0.50
        return score

    def _extract_crop(self, frame_bgr: np.ndarray, candidate: PlateCandidate) -> tuple[np.ndarray, np.ndarray]:
        height, width = frame_bgr.shape[:2]
        x_f, y_f, w_f, h_f = candidate.xywh
        pad_x = max(5, int(round(w_f * 0.45)))
        pad_y = max(4, int(round(h_f * 0.65)))
        x0 = max(0, int(math.floor(x_f)) - pad_x)
        y0 = max(0, int(math.floor(y_f)) - pad_y)
        x1 = min(width, int(math.ceil(x_f + w_f)) + pad_x)
        y1 = min(height, int(math.ceil(y_f + h_f)) + pad_y)
        crop = frame_bgr[y0:y1, x0:x1].copy()
        if crop.size == 0:
            return crop, crop

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        threshold_value = max(40, int(np.percentile(gray, 50)))
        _, bright = cv2.threshold(gray, threshold_value, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rectified: np.ndarray | None = None
        if contours:
            for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:6]:
                rect = cv2.minAreaRect(contour)
                rw, rh = rect[1]
                small, large = min(rw, rh), max(rw, rh)
                if small < 4 or large / max(small, 1.0) < 1.45:
                    continue
                points = _order_quad(cv2.boxPoints(rect))
                out_w, out_h = self.output_size
                destination = np.asarray(
                    [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
                    dtype=np.float32,
                )
                rectified = cv2.warpPerspective(crop, cv2.getPerspectiveTransform(points, destination), (out_w, out_h))
                break
        if rectified is None:
            rectified = cv2.resize(crop, self.output_size, interpolation=cv2.INTER_CUBIC)
        return crop, rectified

    def recognize(self, frame_rgb: np.ndarray, candidate: PlateCandidate) -> PlateOCRResult:
        if not self.ready:
            return PlateOCRResult(reason="pytesseract_unavailable")
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        crop, rectified = self._extract_crop(frame_bgr, candidate)
        if crop.size == 0 or rectified.size == 0:
            return PlateOCRResult(crop_bgr=crop, rectified_bgr=rectified, reason="empty_crop")

        gray = cv2.cvtColor(rectified, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(gray)
        _, otsu = cv2.threshold(clahe, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        variants: list[tuple[str, np.ndarray]] = [
            ("gray", gray),
            ("clahe", clahe),
            ("otsu", otsu),
            ("otsu_inv", 255 - otsu),
        ][: self.max_variants]
        raw_rows: list[dict[str, Any]] = []
        whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        deadline = time.monotonic() + self.timeout_s
        with self._lock:
            for variant_name, image in variants:
                for psm in (7, 8):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    config = f"--oem 3 --psm {psm} -c tessedit_char_whitelist={whitelist}"
                    try:
                        data = pytesseract.image_to_data(
                            image, config=config, output_type=pytesseract.Output.DICT,
                            timeout=remaining,
                        )
                    except Exception as exc:
                        return PlateOCRResult(
                            crop_bgr=crop,
                            rectified_bgr=rectified,
                            reason=f"tesseract_error:{type(exc).__name__}:{exc}",
                        )
                    words: list[str] = []
                    confidences: list[float] = []
                    for token, raw_conf in zip(data.get("text", []), data.get("conf", []), strict=False):
                        token_norm = self.normalize(token)
                        try:
                            conf = float(raw_conf)
                        except Exception:
                            conf = -1.0
                        if token_norm and conf >= 0.0:
                            words.append(token_norm)
                            confidences.append(conf)
                    text = self.normalize("".join(words))
                    if not text:
                        continue
                    raw_rows.append(
                        {
                            "text": text,
                            "variant": variant_name,
                            "psm": psm,
                            "tesseract_confidence": float(sum(confidences) / (100.0 * len(confidences)) if confidences else 0.0),
                            "pattern_score": self._pattern_score(text),
                        }
                    )

        valid_rows = [row for row in raw_rows if self._valid(str(row["text"]))]
        if not valid_rows:
            return PlateOCRResult(
                crop_bgr=crop,
                rectified_bgr=rectified,
                alternatives=raw_rows,
                reason="no_valid_plate_text",
            )
        counts = Counter(str(row["text"]) for row in valid_rows)
        scored: list[tuple[float, float, str, float]] = []
        reliability = {"gray": 1.00, "clahe": 0.98, "otsu": 0.84, "otsu_inv": 0.82}
        total_weight = max(1e-6, sum(reliability.get(name, 0.75) for name, _ in variants) * 2.0)
        for text, count in counts.items():
            rows = [row for row in valid_rows if row["text"] == text]
            avg_tess = float(sum(float(row["tesseract_confidence"]) for row in rows) / len(rows))
            pattern = max(float(row["pattern_score"]) for row in rows)
            weighted_votes = sum(reliability.get(str(row["variant"]), 0.75) for row in rows)
            consensus = weighted_votes / total_weight
            score = 0.50 * consensus + 0.28 * avg_tess + 0.22 * min(1.0, pattern / 2.25)
            # Detector/geometry evidence contributes only a small amount; OCR
            # must still carry the recognition decision.
            score += 0.08 * float(np.clip(candidate.detector_confidence, 0.0, 1.0))
            # Keep weighted vote as the second sort key so native gray/CLAHE
            # consensus wins exact score ties over hard-threshold artifacts.
            scored.append((score, weighted_votes, text, avg_tess))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        confidence, weighted_votes, text, avg_tess = scored[0]
        matching_rows = counts.get(text, 0)
        multi_view_consensus = matching_rows >= 2 and weighted_votes >= 1.60
        exceptional_single = avg_tess >= 0.88 and self._pattern_score(text) >= 1.0
        recognizable = bool(confidence >= self.min_confidence and (multi_view_consensus or exceptional_single))
        return PlateOCRResult(
            text=text if recognizable else "",
            normalized=text,
            confidence=float(np.clip(confidence, 0.0, 1.0)),
            recognizable=recognizable,
            crop_bgr=crop,
            rectified_bgr=rectified,
            alternatives=sorted(valid_rows, key=lambda row: (counts[str(row["text"])], row["pattern_score"], row["tesseract_confidence"]), reverse=True),
            reason="" if recognizable else "ocr_confidence_below_threshold",
        )


def save_recognized_plate(
    result: PlateOCRResult,
    *,
    output_dir: str | Path,
    stream_id: str,
    vehicle_track_id: int,
    frame_index: int,
    time_seconds: float,
    candidate: PlateCandidate,
) -> tuple[str, str]:
    """Save the rectified crop and append a JSONL audit entry."""
    if not result.recognizable or result.rectified_bgr is None or result.rectified_bgr.size == 0:
        return "", ""
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_stream = re.sub(r"[^A-Za-z0-9_.-]+", "_", stream_id or "stream")[:64]
    safe_text = re.sub(r"[^A-Z0-9]+", "", result.text)[:16] or "UNKNOWN"
    stem = f"{safe_stream}_vehicle{int(vehicle_track_id):04d}_{safe_text}_frame{int(frame_index):08d}"
    image_path = root / f"{stem}.jpg"
    metadata_path = root / "recognized_plates.jsonl"
    tmp_path = image_path.with_suffix(".tmp.jpg")
    if not cv2.imwrite(str(tmp_path), result.rectified_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95]):
        return "", ""
    tmp_path.replace(image_path)
    record = {
        "stream_id": stream_id,
        "vehicle_track_id": int(vehicle_track_id),
        "frame_index": int(frame_index),
        "time_seconds": float(time_seconds),
        "text": result.text,
        "ocr_confidence": float(result.confidence),
        "plate_bbox_xywh": [float(value) for value in candidate.xywh],
        "candidate_score": float(candidate.score),
        "candidate_source": candidate.source,
        "image": image_path.name,
    }
    with metadata_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return str(image_path), str(metadata_path)
