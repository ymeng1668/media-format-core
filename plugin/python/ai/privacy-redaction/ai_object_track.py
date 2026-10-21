# ai_object_track.py - object tracking processor.
#
# Summary:
#   Detects objects and maintains track identifiers across sampled frames.
#
# Design:
#   Provides object-track metadata for video analysis exports.
#
# Functionality:
#   Runs object detection/tracking, updates track state, and emits boxes, labels, confidences, and
#   ids.
#
# Options:
#   Uses arguments supplied by the caller or parent CLI; this module does not add another top-level
#   parser.
#
# Verification:
#   Compile-check this file and smoke the selected processor through `python3
#   plugins/ai/main.py <media> --filter <filter>`.

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any, Union

import contextlib
import cv2, numpy as np, gc

from core import (
    AIPreprocessProfile, StableConditionController, bbox, ensure_frame_context,
    event, normalize_frame_result, process_meta,
)
from core.geometry import clip_bbox_xywh, nms_xywh
from core.model_paths import resolve_model_ref

try:
    import torch
    # ensure no autograd allocations globally
    try:
        torch.set_grad_enabled(False)
    except Exception:
        pass
except Exception:
    torch = None

try:
    from ultralytics import YOLO  # pip install ultralytics
except Exception:
    YOLO = None

try:
    import onnxruntime as ort
except Exception:
    ort = None


# ---------------------------- simple IOU tracker -----------------------------

@dataclass
class Track:
    tid: int
    xywh: np.ndarray  # (x,y,w,h) in pixels
    cls: int
    conf: float
    miss: int = 0
    hits: int = 0
    age: int = 1
    state: str = "tentative"
    vx: float = 0.0
    vy: float = 0.0


@dataclass
class Detection:
    xywh: np.ndarray
    conf: float
    cls: int


