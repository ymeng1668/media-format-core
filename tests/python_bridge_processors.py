"""Deterministic, model-free processors for the native Python bridge tests."""

import threading
import numpy as np


class Probe:
    def __init__(self, value=17, hold=False):
        self.value = value
        self.hold = hold
        self.frame = None
        self.count = 0
        self.thread = threading.get_ident()

    def process(self, frame, metadata):
        assert threading.get_ident() == self.thread
        self.count += 1
        frame[0, 0] = self.value
        if self.hold:
            self.frame = frame
        return {"valid": True, "degraded": False, "frame_index": metadata["frame_index"],
                "pts_us": metadata["pts_us"], "address": frame.__array_interface__["data"][0],
                "count": self.count, "time": metadata["time"], "tag": metadata.get("tag", "")}

    def reset(self):
        self.count = 0
        self.frame = None

    def close(self):
        self.frame = None


class Raises:
    def process(self, frame, metadata):
        raise RuntimeError("intentional processor exception")


class Invalid:
    def process(self, frame, metadata):
        return {"valid": False, "reason": "intentional invalid result"}


class WrongShape:
    def process(self, frame, metadata):
        return {"valid": True, "frame": np.zeros((1, 1, 3), dtype=np.uint8)}


class LargeResult:
    def process(self, frame, metadata):
        return {"valid": True, "payload": "x" * 4096}


class Replacement:
    def process(self, frame, metadata):
        return {"valid": True, "frame": np.full_like(frame, 37)}
