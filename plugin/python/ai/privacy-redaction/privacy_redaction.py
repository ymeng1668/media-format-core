"""Face/plate privacy entry point for native and Python hosts.

Inference uses explicitly installed local models; see README.md for setup.
"""

import numpy as np

from ai_privacy_redaction import PrivacyRedactionProcessor as _PrivacyProcessor
from core.privacy_presets import load_privacy_config
from core.privacy_highlight import draw_highlights


class PrivacyRedactionProcessor(_PrivacyProcessor):
    def __init__(self, **config):
        defaults = {
            "preset": "balanced", "mode": "highlight",
            "automatic_detection_required": True,
            "plate_mask_policy": "confirmed_detected", "plate_ocr_enabled": True,
            "emit_plate_text": False, "save_recognized_plates": False,
            "emit_plate_candidates": False,
        }
        file_config, _ = load_privacy_config(config.get("config_path"))
        effective = {**defaults, **file_config, **config}
        effective.setdefault("apply_redaction", effective["mode"] != "highlight")
        super().__init__(**effective)

    def process(self, frame_rgb, metadata=None):
        if not isinstance(frame_rgb, np.ndarray) or frame_rgb.dtype != np.uint8 or frame_rgb.ndim != 3 or frame_rgb.shape[2] != 3:
            raise ValueError("Privacy input must be an RGB24 uint8 image")
        result = super().process(frame_rgb, metadata)
        draw_highlights(result["frame"], result.get("events", []))
        return result
