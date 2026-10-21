"""Private helpers shared by lightweight Python AI processors."""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core import bbox, event, frame_result, process_meta


def _gray(frame_rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)


def _quality(frame_rgb: np.ndarray) -> dict[str, float]:
    g = _gray(frame_rgb)
    return {
        "luma": float(g.mean()),
        "contrast": float(g.std()),
        "blur": float(cv2.Laplacian(g, cv2.CV_64F).var()),
    }


def _result(algorithm: str, pts: float, idx: int, events: list[dict[str, Any]], metrics: dict[str, Any] | None = None, *, valid: bool = True, reason: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return frame_result(
        algorithm=algorithm,
        time=pts,
        frame_index=idx,
        processed=True,
        valid=valid,
        reason=reason,
        events=events,
        metrics=metrics or {},
        extra=extra,
    )

__all__ = [
    'math',
    'os',
    're',
    'subprocess',
    'tempfile',
    'wave',
    'Path',
    'Any',
    'cv2',
    'np',
    'bbox',
    'event',
    'frame_result',
    'process_meta',
    '_gray',
    '_quality',
    '_result',
]
