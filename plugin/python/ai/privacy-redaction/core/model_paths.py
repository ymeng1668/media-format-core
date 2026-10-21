"""Model path resolution for Python AI processors."""

from __future__ import annotations

import os
from pathlib import Path


_REMOTE_PREFIXES = (
    "http://",
    "https://",
    "hf://",
    "s3://",
    "gs://",
)


def default_model_dir() -> Path:
    configured = os.environ.get("MEDIA_FORMAT_AI_MODEL_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "media-format-core" / "ai" / "models"


def resolve_model_ref(model: str | os.PathLike[str] | None) -> str:
    """Resolve local model filenames without writing into the process cwd.

    Ultralytics downloads bare model names such as ``yolov8n.pt`` into the
    current working directory. Local model names are redirected to a stable
    model cache shared by native and Python hosts.
    Remote model identifiers, repository IDs, and explicit paths are preserved.
    """

    if model is None:
        return ""
    value = str(model).strip()
    if not value:
        return ""
    lower = value.lower()
    if lower.startswith(_REMOTE_PREFIXES):
        return value
    path = Path(value).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        return str(path)
    if "/" in value or "\\" in value:
        return value
    if not lower.endswith((".pt", ".onnx", ".engine", ".torchscript")):
        return value
    model_dir = default_model_dir()
    model_dir.mkdir(parents=True, exist_ok=True)
    return str(model_dir / value)
