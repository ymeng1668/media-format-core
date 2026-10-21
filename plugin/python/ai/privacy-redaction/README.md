# Privacy Redaction

Face and license-plate detection with highlight, blur, pixelation, and solid
masks. Model files and Python dependencies are installed separately.

## Architecture

This plugin contains the privacy V5 implementation and only
its required contracts, geometry, preprocessing, tracking, face, plate and OCR
helpers. Results use the `media_format.ai.*` schema namespace.

The native library is a generic host. Select this module and class at runtime:

```text
module:     privacy_redaction
class_name: PrivacyRedactionProcessor
```

Each instance has independent face/vehicle/plate tracking and OCR state. RGB
pixels are processed directly; exact frame index/time arrive from the host.
Stream changes, backward time and geometry changes reset temporal tracking.
Call `reset()` explicitly after a seek or source replacement as well.

## Setup

Use Python 3.11+ with an environment matching the native CPython library:

```bash
python3 -m venv /absolute/venv
/absolute/venv/bin/python -m pip install -r plugin/python/ai/privacy-redaction/requirements.txt
export PYTHONPATH="$PWD/plugin/python:$PWD/plugin/python/ai/privacy-redaction"
export MEDIA_FORMAT_AI_MODEL_DIR=/absolute/approved-models
```

Install or select approved local models explicitly. No model is bundled.
The setup tool performs downloads only when invoked by the operator:

```bash
/absolute/venv/bin/python -m download_models --only face
/absolute/venv/bin/python -m download_models --only plate
```

Required roles for the default face+plate configuration:

| Role | Local model / backend |
| --- | --- |
| Faces | `face_detection_yunet_2023mar.onnx`, OpenCV YuNet |
| Vehicle/person context | Approved local `yolov8n.pt`, Ultralytics, or caller-provided `object_tracks` |
| Plates | `yolo-v9-t-512-license-plates-end2end.onnx`, OpenCV DNN or ONNX Runtime CPU fallback |
| Optional person-head fallback | `object_detection_yolox_2022nov.onnx`, or person tracks from the caller/tracker |
| Plate OCR (enabled by default) | `pytesseract` and a separately installed native Tesseract executable |

The setup tool does not approve model rights and does not install the
Ultralytics object-tracker weights. Supply those locally under the applicable
terms, or provide vehicle/person tracks from an independent source. The plate
weights are not presumed permissively licensed because their surrounding
project uses MIT. ONNX conversion does not change model licensing.

The default model directory is `MEDIA_FORMAT_AI_MODEL_DIR`, otherwise
`~/.cache/media-format-core/ai/models`. Explicit model paths override discovery.
The default processing
path never downloads missing weights: it reports unavailable state and blocks.

Privacy-specific overrides use `MEDIA_FORMAT_PRIVACY_MODEL_DIR`,
`MEDIA_FORMAT_PRIVACY_FACE_MODEL`, `MEDIA_FORMAT_PRIVACY_PERSON_MODEL`,
`MEDIA_FORMAT_PRIVACY_PLATE_MODEL`, and `MEDIA_FORMAT_PRIVACY_PRESET`.
The face detector also accepts `MEDIA_FORMAT_YUNET_FACE_MODEL`.

## Configuration

Start with [privacy.example.json](privacy.example.json). Defaults are
`mode=highlight`, `preset=balanced`, required automatic detection, confirmed
plate evidence, OCR enabled, no plate-text export and no saved crops. Highlight
draws labeled green boxes only; choose `blur`, `pixelate`, or `solid` for redaction.
The preset supplies the failure policy; missing required detection backends
block processing. File rendering supplies source dimensions and frame rate,
processes every frame in order, and numbers frames starting at 1.

- `targets`: `face`, `license_plate`, or both.
- `mode`: `pixelate`, `blur`, `solid`, `highlight`. Highlight draws boxes and is
  diagnostic only; it does not protect privacy.
- `preset`: `auto`, `fast`, `balanced`, `strict`, `archive`, `diagnostic`.
- `plate_mask_policy=confirmed_detected` masks stable candidates without OCR.
  `recognized_only` requires OCR; unreadable plates remain visible by that policy.
- Face-only cabin use: `targets=["face"]`, `auto_object_track=false`,
  `person_head_fallback=false` avoids the vehicle tracker dependency at runtime.
- Existing camera metadata can provide `object_tracks`: each item needs a
  vehicle/person label, confidence, pixel `bbox={x,y,w,h}`, and stable `track.id`.
  The image-based plate detector then searches those vehicle regions directly.
- Plate OCR is bounded and enabled by default. Install the native Tesseract
  executable in addition to the Python requirements (`sudo apt install
  tesseract-ocr` on Ubuntu). Set `plate_ocr_enabled=false` to disable it.
  Confirmed plates are still masked when OCR cannot read their text.

Never treat an operational-ready report as a recall guarantee. A missed vehicle
track can prevent its plate from being searched. Camera angle, tiny/blurred
faces/plates, model class errors and tracking gaps still need representative
coverage validation. The plugin emits readiness, region and timing metrics.

## Run through n9m_app

The Python bridge is enabled by default. Build the parent project with
`Python3_EXECUTABLE` set to the chosen virtualenv. From the parent repository
root, after installing models:

```bash
./bin/n9m_app input.mp4 --privacy-redaction --privacy-mode blur \
  --privacy-config deps/media-format-core/plugin/python/ai/privacy-redaction/privacy.face.example.json \
  --o redacted.mp4
```

Use `highlight` for boxes, `pixelate` for mosaic, or `solid` for black masks.
The face-only example needs only YuNet. Omit the config or use
`privacy.example.json` for faces plus plates with local tracker/plate models.
The existing Type 13 `--face-*` and `--forward-*` modes remain independent.

## Verification

```bash
PYTHONPATH=plugin/python:plugin/python/ai/privacy-redaction PYTHONDONTWRITEBYTECODE=1 \
  /absolute/venv/bin/python -m pytest -p no:cacheprovider -q tests/python_privacy
```

Native CTest covers borrowed-buffer ownership/release, strides, exact timestamp
metadata, worker serialization, host-owned Python, repeated close/reset and
failure paths. Python tests cover both masking and visible highlight output,
vehicle-bound plate association, confirmation gates, missing models and ONNX
fallback. Native evidence can be generated with the non-installed
`media_format_python_video_probe` target; it consumes decoded RGB, not a camera
protocol. Existing `render_privacy()` remains metadata-driven and does not
automatically invoke this bridge; consumers must explicitly supply decoded
frames to it.
