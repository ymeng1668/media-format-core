"""Geometry helpers shared by Python AI processors.

All helpers are deliberately total for finite numeric input and clip model or
preprocessing output before it is passed to the strict media-format result contract.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

import cv2
import numpy as np


def clip_bbox_xyxy(box: Iterable[float], width: int, height: int) -> tuple[float, float, float, float]:
    values = [float(v) for v in box]
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError("xyxy box must contain four finite values")
    x1, y1, x2, y2 = values
    max_x = max(0.0, float(width))
    max_y = max(0.0, float(height))
    x1 = max(0.0, min(max_x, x1))
    y1 = max(0.0, min(max_y, y1))
    x2 = max(0.0, min(max_x, x2))
    y2 = max(0.0, min(max_y, y2))
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def xyxy_to_xywh(box: Iterable[float]) -> tuple[float, float, float, float]:
    values = [float(v) for v in box]
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError("xyxy box must contain four finite values")
    x1, y1, x2, y2 = values
    return x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1)


def clip_bbox_xywh(
    box: Iterable[float],
    width: int,
    height: int,
    *,
    min_size: float = 0.0,
) -> tuple[float, float, float, float] | None:
    """Clip ``x,y,w,h`` to an image and return ``None`` for an empty box."""

    values = [float(v) for v in box]
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        return None
    x, y, w, h = values
    if w < 0.0:
        x, w = x + w, -w
    if h < 0.0:
        y, h = y + h, -h
    x1, y1, x2, y2 = clip_bbox_xyxy((x, y, x + w, y + h), width, height)
    clipped = xyxy_to_xywh((x1, y1, x2, y2))
    if clipped[2] <= float(min_size) or clipped[3] <= float(min_size):
        return None
    return clipped


def clip_polygon_to_frame(
    polygon: Iterable[Sequence[float]],
    width: int,
    height: int,
    *,
    min_points: int = 2,
) -> list[list[float]]:
    """Clip finite polygon points to valid source-image coordinates.

    OpenCV and perspective transforms can return tiny negative values such as
    ``-1e-5`` for a point that mathematically lies on the image border.  The
    strict result contract correctly rejects negative pixel coordinates, so
    processors must clip before constructing events.
    """

    max_x = max(0.0, float(width - 1))
    max_y = max(0.0, float(height - 1))
    output: list[list[float]] = []
    for point in polygon:
        if len(point) < 2:
            continue
        try:
            x, y = float(point[0]), float(point[1])
        except Exception:
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        output.append([max(0.0, min(max_x, x)), max(0.0, min(max_y, y))])
    return output if len(output) >= max(1, int(min_points)) else []


def polygon_bbox_xywh(
    polygon: Iterable[Sequence[float]],
    width: int,
    height: int,
    *,
    min_size: float = 0.0,
) -> tuple[float, float, float, float] | None:
    points = clip_polygon_to_frame(polygon, width, height, min_points=2)
    if not points:
        return None
    arr = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    x, y, w, h = [float(v) for v in cv2.boundingRect(arr)]
    return clip_bbox_xywh((x, y, w, h), width, height, min_size=min_size)


def bbox_iou_xywh(a: Iterable[float], b: Iterable[float]) -> float:
    ax, ay, aw, ah = [float(v) for v in a]
    bx, by, bw, bh = [float(v) for v in b]
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    inter_w = max(0.0, min(ax2, bx2) - max(ax, bx))
    inter_h = max(0.0, min(ay2, by2) - max(ay, by))
    inter = inter_w * inter_h
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0.0 else 0.0


def nms_xywh(boxes: np.ndarray, scores: np.ndarray, iou_thresh: float = 0.5) -> list[int]:
    if len(boxes) == 0:
        return []
    xyxy = boxes.astype(float).copy()
    xyxy[:, 2] = xyxy[:, 0] + xyxy[:, 2]
    xyxy[:, 3] = xyxy[:, 1] + xyxy[:, 3]
    order = scores.astype(float).argsort()[::-1]
    keep: list[int] = []
    while len(order):
        i = int(order[0])
        keep.append(i)
        if len(order) == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(xyxy[i, 0], xyxy[rest, 0])
        yy1 = np.maximum(xyxy[i, 1], xyxy[rest, 1])
        xx2 = np.minimum(xyxy[i, 2], xyxy[rest, 2])
        yy2 = np.minimum(xyxy[i, 3], xyxy[rest, 3])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        area_i = max(0.0, (xyxy[i, 2] - xyxy[i, 0]) * (xyxy[i, 3] - xyxy[i, 1]))
        area_r = np.maximum(0.0, xyxy[rest, 2] - xyxy[rest, 0]) * np.maximum(0.0, xyxy[rest, 3] - xyxy[rest, 1])
        iou = inter / np.maximum(area_i + area_r - inter, 1e-6)
        order = rest[iou < float(iou_thresh)]
    return keep


__all__ = [
    "bbox_iou_xywh",
    "clip_bbox_xywh",
    "clip_bbox_xyxy",
    "clip_polygon_to_frame",
    "nms_xywh",
    "polygon_bbox_xywh",
    "xyxy_to_xywh",
]
