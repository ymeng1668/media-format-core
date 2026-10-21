#!/usr/bin/env python3
"""Explicit setup for privacy ONNX models.

The media processor never downloads models at runtime. Run this setup command
once, review the recorded hashes, then keep the models in the directory selected
by ``MEDIA_FORMAT_AI_MODEL_DIR`` or the default media-format-core model cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request

from core.model_paths import default_model_dir


MODELS = {
    "face": {
        "filename": "face_detection_yunet_2023mar.onnx",
        "url": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "size": 232_589,
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "license": "MIT (OpenCV Zoo model directory)",
    },
    "person": {
        "filename": "object_detection_yolox_2022nov.onnx",
        "url": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/object_detection_yolox/object_detection_yolox_2022nov.onnx",
        "size": 35_858_002,
        "sha256": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
        "license": "Apache-2.0 repository; review the model directory license before redistribution",
    },
    "plate": {
        "filename": "yolo-v9-t-512-license-plates-end2end.onnx",
        "url": "https://github.com/ankandrew/open-image-models/releases/download/assets/yolo-v9-t-512-license-plates-end2end.onnx",
        "size": 7_799_480,
        "sha256": "",
        "license": "MIT project; verify model/training-data terms before product redistribution",
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "media-format-Privacy-Model-Installer/5.0",
            "Accept": "application/octet-stream,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as target:
        shutil.copyfileobj(response, target, length=1024 * 1024)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download approved local models for privacy_redaction")
    parser.add_argument("--models-dir", default=str(default_model_dir()))
    parser.add_argument("--only", choices=["all", "face", "person", "plate"], default="all")
    parser.add_argument("--skip-person", action="store_true", help="Compatibility: omit the person model")
    parser.add_argument("--skip-plate", action="store_true", help="Compatibility: omit the plate model")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--plate-sha256", default="", help="Required approved hash for the plate model in controlled production builds")
    args = parser.parse_args()
    if args.only != "all" and (args.skip_person or args.skip_plate):
        parser.error("--only cannot be combined with --skip-person or --skip-plate")

    models_dir = Path(args.models_dir).expanduser()
    models_dir.mkdir(parents=True, exist_ok=True)
    selected = MODELS if args.only == "all" else {args.only: MODELS[args.only]}
    selected = {role: spec for role, spec in selected.items()
                if not (role == "person" and args.skip_person)
                and not (role == "plate" and args.skip_plate)}
    records: dict[str, dict] = {}

    for role, spec in selected.items():
        destination = models_dir / str(spec["filename"])
        expected_sha = str(args.plate_sha256 if role == "plate" and args.plate_sha256 else spec["sha256"]).lower()
        if destination.is_file() and not args.force:
            actual_sha = _sha256(destination)
            actual_size = destination.stat().st_size
            if actual_size == int(spec["size"]) and (not expected_sha or actual_sha == expected_sha):
                print(f"{role}: already verified at {destination}")
                records[role] = {
                    **spec,
                    "path": str(destination.resolve()),
                    "actual_size": actual_size,
                    "actual_sha256": actual_sha,
                    "sha256_pinned": bool(expected_sha),
                }
                continue
            raise RuntimeError(
                f"existing {role} model failed validation: {destination}; use --force only after reviewing the source"
            )

        with tempfile.NamedTemporaryFile(prefix=destination.name + ".", suffix=".part", dir=models_dir, delete=False) as temp:
            temp_path = Path(temp.name)
        try:
            print(f"Downloading {role}: {spec['url']}")
            _download(str(spec["url"]), temp_path)
            actual_size = temp_path.stat().st_size
            actual_sha = _sha256(temp_path)
            if actual_size != int(spec["size"]):
                raise RuntimeError(f"{role} model size mismatch: {actual_size} != {spec['size']}")
            if expected_sha and actual_sha != expected_sha:
                raise RuntimeError(f"{role} model SHA-256 mismatch: {actual_sha} != {expected_sha}")
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)

        records[role] = {
            **spec,
            "path": str(destination.resolve()),
            "actual_size": destination.stat().st_size,
            "actual_sha256": _sha256(destination),
            "sha256_pinned": bool(expected_sha),
        }
        print(f"{role}: installed {destination}")

    manifest_path = models_dir / "privacy_models.sha256.json"
    previous = {}
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    merged = {}
    if isinstance(previous, dict):
        if isinstance(previous.get("models"), dict):
            merged = dict(previous["models"])
        else:
            merged = {role: record for role, record in previous.items()
                      if role in MODELS and isinstance(record, dict)}
    merged.update(records)
    manifest = {
        "schema": "media_format.ai.privacy_models.v1",
        "models": merged,
        "note": "Pin and approve the recorded plate-model SHA-256 before product distribution.",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote model manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
