"""Privacy-redaction presets, model discovery, and effective configuration.

The public privacy processor has many low-level controls because face and plate
privacy are sensitive to camera position, resolution, frame rate, and latency
budget.  This module provides stable high-level presets while retaining explicit
per-option overrides.

Precedence, from lowest to highest:

1. V5 base defaults.
2. Selected preset (``auto``, ``fast``, ``balanced``, ``strict``, ``archive``,
   or ``diagnostic``).
3. Optional JSON configuration file.
4. Explicit constructor/CLI overrides.
5. Automatic local-model discovery, but only for model paths still empty.

No preset downloads a model during media processing.  Automatic mode means
automatic *local* discovery and selection; downloads are an explicit setup step.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from .model_paths import default_model_dir


PRIVACY_PRESET_VERSION = "5.0"


# Defaults are intentionally conservative and map one-for-one to constructor
# parameters in ai_privacy_redaction.PrivacyRedactionProcessor.
PRIVACY_BASE_DEFAULTS: dict[str, Any] = {
    "targets": ["face", "license_plate"],
    "mode": "pixelate",
    "pixel_block": 12,
    "fail_policy": "hold_last",
    "block_mode": "solid",
    "apply_redaction": True,
    "require_face_backend": False,
    "face_model": "",
    "face_detect_every": 2,
    "face_full_scan_every": 4,
    "face_max_dimension": 960,
    "face_score_threshold": 0.55,
    "face_nms_threshold": 0.30,
    "face_roi_scan": True,
    "person_head_fallback": True,
    "face_min_confidence": 0.55,
    "face_high_confidence": 0.82,
    "face_confirm_hits": 2,
    "face_confirm_max_missed": 2,
    "face_min_size_px": 12.0,
    "face_max_frame_fraction": 0.20,
    "person_model": "",
    "person_detect_every": 4,
    "person_min_confidence": 0.55,
    "person_head_high_confidence": 0.82,
    "person_head_min_hits": 2,
    "person_head_max_missed": 1,
    "person_min_height_px": 48.0,
    "max_predicted_face_age": 5,
    "face_expand_width": 1.28,
    "face_expand_height": 1.45,
    "max_person_rois": 8,
    "plate_model": "",
    "plate_detect_every": 3,
    "plate_require_vehicle": True,
    "vehicle_require_track_id": True,
    "vehicle_min_confidence": 0.35,
    "vehicle_min_width_px": 40.0,
    "vehicle_min_height_px": 24.0,
    "max_plate_candidates_per_vehicle": 1,
    "plate_candidate_min_score": 4.25,
    "plate_candidate_confirm_hits": 2,
    "plate_fallback_confirm_hits": 3,
    "plate_candidate_high_confidence": 0.80,
    "plate_candidate_match_iou": 0.22,
    "plate_candidate_center_gate": 0.35,
    "plate_candidate_max_missed_scans": 1,
    "plate_candidate_min_width_px": 14.0,
    "plate_candidate_min_height_px": 5.0,
    "plate_candidate_aspect_min": 1.60,
    "plate_candidate_aspect_max": 7.00,
    "plate_candidate_relative_width_min": 0.05,
    "plate_candidate_relative_width_max": 0.60,
    "plate_candidate_relative_height_min": 0.02,
    "plate_candidate_relative_height_max": 0.28,
    "plate_candidate_relative_y_min": 0.28,
    "plate_candidate_relative_y_max": 0.98,
    # recognized_only: OCR-confirmed only
    # confirmed_detected: temporally confirmed plate detector/proposal, OCR optional
    "plate_mask_policy": "confirmed_detected",
    "plate_fallback_mode": "when_model_unavailable",
    "plate_ocr_enabled": True,
    "plate_ocr_async": True,
    "plate_ocr_retry_interval": 8,
    "plate_ocr_quality_improvement": 0.10,
    "plate_ocr_max_attempts_per_vehicle": 8,
    "plate_ocr_min_confidence": 0.58,
    "plate_ocr_high_confidence": 0.82,
    "plate_confirm_hits": 2,
    "plate_region_profile": "us_generic",
    "save_recognized_plates": False,
    "plate_output_dir": "results/recognized_license_plates",
    "emit_plate_text": True,
    "emit_plate_candidates": False,
    "plate_expand_width": 1.18,
    "plate_expand_height": 1.35,
    "max_predicted_plate_age": 5,
    "max_plate_tracks": 64,
    "max_pending_ocr": 2,
    "auto_object_track": True,
    "object_track_params": {},
    "providers": [],
    "dnn_backend": "cpu",
    "require_plate_backend": False,
    "start_hits": 1,
    "max_missed_frames": 5,
    "max_masks": 96,
    "model_auto_discovery": True,
    "automatic_detection_required": False,
    "object_track_dependency_expected": True,
}


PRIVACY_PRESETS: dict[str, dict[str, Any]] = {
    "fast": {
        "fail_policy": "hold_last",
        "face_detect_every": 3,
        "face_full_scan_every": 6,
        "face_max_dimension": 720,
        "face_min_confidence": 0.62,
        "face_high_confidence": 0.86,
        "person_detect_every": 6,
        "person_min_confidence": 0.62,
        "person_head_min_hits": 2,
        "plate_detect_every": 4,
        "plate_candidate_min_score": 4.75,
        "plate_candidate_confirm_hits": 3,
        "plate_fallback_confirm_hits": 4,
        "plate_candidate_high_confidence": 0.88,
        "plate_mask_policy": "confirmed_detected",
        "max_predicted_face_age": 4,
        "max_predicted_plate_age": 4,
        "max_pending_ocr": 1,
    },
    "balanced": {
        "fail_policy": "hold_last",
        "face_detect_every": 2,
        "face_full_scan_every": 4,
        "face_max_dimension": 960,
        "person_detect_every": 4,
        "plate_detect_every": 3,
        "plate_mask_policy": "confirmed_detected",
        "plate_candidate_confirm_hits": 2,
        "plate_fallback_confirm_hits": 3,
        "plate_fallback_mode": "when_model_unavailable",
    },
    "strict": {
        "fail_policy": "block_frame",
        "require_face_backend": True,
        "require_plate_backend": True,
        "automatic_detection_required": True,
        "face_detect_every": 1,
        "face_full_scan_every": 2,
        "face_max_dimension": 1280,
        "face_min_confidence": 0.50,
        "face_high_confidence": 0.78,
        "person_detect_every": 2,
        "person_min_confidence": 0.50,
        "person_head_min_hits": 2,
        "plate_detect_every": 2,
        "plate_mask_policy": "confirmed_detected",
        "plate_candidate_min_score": 4.10,
        "plate_candidate_confirm_hits": 2,
        "plate_fallback_confirm_hits": 3,
        "plate_candidate_high_confidence": 0.78,
        "max_predicted_face_age": 8,
        "max_predicted_plate_age": 8,
        "max_missed_frames": 8,
    },
    "archive": {
        "fail_policy": "block_frame",
        "require_face_backend": True,
        "require_plate_backend": True,
        "automatic_detection_required": True,
        "face_detect_every": 1,
        "face_full_scan_every": 1,
        "face_max_dimension": 1440,
        "person_detect_every": 2,
        "plate_detect_every": 1,
        "plate_mask_policy": "confirmed_detected",
        "plate_candidate_confirm_hits": 2,
        "plate_fallback_confirm_hits": 3,
        "plate_ocr_async": False,
        "save_recognized_plates": True,
        "max_predicted_face_age": 10,
        "max_predicted_plate_age": 10,
        "max_missed_frames": 10,
    },
    "diagnostic": {
        "mode": "highlight",
        "apply_redaction": False,
        "fail_policy": "best_effort",
        "require_face_backend": False,
        "require_plate_backend": False,
        "face_detect_every": 1,
        "face_full_scan_every": 1,
        "person_detect_every": 1,
        "plate_detect_every": 1,
        "plate_mask_policy": "recognized_only",
        "plate_fallback_mode": "augment",
        "emit_plate_candidates": True,
        "save_recognized_plates": True,
    },
}


PRESET_DESCRIPTIONS = {
    "auto": "Choose balanced for live video, archive for offline video, and fast only for unusually high-rate/high-resolution live input.",
    "fast": "Lower detector cadence and higher evidence thresholds for constrained CPU deployments.",
    "balanced": "Recommended live default: model-backed detection, temporal confirmation, tracking, and bounded OCR.",
    "strict": "Fail closed when required detector models are unavailable; prioritizes privacy recall over latency.",
    "archive": "Recorded-video profile with every-frame scans and synchronous OCR for reproducible output.",
    "diagnostic": "Highlight and emit candidates without modifying pixels; intended for tuning only.",
}


_MODEL_SPECS: dict[str, dict[str, Any]] = {
    "face_model": {
        "env": "MEDIA_FORMAT_PRIVACY_FACE_MODEL",
        "filenames": [
            "face_detection_yunet_2026may.onnx",
            "face_detection_yunet_2023mar.onnx",
        ],
        "min_size": 100_000,
    },
    "person_model": {
        "env": "MEDIA_FORMAT_PRIVACY_PERSON_MODEL",
        "filenames": ["object_detection_yolox_2022nov.onnx"],
        "min_size": 1_000_000,
    },
    "plate_model": {
        "env": "MEDIA_FORMAT_PRIVACY_PLATE_MODEL",
        "filenames": [
            "yolo-v9-t-512-license-plates-end2end.onnx",
            "yolo-v9-t-640-license-plates-end2end.onnx",
            "yolo-v9-t-416-license-plates-end2end.onnx",
            "yolo-v9-t-384-license-plates-end2end.onnx",
            "license_plate_detection_lpd_yunet_2023mar.onnx",
        ],
        "min_size": 100_000,
    },
}


@dataclass(frozen=True)
class ModelDiscovery:
    role: str
    path: str
    source: str
    usable: bool
    reason: str
    size_bytes: int = 0
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "source": self.source,
            "usable": self.usable,
            "reason": self.reason,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_lfs_pointer(path: Path) -> bool:
    try:
        if path.stat().st_size > 4096:
            return False
        prefix = path.read_bytes()[:256]
        return prefix.startswith(b"version https://git-lfs.github.com/spec/v1")
    except OSError:
        return False


def inspect_model_file(role: str, path_value: str | os.PathLike[str] | None) -> ModelDiscovery:
    path_text = str(path_value or "").strip()
    if not path_text:
        return ModelDiscovery(role, "", "unconfigured", False, "not_configured")
    path = Path(path_text).expanduser()
    if not path.is_file():
        return ModelDiscovery(role, str(path), "configured", False, "model_missing")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return ModelDiscovery(role, str(path), "configured", False, f"stat_failed:{exc}")
    spec = _MODEL_SPECS.get(role, {})
    minimum = int(spec.get("min_size", 1))
    if _is_lfs_pointer(path):
        return ModelDiscovery(role, str(path), "configured", False, "git_lfs_pointer", size)
    if size < minimum:
        return ModelDiscovery(role, str(path), "configured", False, f"model_too_small:{size}<{minimum}", size)
    try:
        digest = _sha256(path)
    except OSError as exc:
        return ModelDiscovery(role, str(path), "configured", False, f"hash_failed:{exc}", size)
    return ModelDiscovery(role, str(path.resolve()), "configured", True, "", size, digest)


def _model_dirs(package_dir: Path | None, extra_dirs: Sequence[str | os.PathLike[str]] = ()) -> list[Path]:
    values: list[Path] = []
    for env_name in ("MEDIA_FORMAT_PRIVACY_MODEL_DIR", "MEDIA_FORMAT_AI_MODEL_DIR"):
        value = os.environ.get(env_name, "").strip()
        if value:
            values.append(Path(value).expanduser())
    values.extend(Path(value).expanduser() for value in extra_dirs if str(value).strip())
    if package_dir is not None:
        values.extend([package_dir / "models", package_dir])
    values.append(default_model_dir())
    values.extend([Path.cwd() / "models", Path.cwd()])
    output: list[Path] = []
    seen: set[str] = set()
    for path in values:
        key = str(path.resolve()) if path.exists() else str(path.absolute())
        if key in seen:
            continue
        seen.add(key)
        output.append(path)
    return output


def discover_privacy_models(
    *,
    package_dir: Path | None = None,
    configured: Mapping[str, Any] | None = None,
    extra_dirs: Sequence[str | os.PathLike[str]] = (),
) -> dict[str, ModelDiscovery]:
    configured = dict(configured or {})
    directories = _model_dirs(package_dir, extra_dirs)
    output: dict[str, ModelDiscovery] = {}
    for role, spec in _MODEL_SPECS.items():
        explicit = str(configured.get(role, "") or "").strip()
        env_value = os.environ.get(str(spec["env"]), "").strip()
        if explicit:
            candidate = Path(explicit).expanduser()
            if not candidate.is_absolute() and not candidate.is_file():
                for directory in directories:
                    local = directory / candidate
                    if not local.is_file() and candidate.parent == Path("models"):
                        local = directory / candidate.name
                    if local.is_file():
                        candidate = local
                        break
            result = inspect_model_file(role, candidate)
            output[role] = ModelDiscovery(result.role, result.path, "explicit", result.usable, result.reason, result.size_bytes, result.sha256)
            continue
        if env_value:
            result = inspect_model_file(role, env_value)
            output[role] = ModelDiscovery(result.role, result.path, "environment", result.usable, result.reason, result.size_bytes, result.sha256)
            continue
        found: ModelDiscovery | None = None
        for directory in directories:
            for filename in spec["filenames"]:
                candidate = directory / filename
                result = inspect_model_file(role, candidate)
                if result.usable:
                    found = ModelDiscovery(result.role, result.path, f"auto:{directory}", result.usable, result.reason, result.size_bytes, result.sha256)
                    break
            if found is not None:
                break
        output[role] = found or ModelDiscovery(role, "", "auto", False, "model_not_found")
    return output


def _flatten_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the documented nested JSON format into constructor keys."""

    output: dict[str, Any] = {}
    direct = set(PRIVACY_BASE_DEFAULTS) | {
        "preset",
        "model_auto_discovery",
        "automatic_detection_required",
        "offline",
        "source_width",
        "source_height",
        "source_fps",
    }
    for key, value in data.items():
        if key in direct:
            output[key] = value

    models = data.get("models")
    if isinstance(models, Mapping):
        for short, key in (("face", "face_model"), ("person", "person_model"), ("plate", "plate_model")):
            if short in models:
                output[key] = models[short]

    groups: dict[str, dict[str, str]] = {
        "face": {
            "detect_every": "face_detect_every",
            "full_scan_every": "face_full_scan_every",
            "max_dimension": "face_max_dimension",
            "score_threshold": "face_score_threshold",
            "min_confidence": "face_min_confidence",
            "high_confidence": "face_high_confidence",
            "confirm_hits": "face_confirm_hits",
            "max_predicted_age": "max_predicted_face_age",
            "expand_width": "face_expand_width",
            "expand_height": "face_expand_height",
        },
        "person": {
            "detect_every": "person_detect_every",
            "min_confidence": "person_min_confidence",
            "head_fallback": "person_head_fallback",
            "head_min_hits": "person_head_min_hits",
            "head_high_confidence": "person_head_high_confidence",
            "min_height_px": "person_min_height_px",
        },
        "plate": {
            "detect_every": "plate_detect_every",
            "mask_policy": "plate_mask_policy",
            "fallback_mode": "plate_fallback_mode",
            "max_candidates_per_vehicle": "max_plate_candidates_per_vehicle",
            "candidate_min_score": "plate_candidate_min_score",
            "candidate_confirm_hits": "plate_candidate_confirm_hits",
            "fallback_confirm_hits": "plate_fallback_confirm_hits",
            "candidate_high_confidence": "plate_candidate_high_confidence",
            "ocr_enabled": "plate_ocr_enabled",
            "ocr_async": "plate_ocr_async",
            "ocr_min_confidence": "plate_ocr_min_confidence",
            "ocr_high_confidence": "plate_ocr_high_confidence",
            "save_recognized": "save_recognized_plates",
            "output_dir": "plate_output_dir",
            "max_predicted_age": "max_predicted_plate_age",
        },
        "vehicle": {
            "require_track_id": "vehicle_require_track_id",
            "min_confidence": "vehicle_min_confidence",
            "min_width_px": "vehicle_min_width_px",
            "min_height_px": "vehicle_min_height_px",
        },
        "output": {
            "mode": "mode",
            "pixel_block": "pixel_block",
            "fail_policy": "fail_policy",
            "block_mode": "block_mode",
            "apply_redaction": "apply_redaction",
        },
    }
    for group, mapping in groups.items():
        value = data.get(group)
        if not isinstance(value, Mapping):
            continue
        for short, key in mapping.items():
            if short in value:
                output[key] = value[short]
    return output


