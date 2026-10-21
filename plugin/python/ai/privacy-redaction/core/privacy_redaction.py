"""Pixel compositor helpers for media-format privacy transforms."""

from __future__ import annotations

from typing import Iterable

import cv2
import numpy as np

from .geometry import clip_bbox_xywh

BBoxXYWH = tuple[float, float, float, float]


def expand_privacy_box(
    box: BBoxXYWH,
    *,
    kind: str,
    width: int,
    height: int,
    face_expand_w: float = 1.35,
    face_expand_h: float = 1.55,
    plate_expand_w: float = 1.18,
    plate_expand_h: float = 1.35,
) -> tuple[int, int, int, int] | None:
    x, y, w, h = [float(value) for value in box]
    kind_value = str(kind)
    if kind_value in {"face", "head"}:
        scale_w, scale_h = face_expand_w, face_expand_h
        # Bias upward to include hair/forehead while retaining chin.
        cx = x + w * 0.5
        cy = y + h * 0.45
    else:
        scale_w, scale_h = plate_expand_w, plate_expand_h
        cx = x + w * 0.5
        cy = y + h * 0.5
    expanded = (
        cx - w * scale_w * 0.5,
        cy - h * scale_h * 0.5,
        w * scale_w,
        h * scale_h,
    )
    clipped = clip_bbox_xywh(expanded, width, height, min_size=1.0)
    if clipped is None:
        return None
    ex, ey, ew, eh = clipped
    return int(round(ex)), int(round(ey)), max(1, int(round(ew))), max(1, int(round(eh)))


def apply_privacy_mask(
    frame_rgb: np.ndarray,
    rect: tuple[int, int, int, int],
    *,
    mode: str,
    pixel_block: int = 12,
    solid_color: tuple[int, int, int] = (0, 0, 0),
) -> None:
    x, y, w, h = rect
    roi = frame_rgb[y : y + h, x : x + w]
    if roi.size == 0:
        return
    mode_value = str(mode).lower()
    if mode_value == "solid":
        roi[...] = np.asarray(solid_color, dtype=np.uint8)
        return
    if mode_value == "blur":
        minimum = max(7, min(w, h) // 2)
        kernel = minimum | 1
        max_kernel = min(w if w % 2 else w - 1, h if h % 2 else h - 1)
        kernel = max(3, min(kernel, max_kernel))
        if kernel >= 3:
            roi[...] = cv2.GaussianBlur(roi, (kernel, kernel), 0)
        else:
            roi[...] = 0
        return
    if mode_value != "pixelate":
        raise ValueError("privacy mode must be pixelate, blur, or solid")
    block = max(4, int(pixel_block))
    small_w = max(1, int(round(w / block)))
    small_h = max(1, int(round(h / block)))
    small = cv2.resize(roi, (small_w, small_h), interpolation=cv2.INTER_AREA)
    roi[...] = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)


def redact_frame(
    frame_rgb: np.ndarray,
    regions: Iterable[tuple[str, BBoxXYWH]],
    *,
    mode: str,
    pixel_block: int = 12,
    face_expand_w: float = 1.35,
    face_expand_h: float = 1.55,
    plate_expand_w: float = 1.18,
    plate_expand_h: float = 1.35,
) -> tuple[np.ndarray, list[tuple[str, tuple[int, int, int, int]]]]:
    out = frame_rgb.copy()
    height, width = out.shape[:2]
    applied: list[tuple[str, tuple[int, int, int, int]]] = []
    for kind, box in regions:
        rect = expand_privacy_box(
            box,
            kind=kind,
            width=width,
            height=height,
            face_expand_w=face_expand_w,
            face_expand_h=face_expand_h,
            plate_expand_w=plate_expand_w,
            plate_expand_h=plate_expand_h,
        )
        if rect is None:
            continue
        apply_privacy_mask(out, rect, mode=mode, pixel_block=pixel_block)
        applied.append((kind, rect))
    return out, applied
