"""Generic synchronous frame dispatch for media-format-core's CPython bridge.

This module contains no detector or model registry. Each
host owns one Python-created worker so native ML objects are created, used and
released on a Python thread. The NumPy view retains the C++ allocation owner.
"""

from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import threading

import numpy as np


class _FrameLease:
    def __init__(self, address, width, height, stride, owner):
        self.owner = owner
        self.__array_interface__ = {
            "shape": (height, width, 3), "typestr": "|u1",
            "data": (address, False), "strides": (stride, 3, 1), "version": 3,
        }


def _json_default(value):
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Non-JSON processor result: {type(value).__name__}")


class FrameProcessorHost:
    def __init__(self, module, class_name, config_json):
        config = json.loads(config_json)
        if not isinstance(config, dict):
            raise ValueError("Processor configuration must be a JSON object")
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-format-python")
        self._processor = None
        self._closed = False
        self._thread = None
        try:
            self._worker.submit(self._open, module, class_name, config).result()
        except BaseException:
            self._worker.shutdown(wait=True, cancel_futures=True)
            raise

    def _open(self, module, class_name, config):
        self._thread = threading.get_ident()
        cls = getattr(importlib.import_module(module), class_name)
        self._processor = cls(**config)
        if not callable(getattr(self._processor, "process", None)):
            self._release()
            raise TypeError("Processor must implement process(rgb_array, metadata)")

    def process(self, address, width, height, stride, owner, pts_us, frame_index, metadata_json):
        if self._closed:
            raise RuntimeError("Processor host is closed")
        return self._worker.submit(self._process, address, width, height, stride,
                                   owner, pts_us, frame_index, metadata_json).result()

    def _process(self, address, width, height, stride, owner, pts_us, frame_index, metadata_json):
        metadata = json.loads(metadata_json)
        if not isinstance(metadata, dict):
            raise ValueError("Frame metadata must be a JSON object")
        metadata.update(time=pts_us / 1_000_000.0, pts_us=pts_us, frame_index=frame_index,
                        width=width, height=height, pixel_format="rgb24")
        frame = np.asarray(_FrameLease(address, width, height, stride, owner))
        result = self._processor.process(frame, metadata)
        if not isinstance(result, dict) or not isinstance(result.get("valid"), bool):
            raise TypeError("Processor must return a dictionary with Boolean valid")
        # Validate the complete result before copying/returning success. Invalid
        # metadata must not leave a caller believing that unprocessed video is safe.
        result = dict(result)
        for key in ("degraded", "blocked"):
            if key in result and not isinstance(result[key], bool):
                raise TypeError(f"Processor result {key} must be Boolean")
        output = result.pop("frame", None)
        blocked = bool(result.get("blocked", False)) or any(
            event.get("label") == "privacy_frame_blocked"
            for event in result.get("events", []) if isinstance(event, dict))
        result["blocked"] = blocked
        serialized = json.dumps(result, default=_json_default, allow_nan=False, separators=(",", ":"))
        if output is not None:
            if not isinstance(output, np.ndarray) or output.dtype != np.uint8 or output.shape != frame.shape:
                raise ValueError("Output frame must be uint8 RGB with unchanged dimensions")
            same_view = (output.__array_interface__["data"][0] == frame.__array_interface__["data"][0]
                         and output.strides == frame.strides)
            if not same_view:
                np.copyto(frame, output)
        return serialized

    def reset(self):
        if self._closed:
            raise RuntimeError("Processor host is closed")
        self._worker.submit(self._reset).result()

    def _reset(self):
        reset = getattr(self._processor, "reset", None)
        if callable(reset):
            reset()

    def _release(self):
        try:
            close = getattr(self._processor, "close", None)
            if callable(close):
                close()
        finally:
            self._processor = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._worker.submit(self._release).result()
        finally:
            self._worker.shutdown(wait=True, cancel_futures=True)
