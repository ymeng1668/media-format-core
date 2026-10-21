from __future__ import annotations

import numpy as np

from core.privacy_detectors import OpenCVYoloV9PlateDetector, OpenCVYoloXContextDetector


class _PlateNet:
    def setInput(self, _blob):
        return None

    def forward(self):
        return np.asarray([[[0, 100, 120, 220, 170, 0, 0.91]]], dtype=np.float32)


def test_yolov9_plate_output_maps_to_source_coordinates():
    detector = OpenCVYoloV9PlateDetector("missing.onnx", input_size=512)
    detector._net = _PlateNet()  # test the adapter without shipping a model binary
    detector._init_attempted = True
    frame = np.zeros((512, 512, 3), dtype=np.uint8)
    detections = detector.detect(frame)
    assert len(detections) == 1
    item = detections[0]
    assert item.kind == "license_plate"
    assert item.xywh == (100.0, 120.0, 120.0, 50.0)
    assert item.confidence > 0.9


def test_yolov9_parser_accepts_transposed_output():
    raw = np.asarray([[0], [10], [20], [30], [40], [0], [0.8]], dtype=np.float32)
    rows = OpenCVYoloV9PlateDetector._rows(raw)
    assert rows.shape == (1, 7)


def test_yolox_prediction_matrix_removes_batch_dimension():
    raw = np.zeros((1, 8400, 85), dtype=np.float32)
    matrix = OpenCVYoloXContextDetector._prediction_matrix(raw)
    assert matrix.shape == (8400, 85)

class _YoloXNet:
    def setInput(self, _blob):
        return None

    def getUnconnectedOutLayersNames(self):
        return ["output"]

    def forward(self, _names):
        values = np.zeros((1, 8400, 85), dtype=np.float32)
        values[0, 0, 0] = 10.0
        values[0, 0, 1] = 10.0
        values[0, 0, 2] = np.log(5.0)
        values[0, 0, 3] = np.log(5.0)
        values[0, 0, 4] = 1.0
        values[0, 0, 5] = 1.0  # COCO person
        return [values]


def test_yolox_context_adapter_emits_person():
    detector = OpenCVYoloXContextDetector("missing.onnx", conf_threshold=0.2)
    detector._net = _YoloXNet()
    detector._init_attempted = True
    frame = np.zeros((640, 640, 3), dtype=np.uint8)
    detections = detector.detect(frame)
    assert len(detections) == 1
    assert detections[0].kind == "person"
    assert detections[0].attributes["class_label"] == "person"
    x, y, w, h = detections[0].xywh
    assert 55 <= x <= 65
    assert 55 <= y <= 65
    assert 35 <= w <= 45
    assert 35 <= h <= 45
