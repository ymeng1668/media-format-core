"""Optional model backends for the media-format dashcam privacy processor.

Backends are lazy and failure-isolated.  Missing optional packages or model
files never crash the media graph; the processor's fail policy decides whether
to continue best-effort, hold previous masks, or block the frame.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

import cv2
import numpy as np

from .geometry import clip_bbox_xywh
from .model_paths import default_model_dir
from .privacy_tracking import PrivacyDetection


@dataclass(slots=True)
class DetectorReadiness:
    ready: bool
    backend: str
    reason: str = ""


class YuNetFaceDetector:
    def __init__(
        self,
        model_path: str = "",
        *,
        score_threshold: float = 0.45,
        nms_threshold: float = 0.30,
        top_k: int = 5000,
        max_dimension: int = 960,
    ) -> None:
        package_default = Path(__file__).resolve().parents[1] / "models" / "face_detection_yunet_2023mar.onnx"
        configured = str(model_path or os.environ.get("MEDIA_FORMAT_YUNET_FACE_MODEL", "")).strip()
        self.model_path = _resolve_local_model(configured, "models/face_detection_yunet_2023mar.onnx") if configured else (package_default if package_default.is_file() else default_model_dir() / package_default.name)
        self.score_threshold = float(score_threshold)
        self.nms_threshold = float(nms_threshold)
        self.top_k = max(1, int(top_k))
        self.max_dimension = max(160, int(max_dimension))
        self._detector: Any = None
        self._init_attempted = False
        self._error = ""

    @property
    def readiness(self) -> DetectorReadiness:
        if self._error:
            return DetectorReadiness(False, "opencv_yunet", self._error)
        if self._detector is not None:
            return DetectorReadiness(True, "opencv_yunet")
        if not self.model_path.is_file():
            return DetectorReadiness(False, "opencv_yunet", f"model_missing:{self.model_path}")
        if self._init_attempted and self._error:
            return DetectorReadiness(False, "opencv_yunet", self._error)
        return DetectorReadiness(True, "opencv_yunet", "lazy")

    def _ensure(self, input_size: tuple[int, int]) -> bool:
        if self._detector is not None:
            return True
        if self._init_attempted:
            return False
        self._init_attempted = True
        if not self.model_path.is_file():
            self._error = f"model_missing:{self.model_path}"
            return False
        try:
            self._detector = cv2.FaceDetectorYN_create(
                str(self.model_path),
                "",
                input_size,
                self.score_threshold,
                self.nms_threshold,
                self.top_k,
            )
            self._error = ""
            return True
        except Exception as exc:  # pragma: no cover - backend-specific
            self._detector = None
            self._error = f"{type(exc).__name__}: {exc}"
            return False

    def detect(self, frame_rgb: np.ndarray, *, rois: Sequence[tuple[float, float, float, float]] | None = None) -> list[PrivacyDetection]:
        height, width = frame_rgb.shape[:2]
        jobs: list[tuple[np.ndarray, int, int, float, str]] = []
        if rois:
            for raw in rois:
                clipped = clip_bbox_xywh(raw, width, height, min_size=12.0)
                if clipped is None:
                    continue
                x, y, w, h = clipped
                x0, y0 = int(x), int(y)
                crop = frame_rgb[y0 : int(y + h), x0 : int(x + w)]
                if crop.size:
                    jobs.append((crop, x0, y0, 1.0, "opencv_yunet_roi"))
        else:
            scale = min(1.0, float(self.max_dimension) / float(max(width, height)))
            if scale < 1.0:
                scan = cv2.resize(frame_rgb, (max(1, int(round(width * scale))), max(1, int(round(height * scale)))), interpolation=cv2.INTER_AREA)
            else:
                scan = frame_rgb
            jobs.append((scan, 0, 0, scale, "opencv_yunet_full"))

        output: list[PrivacyDetection] = []
        for image_rgb, offset_x, offset_y, scale, source in jobs:
            scan_h, scan_w = image_rgb.shape[:2]
            if not self._ensure((scan_w, scan_h)):
                continue
            try:
                self._detector.setInputSize((scan_w, scan_h))
                _retval, rows = self._detector.detect(cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR))
                self._error = ""
            except Exception as exc:  # pragma: no cover - backend-specific
                self._error = f"{type(exc).__name__}: {exc}"
                continue
            if rows is None:
                continue
            inv = 1.0 / max(scale, 1e-12)
            for row in np.asarray(rows):
                if len(row) < 15:
                    continue
                x, y, w, h = [float(v) for v in row[:4]]
                score = float(row[-1])
                box = (
                    offset_x + x * inv,
                    offset_y + y * inv,
                    w * inv,
                    h * inv,
                )
                clipped = clip_bbox_xywh(box, width, height, min_size=3.0)
                if clipped is None:
                    continue
                output.append(PrivacyDetection("face", clipped, score, source))
        return output


class OpenImageModelDetector:
    """Adapter for MIT-licensed ``open-image-models`` ONNX detectors."""

    def __init__(
        self,
        model: str,
        *,
        kind: str,
        conf_threshold: float = 0.20,
        allowed_labels: Iterable[str] = (),
        providers: Sequence[str] | None = None,
        batch_size: int = 1,
    ) -> None:
        self.model = str(model or "").strip()
        self.kind = str(kind)
        self.conf_threshold = float(conf_threshold)
        self.allowed_labels = {str(item).lower() for item in allowed_labels}
        self.providers = list(providers or []) or None
        self.batch_size = max(1, int(batch_size))
        self._detector: Any = None
        self._init_attempted = False
        self._error = ""

    @property
    def readiness(self) -> DetectorReadiness:
        if self._error:
            return DetectorReadiness(False, "open_image_models", self._error)
        if self._detector is not None:
            return DetectorReadiness(True, "open_image_models")
        if not self.model:
            return DetectorReadiness(False, "open_image_models", "model_not_configured")
        if self._init_attempted and self._error:
            return DetectorReadiness(False, "open_image_models", self._error)
        return DetectorReadiness(True, "open_image_models", "lazy")

    def _ensure(self) -> bool:
        if self._detector is not None:
            return True
        if self._init_attempted:
            return False
        self._init_attempted = True
        if not self.model:
            self._error = "model_not_configured"
            return False
        try:
            from open_image_models import create_detector  # type: ignore

            model_path = Path(self.model)
            if not model_path.is_file() and not model_path.is_absolute():
                package_relative = Path(__file__).resolve().parents[1] / model_path
                if package_relative.is_file():
                    model_path = package_relative
            if model_path.is_file():
                backend = "yolo_v9" if self.kind == "license_plate" else "rf_detr"
                if self.kind == "license_plate":
                    labels = ("License Plate",)
                else:
                    from open_image_models.detection.core.coco import COCO_CLASSES  # type: ignore
                    labels = COCO_CLASSES
                self._detector = create_detector(
                    model_path,
                    backend=backend,
                    class_labels=labels,
                    conf_thresh=self.conf_threshold,
                    providers=self.providers,
                    batch_size=self.batch_size,
                )
            else:
                raise FileNotFoundError(f"privacy model must be installed locally: {self.model}")
            self._error = ""
            return True
        except Exception as exc:
            self._detector = None
            self._error = f"{type(exc).__name__}: {exc}"
            return False

    def detect(self, frame_rgb: np.ndarray, *, rois: Sequence[tuple[float, float, float, float]] | None = None) -> list[PrivacyDetection]:
        if not self._ensure():
            return []
        height, width = frame_rgb.shape[:2]
        jobs: list[tuple[np.ndarray, int, int]] = []
        if rois:
            for raw in rois:
                clipped = clip_bbox_xywh(raw, width, height, min_size=8.0)
                if clipped is None:
                    continue
                x, y, w, h = clipped
                x0, y0, x1, y1 = int(x), int(y), int(x + w), int(y + h)
                crop = frame_rgb[y0:y1, x0:x1]
                if crop.size:
                    jobs.append((crop, x0, y0))
        else:
            jobs.append((frame_rgb, 0, 0))

        detections: list[PrivacyDetection] = []
        if not jobs:
            return detections
        images_bgr = [cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR) for image_rgb, _x, _y in jobs]
        try:
            raw_results = self._detector.predict(images_bgr if len(images_bgr) > 1 else images_bgr[0])
            self._error = ""
        except Exception as exc:  # pragma: no cover - backend-specific
            self._error = f"{type(exc).__name__}: {exc}"
            return detections
        if len(jobs) == 1:
            result_groups = [raw_results or []]
        else:
            result_groups = list(raw_results or [])
        for (_image_rgb, offset_x, offset_y), results in zip(jobs, result_groups):
            for result in results or []:
                label = str(getattr(result, "label", "")).lower()
                if self.allowed_labels and label not in self.allowed_labels:
                    continue
                score = float(getattr(result, "confidence", 0.0))
                box = getattr(result, "bounding_box", None)
                if box is None:
                    continue
                try:
                    xywh = (
                        offset_x + float(box.x1),
                        offset_y + float(box.y1),
                        float(box.x2 - box.x1),
                        float(box.y2 - box.y1),
                    )
                except Exception:
                    continue
                clipped = clip_bbox_xywh(xywh, width, height, min_size=2.0)
                if clipped is None:
                    continue
                kind = self.kind
                if self.kind == "object":
                    kind = "person" if label in {"person", "pedestrian"} else "vehicle"
                detections.append(
                    PrivacyDetection(
                        kind=kind,
                        xywh=clipped,
                        confidence=score,
                        source="open_image_models",
                        attributes={"model": self.model, "class_label": label},
                    )
                )
        return detections

# COCO labels used by the OpenCV Zoo YOLOX model.  Only privacy-relevant
# classes are materialized by the adapter below.
_COCO_CONTEXT_LABELS: dict[int, str] = {
    0: "person",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}


def _configure_opencv_dnn(net: Any, backend: str) -> str:
    """Configure an OpenCV DNN network and return the active backend label."""
    value = str(backend or "cpu").strip().lower()
    if value in {"cuda", "gpu"}:
        net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
        net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
        return "opencv_dnn_cuda"
    if value in {"cuda_fp16", "gpu_fp16", "fp16"}:
        net.setPreferableBackend(cv2.dnn.DNN_BACKEND_CUDA)
        net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA_FP16)
        return "opencv_dnn_cuda_fp16"
    if value in {"openvino", "inference_engine"}:
        backend_id = getattr(cv2.dnn, "DNN_BACKEND_INFERENCE_ENGINE", None)
        if backend_id is None:
            raise RuntimeError("OpenCV build has no OpenVINO/Inference Engine DNN backend")
        net.setPreferableBackend(backend_id)
        net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        return "opencv_dnn_openvino"
    net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
    net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
    return "opencv_dnn_cpu"


def _resolve_local_model(model_path: str, package_name: str) -> Path:
    configured = str(model_path or "").strip()
    candidate = Path(configured).expanduser() if configured else Path(package_name)
    if not candidate.is_absolute():
        package_candidate = Path(__file__).resolve().parents[1] / candidate
        if package_candidate.is_file():
            candidate = package_candidate
        elif not candidate.is_file():
            cached = default_model_dir() / candidate.name
            candidate = cached if cached.is_file() or not configured else package_candidate
    return candidate


def _letterbox_center(
    image: np.ndarray,
    size: int,
    *,
    color: tuple[int, int, int] = (114, 114, 114),
) -> tuple[np.ndarray, float, float, float]:
    height, width = image.shape[:2]
    ratio = min(float(size) / max(height, 1), float(size) / max(width, 1))
    resized_w = max(1, int(round(width * ratio)))
    resized_h = max(1, int(round(height * ratio)))
    resized = cv2.resize(image, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR)
    pad_x = (size - resized_w) / 2.0
    pad_y = (size - resized_h) / 2.0
    left = int(round(pad_x - 0.1))
    right = int(round(pad_x + 0.1))
    top = int(round(pad_y - 0.1))
    bottom = int(round(pad_y + 0.1))
    padded = cv2.copyMakeBorder(resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    if padded.shape[0] != size or padded.shape[1] != size:
        padded = cv2.resize(padded, (size, size), interpolation=cv2.INTER_LINEAR)
    return padded, ratio, pad_x, pad_y


def _letterbox_top_left(
    image: np.ndarray,
    size: int,
    *,
    color: float = 114.0,
) -> tuple[np.ndarray, float]:
    height, width = image.shape[:2]
    ratio = min(float(size) / max(height, 1), float(size) / max(width, 1))
    resized_w = max(1, int(width * ratio))
    resized_h = max(1, int(height * ratio))
    resized = cv2.resize(image, (resized_w, resized_h), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    padded = np.full((size, size, 3), float(color), dtype=np.float32)
    padded[:resized_h, :resized_w] = resized
    return padded, ratio


class OpenCVYoloV9PlateDetector:
    """Direct OpenCV-DNN adapter for YOLOv9 end-to-end plate ONNX models.

    The expected output is the end-to-end NMS form used by the Open Image
    Models license-plate weights: ``[batch, x1, y1, x2, y2, class, score]``.
    OpenCV DNN remains the first backend. Local ONNX Runtime inference is used
    when OpenCV cannot import end-to-end operators such as NonMaxSuppression.
    """

    def __init__(
        self,
        model_path: str = "models/yolo-v9-t-512-license-plates-end2end.onnx",
        *,
        conf_threshold: float = 0.20,
        input_size: int = 512,
        dnn_backend: str = "cpu",
    ) -> None:
        self.model_path = _resolve_local_model(model_path, "models/yolo-v9-t-512-license-plates-end2end.onnx")
        self.conf_threshold = float(conf_threshold)
        self.input_size = max(160, int(input_size))
        self.dnn_backend = str(dnn_backend or "cpu")
        self._net: Any = None
        self._ort_session: Any = None
        self._ort_input = ""
        self._init_attempted = False
        self._error = ""
        self._backend_label = "opencv_dnn"

    @property
    def readiness(self) -> DetectorReadiness:
        if self._error:
            return DetectorReadiness(False, self._backend_label, self._error)
        if self._net is not None or self._ort_session is not None:
            return DetectorReadiness(True, self._backend_label)
        if not self.model_path.is_file():
            return DetectorReadiness(False, self._backend_label, f"model_missing:{self.model_path}")
        return DetectorReadiness(True, self._backend_label, "lazy")

    def _ensure(self) -> bool:
        if self._net is not None or self._ort_session is not None:
            return True
        if self._init_attempted:
            return False
        self._init_attempted = True
        if not self.model_path.is_file():
            self._error = f"model_missing:{self.model_path}"
            return False
        try:
            self._net = cv2.dnn.readNetFromONNX(str(self.model_path))
            self._backend_label = _configure_opencv_dnn(self._net, self.dnn_backend)
            self._error = ""
            return True
        except Exception as exc:  # pragma: no cover - model/backend-specific
            self._net = None
            opencv_error = f"{type(exc).__name__}: {exc}"
        try:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            session = ort.InferenceSession(str(self.model_path), sess_options=options,
                                           providers=["CPUExecutionProvider"])
            inputs = session.get_inputs()
            if len(inputs) != 1 or inputs[0].type != "tensor(float)":
                raise ValueError("plate model must have one float32 image input")
            shape = inputs[0].shape
            if len(shape) != 4:
                raise ValueError("plate model input must be NCHW")
            if isinstance(shape[2], int) and isinstance(shape[3], int):
                if shape[2] != shape[3] or shape[2] <= 0:
                    raise ValueError("plate model input must be square")
                self.input_size = shape[2]
            self._ort_session = session
            self._ort_input = inputs[0].name
            self._backend_label = "onnxruntime_cpu"
            self._error = ""
            return True
        except Exception as exc:
            self._error = f"{opencv_error}; onnxruntime: {type(exc).__name__}: {exc}"
            return False

    @staticmethod
    def _rows(raw: Any) -> np.ndarray:
        if isinstance(raw, (list, tuple)):
            if not raw:
                return np.empty((0, 7), dtype=np.float32)
            raw = raw[0]
        values = np.asarray(raw)
        while values.ndim > 2 and values.shape[0] == 1:
            values = values[0]
        if values.ndim == 1:
            values = values.reshape(1, -1)
        if values.ndim != 2:
            return np.empty((0, 7), dtype=np.float32)
        if values.shape[1] < 7 and values.shape[0] >= 7:
            values = values.T
        return values if values.shape[1] >= 7 else np.empty((0, 7), dtype=np.float32)

    def _detect_one(
        self,
        image_rgb: np.ndarray,
        *,
        offset_x: int,
        offset_y: int,
        source_width: int,
        source_height: int,
    ) -> list[PrivacyDetection]:
        padded, ratio, pad_x, pad_y = _letterbox_center(image_rgb, self.input_size)
        # Input is already RGB, so swapRB must remain false.
        blob = cv2.dnn.blobFromImage(
            padded,
            scalefactor=1.0 / 255.0,
            size=(self.input_size, self.input_size),
            mean=(0.0, 0.0, 0.0),
            swapRB=False,
            crop=False,
        )
        try:
            if self._ort_session is not None:
                raw = self._ort_session.run(None, {self._ort_input: blob})
            else:
                self._net.setInput(blob)
                raw = self._net.forward()
            self._error = ""
        except Exception as exc:  # pragma: no cover - model/backend-specific
            self._error = f"{type(exc).__name__}: {exc}"
            return []
        output: list[PrivacyDetection] = []
        for row in self._rows(raw):
            score = float(row[6])
            if not np.isfinite(score) or score < self.conf_threshold:
                continue
            x1 = (float(row[1]) - pad_x) / max(ratio, 1e-12)
            y1 = (float(row[2]) - pad_y) / max(ratio, 1e-12)
            x2 = (float(row[3]) - pad_x) / max(ratio, 1e-12)
            y2 = (float(row[4]) - pad_y) / max(ratio, 1e-12)
            clipped = clip_bbox_xywh(
                (offset_x + x1, offset_y + y1, x2 - x1, y2 - y1),
                source_width,
                source_height,
                min_size=2.0,
            )
            if clipped is None:
                continue
            output.append(
                PrivacyDetection(
                    "license_plate",
                    clipped,
                    score,
                    f"{self._backend_label}_yolov9_plate",
                    {"model": str(self.model_path), "class_id": int(row[5])},
                )
            )
        return output

    def detect(
        self,
        frame_rgb: np.ndarray,
        *,
        rois: Sequence[tuple[float, float, float, float]] | None = None,
    ) -> list[PrivacyDetection]:
        if not self._ensure():
            return []
        height, width = frame_rgb.shape[:2]
        output: list[PrivacyDetection] = []
        if rois:
            for raw in rois:
                clipped = clip_bbox_xywh(raw, width, height, min_size=12.0)
                if clipped is None:
                    continue
                x, y, w, h = clipped
                x0, y0, x1, y1 = int(x), int(y), int(x + w), int(y + h)
                crop = frame_rgb[y0:y1, x0:x1]
                if crop.size:
                    output.extend(
                        self._detect_one(
                            crop,
                            offset_x=x0,
                            offset_y=y0,
                            source_width=width,
                            source_height=height,
                        )
                    )
        else:
            output.extend(
                self._detect_one(
                    frame_rgb,
                    offset_x=0,
                    offset_y=0,
                    source_width=width,
                    source_height=height,
                )
            )
        return output


class OpenCVYoloXContextDetector:
    """OpenCV-Zoo YOLOX person/vehicle context detector.

    It emits only ``person`` and plate-relevant ``vehicle`` observations.  The
    processor uses those boxes to mask profile heads and to rescan vehicles for
    small plates at higher effective resolution.
    """

    def __init__(
        self,
        model_path: str = "models/object_detection_yolox_2022nov.onnx",
        *,
        conf_threshold: float = 0.28,
        nms_threshold: float = 0.50,
        input_size: int = 640,
        dnn_backend: str = "cpu",
        max_detections: int = 128,
    ) -> None:
        self.model_path = _resolve_local_model(model_path, "models/object_detection_yolox_2022nov.onnx")
        self.conf_threshold = float(conf_threshold)
        self.nms_threshold = float(nms_threshold)
        self.input_size = max(160, int(input_size))
        self.dnn_backend = str(dnn_backend or "cpu")
        self.max_detections = max(1, int(max_detections))
        self._net: Any = None
        self._init_attempted = False
        self._error = ""
        self._backend_label = "opencv_dnn"
        self._grids, self._expanded_strides = self._anchors(self.input_size)

    @staticmethod
    def _anchors(size: int) -> tuple[np.ndarray, np.ndarray]:
        grids: list[np.ndarray] = []
        strides_out: list[np.ndarray] = []
        for stride in (8, 16, 32):
            hsize = size // stride
            wsize = size // stride
            xv, yv = np.meshgrid(np.arange(hsize), np.arange(wsize))
            grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
            grids.append(grid)
            strides_out.append(np.full((*grid.shape[:2], 1), stride, dtype=np.float32))
        return np.concatenate(grids, axis=1), np.concatenate(strides_out, axis=1)

    @property
    def readiness(self) -> DetectorReadiness:
        if self._error:
            return DetectorReadiness(False, self._backend_label, self._error)
        if self._net is not None:
            return DetectorReadiness(True, self._backend_label)
        if not self.model_path.is_file():
            return DetectorReadiness(False, self._backend_label, f"model_missing:{self.model_path}")
        return DetectorReadiness(True, self._backend_label, "lazy")

    def _ensure(self) -> bool:
        if self._net is not None:
            return True
        if self._init_attempted:
            return False
        self._init_attempted = True
        if not self.model_path.is_file():
            self._error = f"model_missing:{self.model_path}"
            return False
        try:
            self._net = cv2.dnn.readNetFromONNX(str(self.model_path))
            self._backend_label = _configure_opencv_dnn(self._net, self.dnn_backend)
            self._error = ""
            return True
        except Exception as exc:  # pragma: no cover - model/backend-specific
            self._net = None
            self._error = f"{type(exc).__name__}: {exc}"
            return False

    @staticmethod
    def _prediction_matrix(raw: Any) -> np.ndarray:
        if isinstance(raw, (list, tuple)):
            if not raw:
                return np.empty((0, 85), dtype=np.float32)
            raw = raw[0]
        values = np.asarray(raw)
        while values.ndim > 2 and values.shape[0] == 1:
            values = values[0]
        if values.ndim == 1:
            values = values.reshape(1, -1)
        if values.ndim != 2:
            return np.empty((0, 85), dtype=np.float32)
        if values.shape[1] < 6 and values.shape[0] >= 6:
            values = values.T
        return values

    def detect(
        self,
        frame_rgb: np.ndarray,
        *,
        rois: Sequence[tuple[float, float, float, float]] | None = None,
    ) -> list[PrivacyDetection]:
        del rois  # context detection is always full-frame
        if not self._ensure():
            return []
        height, width = frame_rgb.shape[:2]
        padded, ratio = _letterbox_top_left(frame_rgb, self.input_size)
        blob = np.transpose(padded, (2, 0, 1))[np.newaxis, :, :, :].astype(np.float32)
        try:
            self._net.setInput(blob)
            raw = self._net.forward(self._net.getUnconnectedOutLayersNames())
            self._error = ""
        except Exception as exc:  # pragma: no cover - model/backend-specific
            self._error = f"{type(exc).__name__}: {exc}"
            return []
        dets = self._prediction_matrix(raw)
        if dets.size == 0 or dets.shape[1] < 6:
            return []
        expected = self._grids.shape[1]
        if dets.shape[0] != expected and dets.shape[1] == expected:
            dets = dets.T
        if dets.shape[0] != expected:
            self._error = f"unexpected_yolox_output_shape:{tuple(dets.shape)}"
            return []
        decoded = dets.copy()
        decoded[:, :2] = (decoded[:, :2] + self._grids[0]) * self._expanded_strides[0]
        decoded[:, 2:4] = np.exp(np.clip(decoded[:, 2:4], -20.0, 20.0)) * self._expanded_strides[0]
        objectness = decoded[:, 4]
        class_scores = decoded[:, 5:]
        if class_scores.size == 0:
            return []
        class_ids = np.argmax(class_scores, axis=1)
        scores = objectness * class_scores[np.arange(class_scores.shape[0]), class_ids]
        keep_mask = np.fromiter(
            (int(class_id) in _COCO_CONTEXT_LABELS and float(score) >= self.conf_threshold for class_id, score in zip(class_ids, scores)),
            dtype=bool,
            count=len(scores),
        )
        indices = np.flatnonzero(keep_mask)
        if indices.size == 0:
            return []
        centers = decoded[indices, :4]
        selected_scores = scores[indices].astype(np.float32)
        selected_classes = class_ids[indices].astype(np.int32)
        boxes = np.column_stack(
            (
                centers[:, 0] - centers[:, 2] * 0.5,
                centers[:, 1] - centers[:, 3] * 0.5,
                centers[:, 2],
                centers[:, 3],
            )
        ).astype(np.float32)
        try:
            kept = cv2.dnn.NMSBoxesBatched(
                boxes.tolist(),
                selected_scores.tolist(),
                selected_classes.tolist(),
                self.conf_threshold,
                self.nms_threshold,
            )
            kept_indices = np.asarray(kept, dtype=np.int32).reshape(-1)
        except Exception:
            kept_parts: list[int] = []
            for class_id in np.unique(selected_classes):
                local = np.flatnonzero(selected_classes == class_id)
                class_keep = cv2.dnn.NMSBoxes(
                    boxes[local].tolist(),
                    selected_scores[local].tolist(),
                    self.conf_threshold,
                    self.nms_threshold,
                )
                for item in np.asarray(class_keep, dtype=np.int32).reshape(-1):
                    kept_parts.append(int(local[int(item)]))
            kept_indices = np.asarray(kept_parts, dtype=np.int32)
        if kept_indices.size == 0:
            return []
        order = kept_indices[np.argsort(selected_scores[kept_indices])[::-1]][: self.max_detections]
        output: list[PrivacyDetection] = []
        for index in order:
            x, y, w, h = (float(value) / max(ratio, 1e-12) for value in boxes[int(index)])
            clipped = clip_bbox_xywh((x, y, w, h), width, height, min_size=4.0)
            if clipped is None:
                continue
            class_id = int(selected_classes[int(index)])
            label = _COCO_CONTEXT_LABELS[class_id]
            kind = "person" if class_id == 0 else "vehicle"
            output.append(
                PrivacyDetection(
                    kind,
                    clipped,
                    float(selected_scores[int(index)]),
                    "opencv_dnn_yolox",
                    {"model": str(self.model_path), "class_label": label, "class_id": class_id},
                )
            )
        return output