def load_privacy_config(path_value: str | os.PathLike[str] | None) -> tuple[dict[str, Any], str]:
    path_text = str(path_value or "").strip()
    if not path_text:
        return {}, ""
    path = Path(path_text).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"privacy config not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError("privacy config must contain a JSON object")
    return _flatten_config(data), str(path.resolve())


def choose_auto_preset(*, offline: bool, source_width: int, source_height: int, source_fps: float) -> str:
    if offline:
        return "archive"
    pixels = max(0, int(source_width)) * max(0, int(source_height))
    if source_fps > 35.0 or pixels > 3_500_000:
        return "fast"
    return "balanced"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def resolve_privacy_configuration(
    *,
    preset: str = "auto",
    config_path: str | os.PathLike[str] | None = None,
    explicit: Mapping[str, Any] | None = None,
    package_dir: Path | None = None,
    model_auto_discovery: bool = True,
    source_width: int = 0,
    source_height: int = 0,
    source_fps: float = 0.0,
    offline: bool = False,
) -> dict[str, Any]:
    file_values, resolved_path = load_privacy_config(config_path)
    requested_input = str(preset or "auto").strip().lower()
    requested = str(
        (explicit or {}).get("preset")
        or (requested_input if requested_input != "auto" else "")
        or file_values.get("preset")
        or os.environ.get("MEDIA_FORMAT_PRIVACY_PRESET", "auto")
        or "auto"
    ).strip().lower()
    if requested not in set(PRIVACY_PRESETS) | {"auto"}:
        raise ValueError(f"unknown privacy preset {requested!r}; expected one of auto, {', '.join(sorted(PRIVACY_PRESETS))}")
    selected = choose_auto_preset(
        offline=bool(offline or file_values.get("offline", False)),
        source_width=int(source_width or file_values.get("source_width", 0) or 0),
        source_height=int(source_height or file_values.get("source_height", 0) or 0),
        source_fps=float(source_fps or file_values.get("source_fps", 0.0) or 0.0),
    ) if requested == "auto" else requested

    config = dict(PRIVACY_BASE_DEFAULTS)
    config.update(PRIVACY_PRESETS[selected])
    config.update({key: value for key, value in file_values.items() if key in config and value is not None})
    for key, value in dict(explicit or {}).items():
        if key in config and value is not None:
            config[key] = value

    auto_models = bool(
        (explicit or {}).get("model_auto_discovery")
        if (explicit or {}).get("model_auto_discovery") is not None
        else file_values.get("model_auto_discovery", model_auto_discovery)
    )
    discoveries = discover_privacy_models(package_dir=package_dir, configured=config) if auto_models else {
        role: inspect_model_file(role, config.get(role, "")) for role in _MODEL_SPECS
    }
    for role, result in discoveries.items():
        if result.usable:
            config[role] = result.path

    policy = str(config.get("plate_mask_policy", "confirmed_detected") or "confirmed_detected").lower()
    if policy == "all_detected":
        policy = "confirmed_detected"  # V4 compatibility spelling.
    if policy not in {"recognized_only", "confirmed_detected"}:
        raise ValueError("plate_mask_policy must be recognized_only or confirmed_detected")
    config["plate_mask_policy"] = policy

    fallback_mode = str(config.get("plate_fallback_mode", "when_model_unavailable") or "when_model_unavailable").lower()
    if fallback_mode not in {"disabled", "when_model_unavailable", "augment"}:
        raise ValueError("plate_fallback_mode must be disabled, when_model_unavailable, or augment")
    config["plate_fallback_mode"] = fallback_mode

    targets_value = config.get("targets", ["face", "license_plate"])
    if isinstance(targets_value, str):
        targets = [item.strip() for item in targets_value.split(",") if item.strip()]
    else:
        targets = [str(item).strip() for item in targets_value if str(item).strip()]
    config["targets"] = targets

    # Re-inspect final selected paths, because explicit paths may have replaced
    # discoveries after the first pass.
    final_models: dict[str, ModelDiscovery] = {}
    for role in _MODEL_SPECS:
        inspected = inspect_model_file(role, config.get(role, ""))
        original = discoveries.get(role)
        source = original.source if original is not None and original.path == inspected.path else inspected.source
        final_models[role] = ModelDiscovery(
            inspected.role, inspected.path, source, inspected.usable, inspected.reason, inspected.size_bytes, inspected.sha256
        )
    tesseract_python = importlib.util.find_spec("pytesseract") is not None
    tesseract_binary = shutil.which("tesseract") or ""
    ocr_ready = bool(tesseract_python and tesseract_binary)
    object_track_expected = bool(config.get("object_track_dependency_expected", True) or config.get("auto_object_track", False))

    missing: list[str] = []
    if "face" in targets and not final_models["face_model"].usable:
        missing.append("face_model")
    if "face" in targets and bool(config.get("person_head_fallback", True)):
        if not (final_models["person_model"].usable or object_track_expected):
            missing.append("person_context")
    if "license_plate" in targets and not final_models["plate_model"].usable:
        missing.append("plate_model")
    if "license_plate" in targets and not object_track_expected:
        missing.append("object_track")
    if "license_plate" in targets and policy == "recognized_only" and (
        not bool(config.get("plate_ocr_enabled", True)) or not ocr_ready
    ):
        missing.append("plate_ocr")

    required = bool(
        (explicit or {}).get("automatic_detection_required")
        if (explicit or {}).get("automatic_detection_required") is not None
        else config.get("automatic_detection_required", False)
    )
    config["automatic_detection_required"] = required
    config["model_auto_discovery"] = auto_models

    digest_source = json.dumps(_jsonable(config), sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(digest_source).hexdigest()
    return {
        "requested_preset": requested,
        "selected_preset": selected,
        "preset_version": PRIVACY_PRESET_VERSION,
        "config_path": resolved_path,
        "config": config,
        "model_discovery": {role: result.to_dict() for role, result in final_models.items()},
        "ocr": {
            "python_adapter": tesseract_python,
            "binary": tesseract_binary,
            "ready": ocr_ready,
        },
        "object_track_expected": object_track_expected,
        "automatic_detection_ready_static": not missing,
        "automatic_detection_missing_static": missing,
        "automatic_detection_required": required,
        "config_digest": digest,
    }


def write_effective_config(path_value: str | os.PathLike[str], resolved: Mapping[str, Any]) -> None:
    path = Path(path_value).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(dict(resolved)), indent=2, sort_keys=True) + "\n", encoding="utf-8")
