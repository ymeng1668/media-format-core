"""Adaptive, task-specific frame preprocessing for media-format AI.

The original decoded RGB frame is kept as the source of truth.  Algorithms ask
for lazily generated views instead of destructively filtering the shared frame.
This avoids a universal FFT/filter/IFFT stage and allows media-format to cache common
luma, contrast-enhanced, and low-light views once per frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Iterable, Iterator, Mapping, Sequence

import cv2
import numpy as np


class AIPreprocessProfile(str, Enum):
    MODEL_NATIVE = "model_native"
    FAST_LUMA = "fast_luma"
    OCR_NATIVE = "ocr_native"
    OCR_RECOVERY = "ocr_recovery"
    BARCODE_NATIVE = "barcode_native"
    BARCODE_RECOVERY = "barcode_recovery"
    TRAFFIC_SIGN_NATIVE = "traffic_sign_native"
    TRAFFIC_SIGN_LOW_LIGHT = "traffic_sign_low_light"
    TRACKING_STABLE = "tracking_stable"
    REID_STABLE = "reid_stable"
    FREQUENCY_PROBE = "frequency_probe"
    FREQUENCY_RESTORE_ROI = "frequency_restore_roi"


@dataclass(frozen=True)
class FrameQuality:
    luma_mean: float
    luma_std: float
    blur_variance: float
    shadow_fraction: float
    highlight_fraction: float
    edge_density: float
    high_frequency_ratio: float = 0.0
    periodic_noise_score: float = 0.0
    analysis_scale: float = 1.0
    flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "luma_mean": self.luma_mean,
            "luma_std": self.luma_std,
            "blur_variance": self.blur_variance,
            "shadow_fraction": self.shadow_fraction,
            "highlight_fraction": self.highlight_fraction,
            "edge_density": self.edge_density,
            "high_frequency_ratio": self.high_frequency_ratio,
            "periodic_noise_score": self.periodic_noise_score,
            "analysis_scale": self.analysis_scale,
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class AIFrameView:
    frame: np.ndarray
    profile: str
    source_to_view: np.ndarray
    view_to_source: np.ndarray
    source_roi: tuple[int, int, int, int]
    enhanced: bool = False
    operations: tuple[str, ...] = ()
    quality: FrameQuality | None = None

    def map_point_to_source(self, x: float, y: float) -> tuple[float, float]:
        p = self.view_to_source @ np.asarray([float(x), float(y), 1.0], dtype=np.float64)
        if abs(float(p[2])) < 1e-12:
            return float(p[0]), float(p[1])
        return float(p[0] / p[2]), float(p[1] / p[2])

    def map_bbox_to_source(self, box: Sequence[float]) -> tuple[float, float, float, float]:
        if len(box) < 4:
            raise ValueError("bbox must contain x, y, w, h")
        x, y, w, h = [float(v) for v in box[:4]]
        p0 = self.map_point_to_source(x, y)
        p1 = self.map_point_to_source(x + w, y + h)
        return p0[0], p0[1], max(0.0, p1[0] - p0[0]), max(0.0, p1[1] - p0[1])

    def map_polygon_to_source(self, polygon: Iterable[Sequence[float]]) -> list[list[float]]:
        return [[sx, sy] for sx, sy in (self.map_point_to_source(p[0], p[1]) for p in polygon)]


@dataclass
class StableConditionController:
    """Hysteresis controller that prevents frame-to-frame enhancement flicker."""

    enter_after: int = 3
    exit_after: int = 6
    active: bool = False
    _bad_count: int = 0
    _good_count: int = 0

    def update(self, quality: FrameQuality, *, trigger_flags: tuple[str, ...] = ("low_light", "low_contrast")) -> bool:
        degraded = any(flag in quality.flags for flag in trigger_flags)
        if degraded:
            self._bad_count += 1
            self._good_count = 0
            if not self.active and self._bad_count >= max(1, int(self.enter_after)):
                self.active = True
        else:
            self._good_count += 1
            self._bad_count = 0
            if self.active and self._good_count >= max(1, int(self.exit_after)):
                self.active = False
        return self.active

    def reset(self) -> None:
        self.active = False
        self._bad_count = 0
        self._good_count = 0


def _clip_roi(roi: Sequence[float] | None, width: int, height: int) -> tuple[int, int, int, int]:
    if roi is None:
        return 0, 0, width, height
    if len(roi) < 4:
        raise ValueError("ROI must contain x, y, w, h")
    x, y, w, h = [int(round(float(v))) for v in roi[:4]]
    x0 = max(0, min(width, x))
    y0 = max(0, min(height, y))
    x1 = max(x0, min(width, x + max(0, w)))
    y1 = max(y0, min(height, y + max(0, h)))
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"empty ROI after clipping: {roi!r}")
    return x0, y0, x1 - x0, y1 - y0


def _gray(rgb_or_gray: np.ndarray) -> np.ndarray:
    if rgb_or_gray.ndim == 2:
        return rgb_or_gray
    if rgb_or_gray.ndim == 3 and rgb_or_gray.shape[2] == 1:
        return rgb_or_gray[..., 0]
    return cv2.cvtColor(rgb_or_gray, cv2.COLOR_RGB2GRAY)


def _gamma_lut(mean_luma: float, target: float = 110.0) -> np.ndarray:
    mean = min(254.0, max(1.0, float(mean_luma))) / 255.0
    target_norm = min(0.95, max(0.05, float(target) / 255.0))
    gamma = math.log(target_norm) / math.log(mean)
    gamma = float(np.clip(gamma, 0.55, 1.8))
    return np.asarray([np.clip(((i / 255.0) ** gamma) * 255.0, 0, 255) for i in range(256)], dtype=np.uint8)


def _unsharp(gray: np.ndarray, *, sigma: float = 1.2, amount: float = 0.65) -> np.ndarray:
    blurred = cv2.GaussianBlur(gray, (0, 0), sigmaX=max(0.1, float(sigma)))
    return cv2.addWeighted(gray, 1.0 + float(amount), blurred, -float(amount), 0.0)


def frequency_restore_luma(gray: np.ndarray, *, amount: float = 0.45, cutoff_ratio: float = 0.16) -> np.ndarray:
    """Conservative FFT high-boost restoration for a small degraded ROI.

    This does not invent missing barcode/text detail and is intentionally a
    recovery-only operation.  It should be followed by OCR checksum/confidence
    or temporal validation.
    """

    src = np.asarray(gray, dtype=np.float32)
    if src.ndim != 2 or min(src.shape) < 8:
        return np.asarray(gray).copy()
    h, w = src.shape
    yy, xx = np.ogrid[:h, :w]
    cy, cx = h / 2.0, w / 2.0
    radius2 = (yy - cy) ** 2 + (xx - cx) ** 2
    sigma = max(2.0, min(h, w) * max(0.03, float(cutoff_ratio)))
    low = np.exp(-radius2 / (2.0 * sigma * sigma)).astype(np.float32)
    transfer = 1.0 + float(np.clip(amount, 0.0, 1.5)) * (1.0 - low)
    spectrum = np.fft.fftshift(np.fft.fft2(src))
    restored = np.fft.ifft2(np.fft.ifftshift(spectrum * transfer)).real
    return np.clip(restored, 0, 255).astype(np.uint8)


def phase_correlate_translation(
    previous_rgb_or_gray: np.ndarray,
    current_rgb_or_gray: np.ndarray,
    *,
    max_dimension: int = 512,
) -> dict[str, float]:
    """Estimate dominant translational camera motion using phase correlation.

    The returned ``dx``/``dy`` values are in source-image pixels.  This is a
    motion-analysis helper, not an image-enhancement step; it performs no IFFT
    reconstruction of the model input.  Callers should use feature/homography
    estimation instead when rotation, zoom, or perspective dominates.
    """

    previous = _gray(np.asarray(previous_rgb_or_gray))
    current = _gray(np.asarray(current_rgb_or_gray))
    if previous.shape != current.shape or previous.ndim != 2:
        raise ValueError("phase correlation requires equal-size 2-D images")
    if min(previous.shape) < 8:
        return {"dx": 0.0, "dy": 0.0, "response": 0.0, "scale": 1.0}

    scale = 1.0
    h, w = previous.shape
    if max_dimension > 0 and max(h, w) > int(max_dimension):
        scale = float(max_dimension) / float(max(h, w))
        size = (max(8, int(round(w * scale))), max(8, int(round(h * scale))))
        previous = cv2.resize(previous, size, interpolation=cv2.INTER_AREA)
        current = cv2.resize(current, size, interpolation=cv2.INTER_AREA)
    previous_f = previous.astype(np.float32, copy=False)
    current_f = current.astype(np.float32, copy=False)
    window = cv2.createHanningWindow((previous_f.shape[1], previous_f.shape[0]), cv2.CV_32F)
    shift, response = cv2.phaseCorrelate(previous_f, current_f, window)
    inv_scale = 1.0 / max(scale, 1e-12)
    return {
        "dx": float(shift[0]) * inv_scale,
        "dy": float(shift[1]) * inv_scale,
        "response": float(np.clip(response, 0.0, 1.0)),
        "scale": float(scale),
    }


def _frequency_metrics(gray: np.ndarray) -> tuple[float, float]:
    src = np.asarray(gray, dtype=np.float32)
    if src.ndim != 2 or min(src.shape) < 16:
        return 0.0, 0.0
    max_side = max(src.shape)
    if max_side > 512:
        scale = 512.0 / max_side
        src = cv2.resize(src, (max(16, int(src.shape[1] * scale)), max(16, int(src.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    src = src - float(src.mean())
    window = np.outer(np.hanning(src.shape[0]), np.hanning(src.shape[1])).astype(np.float32)
    power = np.abs(np.fft.fftshift(np.fft.fft2(src * window))) ** 2
    total = float(power.sum()) + 1e-9
    h, w = power.shape
    yy, xx = np.ogrid[:h, :w]
    cy, cx = h // 2, w // 2
    rr = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    high = float(power[rr >= min(h, w) * 0.25].sum() / total)

    # Strong narrow off-centre peaks are a useful periodic-interference signal.
    dc_radius = max(2.0, min(h, w) * 0.04)
    candidate = power.copy()
    candidate[rr <= dc_radius] = 0.0
    flat = candidate.ravel()
    if flat.size < 32:
        return high, 0.0
    top = np.partition(flat, -min(12, flat.size))[-min(12, flat.size):]
    baseline = float(np.median(flat)) + 1e-9
    peak_ratio = float(np.mean(top) / baseline)
    periodic = float(np.clip((math.log10(max(1.0, peak_ratio)) - 2.0) / 3.0, 0.0, 1.0))
    return high, periodic


class AIFrameContext:
    """Immutable source frame plus lazy, cached algorithm-specific views."""

    def __init__(self, frame_rgb: np.ndarray, meta: Mapping[str, Any] | None = None):
        frame = np.asarray(frame_rgb)
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("AIFrameContext requires an HxWx3 uint8 RGB frame")
        self._original = frame
        self.meta: dict[str, Any] = dict(meta or {})
        self.height, self.width = frame.shape[:2]
        self._cache: dict[tuple[str, tuple[int, int, int, int], str], AIFrameView] = {}
        self._quality_cache: dict[tuple[int, int, int, int], FrameQuality] = {}
        self._lighting_quality_cache: dict[tuple[tuple[int, int, int, int], int], FrameQuality] = {}
        self._frequency_cache: dict[tuple[int, int, int, int], tuple[float, float]] = {}
        try:
            configured_quality_side = int(self.meta.get("quality_max_dimension", 512))
        except (TypeError, ValueError):
            configured_quality_side = 512
        self.quality_max_dimension = max(0, configured_quality_side)

    @property
    def original(self) -> np.ndarray:
        return self._original

    def quality(self, roi: Sequence[float] | None = None, *, include_frequency: bool = False) -> FrameQuality:
        rect = _clip_roi(roi, self.width, self.height)
        cached = self._quality_cache.get(rect)
        if cached is not None and (not include_frequency or rect in self._frequency_cache):
            if include_frequency and cached.high_frequency_ratio == 0.0 and cached.periodic_noise_score == 0.0:
                high, periodic = self._frequency_cache.get(rect, (0.0, 0.0))
                cached = FrameQuality(**{**cached.__dict__, "high_frequency_ratio": high, "periodic_noise_score": periodic})
            return cached
        x, y, w, h = rect
        crop = self._original[y : y + h, x : x + w]
        gray = _gray(crop)
        analysis_scale = 1.0
        if self.quality_max_dimension > 0 and max(gray.shape[:2], default=0) > self.quality_max_dimension:
            analysis_scale = float(self.quality_max_dimension) / float(max(gray.shape[:2]))
            analysis_size = (
                max(8, int(round(gray.shape[1] * analysis_scale))),
                max(8, int(round(gray.shape[0] * analysis_scale))),
            )
            analysis_gray = cv2.resize(gray, analysis_size, interpolation=cv2.INTER_AREA)
        else:
            analysis_gray = gray
        mean = float(analysis_gray.mean()) if analysis_gray.size else 0.0
        std = float(analysis_gray.std()) if analysis_gray.size else 0.0
        blur = float(cv2.Laplacian(analysis_gray, cv2.CV_64F).var()) if analysis_gray.size else 0.0
        shadows = float(np.mean(analysis_gray <= 32)) if analysis_gray.size else 0.0
        highlights = float(np.mean(analysis_gray >= 235)) if analysis_gray.size else 0.0
        edges = cv2.Canny(analysis_gray, 64, 160) if analysis_gray.size else np.empty((0, 0), dtype=np.uint8)
        edge_density = float(np.mean(edges > 0)) if edges.size else 0.0
        high = periodic = 0.0
        if include_frequency:
            high, periodic = self._frequency_cache.setdefault(rect, _frequency_metrics(gray))
        flags: list[str] = []
        if mean < 58.0 or shadows > 0.58:
            flags.append("low_light")
        if mean > 215.0 or highlights > 0.58:
            flags.append("overexposed")
        if std < 24.0:
            flags.append("low_contrast")
        # Blur threshold is deliberately conservative; content-specific gates
        # should be calibrated by model/dataset rather than hard-coded globally.
        if blur < 28.0 and edge_density < 0.08:
            flags.append("blur")
        if include_frequency and periodic >= 0.58:
            flags.append("periodic_noise")
        quality = FrameQuality(
            luma_mean=mean,
            luma_std=std,
            blur_variance=blur,
            shadow_fraction=shadows,
            highlight_fraction=highlights,
            edge_density=edge_density,
            high_frequency_ratio=high,
            periodic_noise_score=periodic,
            analysis_scale=float(analysis_scale),
            flags=tuple(flags),
        )
        self._quality_cache[rect] = quality
        return quality

    def lighting_quality(
        self,
        roi: Sequence[float] | None = None,
        *,
        max_dimension: int = 256,
    ) -> FrameQuality:
        """Return a low-cost lighting/contrast probe for adaptive color pipelines.

        Traffic-sign and similar color models only need stable low-light and
        low-contrast decisions.  Computing Laplacian, Canny, and frequency
        metrics for that decision adds avoidable latency, especially on 1080p
        video.  This probe uniformly subsamples before converting to luma and
        intentionally leaves blur/edge fields at zero.
        """

        rect = _clip_roi(roi, self.width, self.height)
        bounded_dimension = max(32, int(max_dimension))
        key = (rect, bounded_dimension)
        cached = self._lighting_quality_cache.get(key)
        if cached is not None:
            return cached

        x, y, w, h = rect
        crop = self._original[y : y + h, x : x + w]
        max_side = max(h, w)
        step = max(1, int(math.ceil(float(max_side) / float(bounded_dimension))))
        sampled = crop[::step, ::step]
        gray = _gray(sampled)
        analysis_scale = 1.0 / float(step)
        if max(gray.shape[:2], default=0) > bounded_dimension:
            scale = float(bounded_dimension) / float(max(gray.shape[:2]))
            gray = cv2.resize(
                gray,
                (
                    max(8, int(round(gray.shape[1] * scale))),
                    max(8, int(round(gray.shape[0] * scale))),
                ),
                interpolation=cv2.INTER_AREA,
            )
            analysis_scale *= scale

        mean = float(gray.mean()) if gray.size else 0.0
        std = float(gray.std()) if gray.size else 0.0
        shadows = float(np.mean(gray <= 32)) if gray.size else 0.0
        highlights = float(np.mean(gray >= 235)) if gray.size else 0.0
        flags: list[str] = []
        if mean < 58.0 or shadows > 0.58:
            flags.append("low_light")
        if mean > 215.0 or highlights > 0.58:
            flags.append("overexposed")
        if std < 24.0:
            flags.append("low_contrast")

        quality = FrameQuality(
            luma_mean=mean,
            luma_std=std,
            blur_variance=0.0,
            shadow_fraction=shadows,
            highlight_fraction=highlights,
            edge_density=0.0,
            analysis_scale=float(analysis_scale),
            flags=tuple(flags),
        )
        self._lighting_quality_cache[key] = quality
        return quality

    def frequency_probe(self, roi: Sequence[float] | None = None) -> dict[str, float | bool]:
        quality = self.quality(roi, include_frequency=True)
        return {
            "high_frequency_ratio": quality.high_frequency_ratio,
            "periodic_noise_score": quality.periodic_noise_score,
            "periodic_noise_detected": "periodic_noise" in quality.flags,
        }

    def get_view(
        self,
        profile: AIPreprocessProfile | str = AIPreprocessProfile.MODEL_NATIVE,
        roi: Sequence[float] | None = None,
        *,
        variant: str = "default",
        quality_override: FrameQuality | None = None,
    ) -> AIFrameView:
        profile_value = profile.value if isinstance(profile, AIPreprocessProfile) else str(profile)
        rect = _clip_roi(roi, self.width, self.height)
        key = (profile_value, rect, str(variant))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        x, y, w, h = rect
        crop = self._original[y : y + h, x : x + w]
        source_to_view = np.asarray([[1.0, 0.0, -float(x)], [0.0, 1.0, -float(y)], [0.0, 0.0, 1.0]], dtype=np.float64)
        view_to_source = np.linalg.inv(source_to_view)
        quality_profiles = {
            AIPreprocessProfile.OCR_RECOVERY.value,
            AIPreprocessProfile.BARCODE_RECOVERY.value,
            AIPreprocessProfile.TRAFFIC_SIGN_LOW_LIGHT.value,
            AIPreprocessProfile.TRACKING_STABLE.value,
            AIPreprocessProfile.FREQUENCY_PROBE.value,
            AIPreprocessProfile.FREQUENCY_RESTORE_ROI.value,
        }
        quality = (
            quality_override
            if profile_value in quality_profiles and quality_override is not None
            else (
                self.quality(
                    rect,
                    include_frequency=profile_value in {
                        AIPreprocessProfile.FREQUENCY_PROBE.value,
                        AIPreprocessProfile.FREQUENCY_RESTORE_ROI.value,
                    },
                )
                if profile_value in quality_profiles
                else None
            )
        )
        operations: list[str] = []
        enhanced = False

        if profile_value in {
            AIPreprocessProfile.MODEL_NATIVE.value,
            AIPreprocessProfile.OCR_NATIVE.value,
            AIPreprocessProfile.TRAFFIC_SIGN_NATIVE.value,
            AIPreprocessProfile.REID_STABLE.value,
        }:
            out = crop
        elif profile_value in {AIPreprocessProfile.FAST_LUMA.value, AIPreprocessProfile.BARCODE_NATIVE.value}:
            out = _gray(crop)
            operations.append("rgb_to_luma")
        elif profile_value == AIPreprocessProfile.OCR_RECOVERY.value:
            assert quality is not None
            gray = _gray(crop)
            operations.append("rgb_to_luma")
            if "low_light" in quality.flags:
                gray = cv2.LUT(gray, _gamma_lut(quality.luma_mean, 112.0))
                operations.append("gamma")
            clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
            gray = clahe.apply(gray)
            operations.append("clahe")
            if quality.shadow_fraction < 0.85 and quality.edge_density > 0.01:
                gray = cv2.bilateralFilter(gray, 5, 35, 35)
                operations.append("bilateral_denoise")
            if "blur" in quality.flags or quality.blur_variance < 65.0:
                gray = _unsharp(gray, sigma=1.1, amount=0.55)
                operations.append("unsharp")
            out = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
            enhanced = True
        elif profile_value == AIPreprocessProfile.BARCODE_RECOVERY.value:
            assert quality is not None
            gray = _gray(crop)
            operations.append("rgb_to_luma")
            mode = str(variant or "contrast").lower()
            if mode == "contrast":
                out = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
                operations.append("clahe")
            elif mode in {"threshold", "inverted"}:
                normalized = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8)).apply(gray)
                block = max(15, (min(gray.shape[:2]) // 12) | 1)
                block = min(block, 61)
                if block % 2 == 0:
                    block += 1
                out = cv2.adaptiveThreshold(normalized, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, 5)
                operations.extend(("clahe", "adaptive_threshold"))
                if mode == "inverted":
                    out = cv2.bitwise_not(out)
                    operations.append("invert")
            elif mode == "sharpen":
                out = _unsharp(gray, sigma=1.0, amount=0.8)
                operations.append("unsharp")
            elif mode == "frequency":
                out = frequency_restore_luma(gray)
                operations.append("fft_high_boost")
            else:
                raise ValueError(f"unknown barcode recovery variant: {variant!r}")
            enhanced = True
        elif profile_value == AIPreprocessProfile.TRAFFIC_SIGN_LOW_LIGHT.value:
            assert quality is not None
            rgb = crop.copy()
            if "low_light" in quality.flags:
                rgb = cv2.LUT(rgb, _gamma_lut(quality.luma_mean, 105.0))
                operations.append("gamma")
            lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
            l, a, b = cv2.split(lab)
            l = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8)).apply(l)
            out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2RGB)
            operations.extend(("lab_luma", "clahe"))
            enhanced = True
        elif profile_value == AIPreprocessProfile.TRACKING_STABLE.value:
            assert quality is not None
            if "low_light" in quality.flags or "low_contrast" in quality.flags:
                rgb = crop.copy()
                if "low_light" in quality.flags:
                    rgb = cv2.LUT(rgb, _gamma_lut(quality.luma_mean, 100.0))
                    operations.append("fixed_gamma")
                lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
                l, a, b = cv2.split(lab)
                l = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(l)
                out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2RGB)
                operations.extend(("lab_luma", "mild_clahe"))
                enhanced = True
            else:
                out = crop
        elif profile_value == AIPreprocessProfile.FREQUENCY_PROBE.value:
            out = _gray(crop)
            operations.append("fft_probe_no_ifft")
        elif profile_value == AIPreprocessProfile.FREQUENCY_RESTORE_ROI.value:
            gray = frequency_restore_luma(_gray(crop))
            out = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
            operations.extend(("rgb_to_luma", "fft_high_boost", "ifft", "gray_to_rgb"))
            enhanced = True
        else:
            raise ValueError(f"unsupported AI preprocess profile: {profile_value!r}")

        view = AIFrameView(
            frame=np.ascontiguousarray(out) if not out.flags.c_contiguous else out,
            profile=profile_value,
            source_to_view=source_to_view,
            view_to_source=view_to_source,
            source_roi=rect,
            enhanced=enhanced,
            operations=tuple(operations),
            quality=quality,
        )
        self._cache[key] = view
        return view

    def barcode_views(
        self,
        roi: Sequence[float] | None = None,
        *,
        max_variants: int = 5,
        allow_frequency: bool = True,
        max_frequency_pixels: int = 262_144,
    ) -> Iterator[AIFrameView]:
        yield self.get_view(AIPreprocessProfile.BARCODE_NATIVE, roi)
        rect = _clip_roi(roi, self.width, self.height)
        frequency_allowed_for_size = rect[2] * rect[3] <= max(0, int(max_frequency_pixels))
        probe_frequency = bool(allow_frequency and frequency_allowed_for_size)
        quality = self.quality(roi, include_frequency=probe_frequency)
        variants: list[str] = ["contrast", "threshold", "inverted"]
        if "blur" in quality.flags:
            variants.insert(1, "sharpen")
        if probe_frequency and "periodic_noise" in quality.flags:
            variants.append("frequency")
        emitted = 1
        for variant in variants:
            if emitted >= max(1, int(max_variants)):
                break
            yield self.get_view(AIPreprocessProfile.BARCODE_RECOVERY, roi, variant=variant)
            emitted += 1


def ensure_frame_context(frame_rgb: np.ndarray, meta: Mapping[str, Any] | None = None) -> AIFrameContext:
    if isinstance(meta, Mapping):
        existing = meta.get("_ai_frame_context")
        if isinstance(existing, AIFrameContext) and existing.original is frame_rgb:
            return existing
    return AIFrameContext(frame_rgb, meta)


__all__ = [
    "AIPreprocessProfile",
    "AIFrameContext",
    "AIFrameView",
    "FrameQuality",
    "StableConditionController",
    "ensure_frame_context",
    "frequency_restore_luma",
    "phase_correlate_translation",
]