class BaseDetector:
    backend = "base"

    def detect(self, frame_bgr: np.ndarray) -> tuple[list[Detection], Any]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class UltralyticsDetector(BaseDetector):
    backend = "ultralytics"

    def __init__(self, model: str, device: str, conf: float, iou: float, imgsz: int, classes: Optional[List[int]], ctx_factory):
        if YOLO is None:
            raise RuntimeError("ultralytics not installed: pip install ultralytics")
        self.model_id = model
        self.model_ref = resolve_model_ref(model)
        self.device = device
        self.conf = float(conf)
        self.iou = float(iou)
        self.imgsz = int(imgsz)
        self.classes = classes
        self._ctx_factory = ctx_factory
        self.yolo = YOLO(self.model_ref)
        try:
            self.yolo.to(device)
        except Exception:
            pass
        names = getattr(getattr(self.yolo, "model", self.yolo), "names", None)
        self.names = names if names is not None else {i: f"class_{i}" for i in range(1000)}

    def detect(self, frame_bgr: np.ndarray) -> tuple[list[Detection], Any]:
        r = None
        with self._ctx_factory():
            with contextlib.closing(self.yolo.predict(
                source=frame_bgr, stream=True,
                save=False, verbose=False,
                conf=self.conf, iou=self.iou, imgsz=self.imgsz,
                device=self.device,
                **({"classes": self.classes} if self.classes is not None else {})
            )) as gen:
                try:
                    r = next(gen)
                except StopIteration:
                    r = None

        dets: list[Detection] = []
        if r is None:
            return dets, r

        boxes = getattr(r, "boxes", None)
        if boxes is not None and len(boxes) > 0:
            xyxy_t = getattr(boxes, "xyxy", None)
            conf_t = getattr(boxes, "conf", None)
            cls_t = getattr(boxes, "cls", None)
            if hasattr(xyxy_t, "detach"):
                xyxy = xyxy_t.detach().cpu().numpy()
                conf = conf_t.detach().cpu().numpy()
                cls = cls_t.detach().cpu().numpy().astype(np.int32)
            else:
                xyxy = np.asarray(xyxy_t)
                conf = np.asarray(conf_t)
                cls = np.asarray(cls_t, dtype=np.int32)
            if self.classes is not None:
                keep = np.array([c in self.classes for c in cls], dtype=bool)
                xyxy, conf, cls = xyxy[keep], conf[keep], cls[keep]
            wh = xyxy[:, 2:4] - xyxy[:, 0:2]
            xy = xyxy[:, 0:2]
            xywh = np.concatenate([xy, wh], axis=1)
            for i in range(xywh.shape[0]):
                dets.append(Detection(xywh[i].astype(float), float(conf[i]), int(cls[i])))
        return dets, r

    def cleanup_predictor(self, drop_predictor: bool = True) -> None:
        pred = None
        try:
            pred = getattr(self.yolo, "_predictor", None)
            if pred is None:
                state = getattr(self.yolo, "__dict__", None)
                if isinstance(state, dict):
                    pred = state.get("predictor")
        except Exception:
            pred = None
        if pred is None:
            return

        def _try_call(obj: Any, method: str) -> None:
            fn = getattr(obj, method, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

        try:
            if hasattr(pred, "results") and isinstance(pred.results, list):
                pred.results.clear()
            _try_call(getattr(pred, "dataset", None), "close")
            _try_call(getattr(pred, "dataloader", None), "close")
            _try_call(getattr(pred, "dataloader", None), "__exit__")
            for attr in (
                "batch", "im", "orig_img", "dataset", "dataloader",
                "vid_writer", "vid_cap", "save_dir", "loader",
                "model", "callbacks", "args", "data", "bs",
                "visualizer", "metrics", "timer"
            ):
                if hasattr(pred, attr):
                    setattr(pred, attr, None)
            if hasattr(pred, "tracker") and pred.tracker is not None:
                try:
                    pred.tracker.reset()
                except Exception:
                    pass
                pred.tracker = None
        except Exception:
            pass

        if drop_predictor:
            try:
                if hasattr(self.yolo, "_predictor"):
                    self.yolo._predictor = None  # type: ignore[attr-defined]
                state = getattr(self.yolo, "__dict__", None)
                if isinstance(state, dict) and state.get("predictor") is not None:
                    state["predictor"] = None
            except Exception:
                pass

    def close(self) -> None:
        self.cleanup_predictor()
        try:
            del self.yolo
        except Exception:
            self.yolo = None  # type: ignore[assignment]


class ONNXRuntimeDetector(BaseDetector):
    backend = "onnxruntime"

    def __init__(self, model: str, conf: float, iou: float, imgsz: int, classes: Optional[List[int]], device: str = "cpu"):
        if ort is None:
            raise RuntimeError("onnxruntime not installed: pip install onnxruntime")
        self.model_id = model
        self.model_ref = resolve_model_ref(model)
        self.conf = float(conf)
        self.iou = float(iou)
        self.imgsz = int(imgsz)
        self.classes = classes
        requested = ["CUDAExecutionProvider", "CPUExecutionProvider"] if str(device).startswith("cuda") else ["CPUExecutionProvider"]
        available = set(ort.get_available_providers())
        providers = [provider for provider in requested if provider in available]
        if not providers:
            raise RuntimeError(f"no requested ONNX Runtime provider available; requested={requested}, available={sorted(available)}")
        self.session = ort.InferenceSession(self.model_ref, providers=providers)
        self.providers = list(self.session.get_providers())
        self.input_name = self.session.get_inputs()[0].name
        self.names = {i: f"class_{i}" for i in range(1000)}

    def detect(self, frame_bgr: np.ndarray) -> tuple[list[Detection], Any]:
        h0, w0 = frame_bgr.shape[:2]
        scale = min(float(self.imgsz) / max(w0, 1), float(self.imgsz) / max(h0, 1))
        new_w, new_h = max(1, int(round(w0 * scale))), max(1, int(round(h0 * scale)))
        resized = cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
        pad_x = (self.imgsz - new_w) // 2
        pad_y = (self.imgsz - new_h) // 2
        canvas = np.full((self.imgsz, self.imgsz, 3), 114, dtype=np.uint8)
        canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
        inp = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        inp = np.transpose(inp, (2, 0, 1))[None, ...]
        outputs = self.session.run(None, {self.input_name: inp})
        out = outputs[0]
        pred = np.asarray(out)
        pred = np.squeeze(pred)
        if pred.ndim == 2 and pred.shape[0] < pred.shape[1] and pred.shape[0] in (6, 84, 85):
            pred = pred.T
        if pred.ndim != 2 or pred.shape[1] < 6:
            return [], outputs

        if pred.shape[1] == 6:
            # Common end-to-end NMS export: x1,y1,x2,y2,confidence,class.
            xyxy = pred[:, :4].astype(np.float32)
            conf = pred[:, 4].astype(np.float32)
            cls = pred[:, 5].astype(np.int32)
            xywh = np.empty_like(xyxy)
            xywh[:, 0:2] = xyxy[:, 0:2]
            xywh[:, 2:4] = xyxy[:, 2:4] - xyxy[:, 0:2]
        else:
            # Raw YOLOv5/v8 export: cx,cy,w,h,[objectness],class scores.
            boxes = pred[:, :4].astype(np.float32)
            if pred.shape[1] >= 85:
                obj = pred[:, 4].astype(np.float32)
                scores = pred[:, 5:].astype(np.float32)
            else:
                obj = np.ones((pred.shape[0],), dtype=np.float32)
                scores = pred[:, 4:].astype(np.float32)
            cls = np.argmax(scores, axis=1).astype(np.int32)
            conf = obj * scores[np.arange(scores.shape[0]), cls]
            xywh = boxes.copy()
            xywh[:, 0] = boxes[:, 0] - boxes[:, 2] * 0.5
            xywh[:, 1] = boxes[:, 1] - boxes[:, 3] * 0.5

        xywh[:, 0] = (xywh[:, 0] - pad_x) / max(scale, 1e-9)
        xywh[:, 1] = (xywh[:, 1] - pad_y) / max(scale, 1e-9)
        xywh[:, 2] /= max(scale, 1e-9)
        xywh[:, 3] /= max(scale, 1e-9)
        xywh[:, 0] = np.clip(xywh[:, 0], 0, max(0, w0 - 1))
        xywh[:, 1] = np.clip(xywh[:, 1], 0, max(0, h0 - 1))
        xywh[:, 2] = np.clip(xywh[:, 2], 0, w0 - xywh[:, 0])
        xywh[:, 3] = np.clip(xywh[:, 3], 0, h0 - xywh[:, 1])

        keep = np.isfinite(conf) & (conf >= self.conf) & (xywh[:, 2] > 0) & (xywh[:, 3] > 0)
        if self.classes is not None:
            keep &= np.array([int(c) in self.classes for c in cls], dtype=bool)
        xywh, conf, cls = xywh[keep], conf[keep], cls[keep]
        keep_idx = nms_xywh(xywh, conf, self.iou)
        return [Detection(xywh[i].astype(float), float(conf[i]), int(cls[i])) for i in keep_idx], outputs

    def close(self) -> None:
        self.session = None


class NaiveIOUTracker:
    def __init__(self, iou_match: float = 0.3, max_age: int = 30, min_hits: int = 3):
        self.iou_match = float(np.clip(iou_match, 0.0, 1.0))
        self.max_age = max(0, int(max_age))
        self.min_hits = max(1, int(min_hits))
        self.tracks: List[Track] = []
        self.next_id = 1

    @staticmethod
    def _iou(a: np.ndarray, b: np.ndarray) -> float:
        ax, ay, aw, ah = [float(v) for v in a]
        bx, by, bw, bh = [float(v) for v in b]
        ax2, ay2 = ax + aw, ay + ah
        bx2, by2 = bx + bw, by + bh
        inter_w = max(0.0, min(ax2, bx2) - max(ax, bx))
        inter_h = max(0.0, min(ay2, by2) - max(ay, by))
        inter = inter_w * inter_h
        union = aw * ah + bw * bh - inter
        return inter / union if union > 0.0 else 0.0

    @staticmethod
    def _predicted_xywh(track: Track) -> np.ndarray:
        predicted = track.xywh.astype(float, copy=True)
        predicted[0] += float(track.vx)
        predicted[1] += float(track.vy)
        return predicted

    @staticmethod
    def _apply_detection(track: Track, detection: Detection, *, low_confidence: bool = False) -> None:
        old_cx = float(track.xywh[0] + track.xywh[2] * 0.5)
        old_cy = float(track.xywh[1] + track.xywh[3] * 0.5)
        new_cx = float(detection.xywh[0] + detection.xywh[2] * 0.5)
        new_cy = float(detection.xywh[1] + detection.xywh[3] * 0.5)
        # A detector observation should dominate the prediction; modest
        # smoothing removes jitter without creating the large lag of V3's 0.3
        # observation weight.
        track.xywh = 0.35 * track.xywh + 0.65 * detection.xywh
        track.vx = 0.65 * track.vx + 0.35 * (new_cx - old_cx)
        track.vy = 0.65 * track.vy + 0.35 * (new_cy - old_cy)
        if low_confidence:
            # ByteTrack's second association stage exists to bridge weak
            # detections.  Do not hide a healthy track merely because the
            # current observation is below the public output threshold.
            track.conf = float(max(detection.conf, track.conf * 0.97))
        else:
            track.conf = float(detection.conf)
        track.cls = int(detection.cls)
        track.miss = 0
        track.hits += 1
        track.state = "tracked" if track.hits >= 1 else "tentative"

    def update(self, dets: List[Detection]) -> List[Track]:
        existing_count = len(self.tracks)
        for track in self.tracks:
            track.miss += 1
            track.age += 1
            track.state = "predicted" if track.miss <= self.max_age else "lost"

        pairs: list[tuple[float, int, int]] = []
        for track_index, track in enumerate(self.tracks):
            predicted = self._predicted_xywh(track)
            for detection_index, detection in enumerate(dets):
                if int(track.cls) != int(detection.cls):
                    continue
                overlap = self._iou(predicted, detection.xywh)
                if overlap >= self.iou_match:
                    pairs.append((overlap, track_index, detection_index))
        pairs.sort(key=lambda item: item[0], reverse=True)

        assigned_tracks: set[int] = set()
        assigned_detections: set[int] = set()
        for _overlap, track_index, detection_index in pairs:
            if track_index in assigned_tracks or detection_index in assigned_detections:
                continue
            self._apply_detection(self.tracks[track_index], dets[detection_index])
            assigned_tracks.add(track_index)
            assigned_detections.add(detection_index)

        # Advance unmatched tracks with the learned constant-velocity model.
        for track_index in range(existing_count):
            if track_index in assigned_tracks:
                continue
            track = self.tracks[track_index]
            if track.miss <= self.max_age:
                track.xywh = self._predicted_xywh(track)

        for detection_index, detection in enumerate(dets):
            if detection_index in assigned_detections:
                continue
            self.tracks.append(
                Track(
                    self.next_id,
                    detection.xywh.astype(float),
                    int(detection.cls),
                    float(detection.conf),
                    miss=0,
                    hits=1,
                    age=1,
                    state="tracked" if self.min_hits <= 1 else "tentative",
                )
            )
            self.next_id += 1

        self.tracks = [
            track
            for track in self.tracks
            if (track.hits >= self.min_hits or track.miss == 0) and track.miss <= self.max_age
        ]
        return self.tracks


class ByteTrackLiteTracker(NaiveIOUTracker):
    """ByteTrack-style two-stage association using high and low score detections."""

    def __init__(
        self,
        iou_match: float = 0.3,
        low_iou_match: float = 0.2,
        max_age: int = 30,
        min_hits: int = 3,
        track_thresh: float = 0.45,
        low_thresh: float = 0.10,
    ):
        super().__init__(iou_match=iou_match, max_age=max_age, min_hits=min_hits)
        self.low_iou_match = float(np.clip(low_iou_match, 0.0, 1.0))
        self.track_thresh = float(np.clip(track_thresh, 0.0, 1.0))
        self.low_thresh = float(np.clip(min(low_thresh, self.track_thresh), 0.0, self.track_thresh))

    def _match_stage(
        self,
        dets: list[Detection],
        det_indices: list[int],
        track_indices: list[int],
        iou_thresh: float,
        *,
        low_confidence: bool,
    ) -> tuple[set[int], set[int]]:
        pairs: list[tuple[float, int, int]] = []
        for track_index in track_indices:
            track = self.tracks[track_index]
            predicted = self._predicted_xywh(track)
            for detection_index in det_indices:
                if int(track.cls) != int(dets[detection_index].cls):
                    continue
                overlap = self._iou(predicted, dets[detection_index].xywh)
                if overlap >= iou_thresh:
                    pairs.append((overlap, track_index, detection_index))
        pairs.sort(key=lambda item: item[0], reverse=True)
        assigned_tracks: set[int] = set()
        assigned_detections: set[int] = set()
        for _overlap, track_index, detection_index in pairs:
            if track_index in assigned_tracks or detection_index in assigned_detections:
                continue
            self._apply_detection(
                self.tracks[track_index],
                dets[detection_index],
                low_confidence=low_confidence,
            )
            assigned_tracks.add(track_index)
            assigned_detections.add(detection_index)
        return assigned_tracks, assigned_detections

    def update(self, dets: list[Detection]) -> list[Track]:
        existing_count = len(self.tracks)
        for track in self.tracks:
            track.miss += 1
            track.age += 1
            track.state = "predicted" if track.miss <= self.max_age else "lost"

        high = [index for index, detection in enumerate(dets) if detection.conf >= self.track_thresh]
        low = [
            index
            for index, detection in enumerate(dets)
            if self.low_thresh <= detection.conf < self.track_thresh
        ]
        track_indices = list(range(existing_count))
        matched_tracks, matched_high = self._match_stage(
            dets, high, track_indices, self.iou_match, low_confidence=False
        )
        remaining_tracks = [index for index in track_indices if index not in matched_tracks]
        matched_low_tracks, _matched_low = self._match_stage(
            dets, low, remaining_tracks, self.low_iou_match, low_confidence=True
        )
        matched_tracks |= matched_low_tracks

        for track_index in track_indices:
            if track_index in matched_tracks:
                continue
            track = self.tracks[track_index]
            if track.miss <= self.max_age:
                track.xywh = self._predicted_xywh(track)

        # ByteTrack only creates new identities from high-confidence detections.
        for detection_index in high:
            if detection_index in matched_high:
                continue
            detection = dets[detection_index]
            self.tracks.append(
                Track(
                    self.next_id,
                    detection.xywh.astype(float),
                    int(detection.cls),
                    float(detection.conf),
                    miss=0,
                    hits=1,
                    age=1,
                    state="tracked" if self.min_hits <= 1 else "tentative",
                )
            )
            self.next_id += 1

        self.tracks = [
            track
            for track in self.tracks
            if (track.hits >= self.min_hits or track.miss == 0) and track.miss <= self.max_age
        ]
        return self.tracks


# ------------------------------ main processor ------------------------------

def _as_float(x: Union[float, int, tuple, list], default: float) -> float:
    try:
        if isinstance(x, (list, tuple)):
            if len(x) == 0:
                return float(default)
            if len(x) == 1:
                x = x[0]
        return float(x)
    except Exception:
        return float(default)

def _as_int(x: Union[int, float, tuple, list], default: int) -> int:
    try:
        if isinstance(x, (list, tuple)):
            if len(x) == 0:
                return int(default)
            if len(x) == 1:
                x = x[0]
        return int(x)
    except Exception:
        return int(default)

def _as_str(x: Any, default: str) -> str:
    try:
        if isinstance(x, (list, tuple)):
            if len(x) == 0:
                return str(default)
            if len(x) == 1:
                x = x[0]
        s = str(x)
        return s if s else str(default)
    except Exception:
        return str(default)


_NULL_CONTEXT = contextlib.nullcontext


class ObjectTrackProcessor:
    """
    Object detector + ByteTrack-style tracker.

    __init__(model="yolov8n.pt", conf=0.25, iou=0.5, tracker="bytetrack",
             max_age=30, min_hits=3, iou_match=0.3, classes=None, imgsz=640,
             device="cpu", cleanup_interval=0)

    set_emitter(fn): fn(t: float, tracks: list[tuple])
    process(rgb_uint8_frame: np.ndarray, pts: float|None) -> {"num_tracks": int}
    run_file(path: str) -> None
    close() -> None
    """

    def __init__(
            self,
            model: Union[str, tuple, list] = "yolov8n.pt",
            conf: Union[float, tuple, list] = 0.25,
            iou: Union[float, tuple, list] = 0.5,
            tracker: str = "bytetrack",
            detector_backend: str = "auto",
            max_age: Union[int, tuple, list] = 30,
            min_hits: Union[int, tuple, list] = 3,
            iou_match: Union[float, tuple, list] = 0.3,
            low_iou_match: Union[float, tuple, list] = 0.2,
            low_conf: Union[float, tuple, list] = 0.10,
            classes: Optional[List[int]] = None,
            imgsz: Union[int, tuple, list] = 640,
            device: Optional[str] = "cpu",
            cleanup_interval: Union[int, tuple, list] = 0,
            fast_close: bool = False,
            preprocess_mode: str = "native",
            low_light_enter: int = 3,
            low_light_exit: int = 8,
            quality_probe_interval: int = 3,
            emit_predicted: bool = False,
    ):
        model = _as_str(model, "yolov8n.pt")
        self.conf = float(np.clip(_as_float(conf, 0.25), 0.0, 1.0))
        self.iou = float(np.clip(_as_float(iou, 0.5), 0.0, 1.0))
        self.imgsz = max(32, _as_int(imgsz, 640))
        self.classes = classes
        self.device = device or "cpu"
        self._emit = None

        # pick the inference context factory once so we do not re-create
        # temporary classes or lambdas every frame (observed to leak a small
        # amount of interpreter memory under ASAN builds when created inside
        # the hot loop)
        if torch is not None and hasattr(torch, "inference_mode"):
            self._inference_ctx_factory = torch.inference_mode  # type: ignore[attr-defined]
        else:
            self._inference_ctx_factory = _NULL_CONTEXT

        tracker_name = _as_str(tracker, "bytetrack").lower()
        detector_backend_name = _as_str(detector_backend, "auto").lower()
        low_conf_value = min(self.conf, max(0.0, _as_float(low_conf, 0.10)))
        self.model_id = model
        self.tracker_name = tracker_name
        self.fast_close = bool(fast_close)
        self.preprocess_mode = str(preprocess_mode or "native").lower()
        if self.preprocess_mode not in {"native", "stable_low_light"}:
            raise ValueError("object_track preprocess_mode must be 'native' or 'stable_low_light'")
        self.emit_predicted = bool(emit_predicted)
        self.quality_probe_interval = max(1, int(quality_probe_interval))
        self._last_quality = None
        self._enhancement_controller = StableConditionController(
            enter_after=max(1, int(low_light_enter)),
            exit_after=max(1, int(low_light_exit)),
        )
        if detector_backend_name == "auto":
            detector_backend_name = "onnxruntime" if model.lower().endswith(".onnx") else "ultralytics"
        # ByteTrack needs detections below the public track threshold for its
        # second association stage.  V3 configured the detector at self.conf,
        # making low_conf ineffective and reducing recall.
        self.detector_conf = low_conf_value if tracker_name in ("bytetrack", "byte_track") else self.conf
        if detector_backend_name in ("onnx", "onnxruntime"):
            self.detector = ONNXRuntimeDetector(model, self.detector_conf, self.iou, self.imgsz, self.classes, self.device)
        elif detector_backend_name in ("ultralytics", "yolo"):
            self.detector = UltralyticsDetector(model, self.device, self.detector_conf, self.iou, self.imgsz, self.classes, self._inference_ctx_factory)
        else:
            raise ValueError(f"unsupported object_track detector_backend: {detector_backend!r}")
        self.names = self.detector.names

        if tracker_name in ("bytetrack", "byte_track"):
            self.trk = ByteTrackLiteTracker(iou_match=_as_float(iou_match, 0.3),
                                            low_iou_match=_as_float(low_iou_match, 0.2),
                                            max_age=_as_int(max_age, 30),
                                            min_hits=_as_int(min_hits, 3),
                                            track_thresh=self.conf,
                                            low_thresh=low_conf_value)
            self.tracker_name = "bytetrack"
        elif tracker_name in ("naive", "iou"):
            self.trk = NaiveIOUTracker(iou_match=_as_float(iou_match, 0.3),
                                       max_age=_as_int(max_age, 30),
                                       min_hits=_as_int(min_hits, 3))
            self.tracker_name = "iou"
        else:
            raise ValueError(f"unsupported object_track tracker: {tracker!r}")

        self._frame_counter = 0
        self._cleanup_interval = max(0, _as_int(cleanup_interval, 0))
        # preallocated BGR buffer to avoid per-frame allocations
        self._bgr = None
        self._last_pts: float | None = None
        self._closed = False

    def _cleanup_predictor(self, drop_predictor: bool = True) -> None:
        cleanup = getattr(getattr(self, "detector", None), "cleanup_predictor", None)
        if callable(cleanup):
            cleanup(drop_predictor)

    # ----- API -----

    def set_emitter(self, fn):
        self._emit = fn

    @staticmethod
    def describe():
        return {"algorithm": "object_track", "module": __name__,
                "class_name": "ObjectTrackProcessor",
                "detector_backends": ["ultralytics", "onnxruntime"],
                "tracker": "bytetrack"}

    def process(self, frame_rgb_uint8: np.ndarray, pts: Optional[float] = None) -> Dict[str, Any]:
        pts, frame_index, meta = process_meta(pts, default_time=0.0)
        context = ensure_frame_context(frame_rgb_uint8, meta)
        # Keep process() robust for deserialized/test-constructed processors and
        # older hosts that may restore instances without the newest attributes.
        quality = getattr(self, "_last_quality", None)
        quality_probe_interval = max(1, int(getattr(self, "quality_probe_interval", 3) or 3))
        quality_probed = False
        enhanced = False
        if self.preprocess_mode == "stable_low_light":
            controller = getattr(self, "_enhancement_controller", None)
            if controller is None:
                controller = StableConditionController()
                self._enhancement_controller = controller
            if quality is None or self._frame_counter % quality_probe_interval == 0:
                quality = context.lighting_quality(max_dimension=256)
                self._last_quality = quality
                controller.update(quality)
                quality_probed = True
            enhanced = controller.active
        profile = AIPreprocessProfile.TRACKING_STABLE if enhanced else AIPreprocessProfile.MODEL_NATIVE
        preprocess_view = context.get_view(profile, quality_override=quality if enhanced else None)
        detector_rgb = preprocess_view.frame
        # Reuse BGR buffer instead of allocating each frame.
        if self._bgr is None or self._bgr.shape != detector_rgb.shape:
            self._bgr = np.empty_like(detector_rgb)
        cv2.cvtColor(detector_rgb, cv2.COLOR_RGB2BGR, dst=self._bgr)
        bgr = self._bgr

        dets, r = self.detector.detect(bgr)

        tracks = self.trk.update(dets)

        out = []
        for tr in tracks:
            visible_state = tr.miss == 0 or self.emit_predicted
            if visible_state and tr.hits >= self.trk.min_hits and tr.conf >= self.conf:
                name = self.names.get(tr.cls, str(tr.cls)) if isinstance(self.names, dict) \
                    else (self.names[tr.cls] if tr.cls < len(self.names) else str(tr.cls))
                clipped = clip_bbox_xywh(
                    tr.xywh.tolist(),
                    frame_rgb_uint8.shape[1],
                    frame_rgb_uint8.shape[0],
                    min_size=0.5,
                )
                if clipped is None:
                    continue
                x, y, w, h = clipped
                out.append((
                    int(tr.tid), str(name), float(tr.conf), float(x), float(y), float(w), float(h),
                    float(tr.vx), float(tr.vy), int(tr.age), int(tr.miss), str(tr.state),
                ))

        if self._emit:
            self._emit(float(pts), out)
        event_time = float(pts)
        last_pts = getattr(self, "_last_pts", None)
        dt = event_time - last_pts if last_pts is not None else 0.0
        velocity_scale = 1.0 / dt if dt > 1e-9 else 0.0
        self._last_pts = event_time
        events = [
            event(
                kind="track",
                label=str(name),
                confidence=float(conf),
                time=event_time,
                bbox=bbox(x, y, w, h),
                track={"id": int(tid), "state": str(state), "age": int(age), "missed": int(missed)},
                attributes={
                    "velocity_px_per_processed_frame": {"x": float(vx), "y": float(vy)},
                    "velocity_px_per_second": {"x": float(vx * velocity_scale), "y": float(vy * velocity_scale)},
                    "processed_frame_dt_sec": float(dt) if dt > 0.0 else 0.0,
                    "model_id": self.model_id,
                    "detector_backend": self.detector.backend,
                    "tracker_backend": self.tracker_name,
                    "preprocess_profile": preprocess_view.profile,
                    "preprocess_operations": list(preprocess_view.operations),
                },
            )
            for tid, name, conf, x, y, w, h, vx, vy, age, missed, state in out
        ]

        # Break back-references so nothing retains the frame
        try:
            if hasattr(r, "orig_img"): r.orig_img = None
            if hasattr(r, "masks"):    r.masks = None
            if hasattr(r, "probs"):    r.probs = None
            if hasattr(r, "obb"):      r.obb   = None
            if hasattr(r, "boxes"):    r.boxes = None
        except Exception:
            pass

        # Drop result reference promptly
        if r is not None:
            del r
        self._frame_counter += 1
        cleanup_interval = max(0, int(getattr(self, "_cleanup_interval", 0) or 0))
        if cleanup_interval > 0 and self._frame_counter % cleanup_interval == 0:
            gc.collect()

        return normalize_frame_result(
            {
                "num_tracks": len(out),
                "metrics": {
                    "detections": len(dets),
                    "active_tracks": len(tracks),
                    "emitted_tracks": len(out),
                    "preprocess_profile": preprocess_view.profile,
                    "quality": quality.to_dict() if quality is not None else {"probed": False},
                    "quality_probed_this_frame": quality_probed,
                    "quality_probe_interval": quality_probe_interval,
                    "detector_confidence_threshold": float(getattr(self, "detector_conf", self.conf)),
                    "public_track_threshold": float(self.conf),
                    "forced_gc_interval": cleanup_interval,
                },
            },
            algorithm="object_track",
            time=event_time,
            frame_index=frame_index,
            processed=True,
            valid=True,
            degraded=enhanced,
            reason="stable_low_light_recovery" if enhanced else "",
            events=events,
        )

    def reset(self, _context: Any = None, _reason: str = "reset") -> None:
        if getattr(self, "trk", None) is not None:
            self.trk.tracks.clear()
            self.trk.next_id = 1
        self._frame_counter = 0
        self._bgr = None
        self._last_pts = None
        self._last_quality = None
        controller = getattr(self, "_enhancement_controller", None)
        if controller is not None:
            controller.reset()

    def run_file(self, media_path: str) -> None:
        cap = cv2.VideoCapture(media_path)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video: {media_path}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        i = 0
        while True:
            ok, bgr = cap.read()
            if not ok:
                break
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            pts = i / max(float(fps), 1e-6)
            self.process(rgb, pts)
            i += 1
        cap.release()

    def _close_impl(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        if getattr(self, "fast_close", False):
            try:
                if getattr(self, "trk", None) is not None and hasattr(self.trk, "tracks"):
                    self.trk.tracks.clear()
            except Exception:
                pass
            self._emit = None
            self._bgr = None
            return
        self._cleanup_predictor()
        try:
            if getattr(self, "trk", None) is not None and hasattr(self.trk, "tracks"):
                try:
                    self.trk.tracks.clear()
                except Exception:
                    pass
        finally:
            self.trk = None
        self._emit = None
        self._bgr = None
        self._enhancement_controller.reset()
        try:
            self.detector.close()
            del self.detector
        except Exception:
            self.detector = None  # type: ignore[assignment]
        gc.collect()
        if torch is not None and hasattr(torch, "cuda"):
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass

    def close(self):
        """Free model & caches; call from C++ node destructor."""
        self._close_impl()

    def close_(self):
        """Alias for close() so native code can call a stable name."""
        self._close_impl()

    def __del__(self):
        try:
            self._close_impl()
        except Exception:
            pass
