"""Versioned media-format Python AI result contracts.

Validation in this module is deliberately total: malformed data returns a
validation error and never raises from the validator.  The C++ bridge can use
this as a hard boundary before serializing or forwarding Python results.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Literal, NotRequired, TypedDict


EVENT_KINDS = frozenset({"box", "track", "text", "interval", "score", "mask", "media"})


EVENT_SCHEMA: dict[str, Any] = {
    "schema": "media_format.ai.event.v1",
    "required": ["schema", "kind", "label", "confidence", "valid"],
    "kinds": sorted(EVENT_KINDS),
    "confidence_range": [0.0, 1.0],
}

FRAME_RESULT_SCHEMA: dict[str, Any] = {
    "schema": "media_format.ai.frame_result.v1",
    "required": [
        "schema", "algorithm", "time", "frame_index", "processed", "valid",
        "degraded", "reason", "events", "num_events", "metrics", "provenance",
    ],
}
_FRAME_RESERVED = frozenset({
    "schema", "algorithm", "time", "frame_index", "processed", "valid",
    "degraded", "reason", "events", "num_events", "media", "metrics",
    "provenance",
})


class BBox(TypedDict):
    x: float
    y: float
    w: float
    h: float
    normalized: NotRequired[bool]


class TrackInfo(TypedDict, total=False):
    id: int
    age: int
    missed: int
    state: str


class TextInfo(TypedDict, total=False):
    value: str
    normalized: str
    language: str
    ocr_confidence: float


class AIEvent(TypedDict, total=False):
    schema: str
    event_id: str
    kind: Literal["box", "track", "text", "interval", "score", "mask", "media"]
    label: str
    confidence: float
    time: float
    start_time: float
    end_time: float
    bbox: BBox
    polygon: list[list[float]]
    track: TrackInfo
    text: TextInfo
    value: float
    unit: str
    valid: bool
    reason: str
    attributes: dict[str, Any]


class FrameResult(TypedDict, total=False):
    schema: str
    algorithm: str
    time: float
    frame_index: int
    processed: bool
    valid: bool
    degraded: bool
    reason: str
    events: list[AIEvent]
    num_events: int
    media: Any
    metrics: dict[str, Any]
    provenance: dict[str, Any]


class ProcessorDescription(TypedDict, total=False):
    schema: str
    algorithm: str
    task: str
    maturity: str
    media: list[str]
    input: dict[str, Any]
    outputs: list[str]
    requires_results: list[str]
    can_modify_media: bool
    state_scope: str
    threading: str
    dependencies: list[str]
    privacy: dict[str, bool]
    params: dict[str, Any]


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _json_safe(value: Any, *, allow_ndarray: bool = False, depth: int = 0) -> bool:
    if depth > 32:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if allow_ndarray and hasattr(value, "shape") and hasattr(value, "dtype"):
        return True
    if isinstance(value, (list, tuple)):
        return all(_json_safe(v, allow_ndarray=allow_ndarray, depth=depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _json_safe(v, allow_ndarray=allow_ndarray, depth=depth + 1) for k, v in value.items())
    return False


def frame_result(
    *,
    algorithm: str,
    time: float,
    frame_index: int = 0,
    processed: bool = True,
    valid: bool = True,
    degraded: bool = False,
    reason: str = "",
    events: list[AIEvent] | None = None,
    metrics: dict[str, Any] | None = None,
    media: Any = None,
    provenance: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> FrameResult:
    event_list = list(events or [])
    result: FrameResult = {
        "schema": "media_format.ai.frame_result.v1",
        "algorithm": str(algorithm),
        "time": float(time),
        "frame_index": int(frame_index),
        "processed": bool(processed),
        "valid": bool(valid),
        "degraded": bool(degraded),
        "reason": str(reason),
        "events": event_list,
        "num_events": len(event_list),
        "media": media,
        "metrics": dict(metrics or {}),
        "provenance": dict(provenance or {}),
    }
    if extra:
        collisions = sorted(_FRAME_RESERVED.intersection(extra))
        if collisions:
            raise ValueError(f"frame_result extra cannot overwrite reserved fields: {', '.join(collisions)}")
        result.update(dict(extra))
    return result


def event(
    *,
    kind: AIEvent["kind"],
    label: str,
    confidence: float = 0.0,
    event_id: str = "",
    time: float | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
    bbox: BBox | None = None,
    polygon: list[list[float]] | None = None,
    track: TrackInfo | None = None,
    text: TextInfo | None = None,
    value: float | None = None,
    unit: str | None = None,
    valid: bool = True,
    reason: str = "",
    attributes: dict[str, Any] | None = None,
) -> AIEvent:
    kind_value = str(kind)
    if kind_value not in EVENT_KINDS:
        raise ValueError(f"unsupported media-format AI event kind: {kind_value!r}")
    raw_confidence = float(confidence)
    clipped_confidence = float(min(1.0, max(0.0, raw_confidence))) if math.isfinite(raw_confidence) else 0.0
    attrs = dict(attributes or {})
    if math.isfinite(raw_confidence) and raw_confidence != clipped_confidence:
        attrs.setdefault("confidence_clipped_from", raw_confidence)
    out: AIEvent = {
        "schema": "media_format.ai.event.v1",
        "kind": kind_value,  # type: ignore[typeddict-item]
        "label": str(label),
        "confidence": clipped_confidence,
        "valid": bool(valid),
        "reason": str(reason),
    }
    if event_id:
        out["event_id"] = str(event_id)
    if time is not None:
        out["time"] = float(time)
    if start_time is not None:
        out["start_time"] = float(start_time)
    if end_time is not None:
        out["end_time"] = float(end_time)
    if bbox is not None:
        out["bbox"] = bbox
    if polygon is not None:
        out["polygon"] = [[float(p[0]), float(p[1])] for p in polygon]
    if track is not None:
        out["track"] = track
    if text is not None:
        out["text"] = text
    if value is not None:
        out["value"] = float(value)
    if unit is not None:
        out["unit"] = str(unit)
    if attrs:
        out["attributes"] = attrs
    return out


def bbox(x: float, y: float, w: float, h: float, *, normalized: bool = False) -> BBox:
    values = [float(x), float(y), float(w), float(h)]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("bbox coordinates must be finite")
    x_value, y_value, width, height = values
    if width < 0.0 or height < 0.0:
        raise ValueError("bbox width and height must be non-negative")
    if normalized and not (
        0.0 <= x_value <= 1.0
        and 0.0 <= y_value <= 1.0
        and 0.0 <= width <= 1.0
        and 0.0 <= height <= 1.0
        and x_value + width <= 1.000001
        and y_value + height <= 1.000001
    ):
        raise ValueError("normalized bbox must stay within [0,1]")
    if not normalized and (x_value < 0.0 or y_value < 0.0):
        raise ValueError("pixel bbox coordinates must be non-negative")
    box: BBox = {"x": x_value, "y": y_value, "w": width, "h": height}
    if normalized:
        box["normalized"] = True
    return box


def sample_skip_result(*, algorithm: str, time: float, frame_index: int = 0) -> FrameResult:
    return frame_result(
        algorithm=algorithm,
        time=time,
        frame_index=frame_index,
        processed=False,
        valid=True,
        reason="sample_skip",
    )


def process_meta(
    meta_or_pts: Any = None,
    *,
    default_time: float = 0.0,
    default_frame_index: int = 0,
) -> tuple[float, int, dict[str, Any]]:
    """Normalize media-format frame metadata or the legacy float PTS value."""
    if isinstance(meta_or_pts, dict):
        meta = dict(meta_or_pts)
        time_value = meta.get("time", meta.get("pts", default_time))
        frame_index = meta.get("frame_index", default_frame_index)
    else:
        meta = {}
        time_value = default_time if meta_or_pts is None else meta_or_pts
        frame_index = default_frame_index
    try:
        t = float(time_value)
        if not math.isfinite(t):
            raise ValueError("non-finite time")
    except Exception:
        t = float(default_time)
    try:
        idx = int(frame_index)
    except Exception:
        idx = int(default_frame_index)
    meta.setdefault("schema", "media_format.ai.frame_context.v1")
    meta.setdefault("time", t)
    meta.setdefault("pts", t)
    meta.setdefault("frame_index", idx)
    meta.setdefault("stream_id", "default")
    meta.setdefault("segment_id", "0")
    meta.setdefault("discontinuity_seq", 0)
    meta.setdefault("upstream", {})
    return t, idx, meta


def normalize_frame_result(
    result: dict[str, Any] | None,
    *,
    algorithm: str,
    time: float,
    frame_index: int = 0,
    processed: bool = True,
    valid: bool = True,
    degraded: bool = False,
    reason: str = "",
    events: Iterable[AIEvent] | None = None,
    provenance: dict[str, Any] | None = None,
) -> FrameResult:
    extra = dict(result or {})
    existing_events = extra.pop("events", None)
    merged_events = list(events if events is not None else (existing_events or []))
    existing_metrics = extra.pop("metrics", None)
    existing_media = extra.pop("media", None)
    existing_provenance = extra.pop("provenance", None)
    for key in _FRAME_RESERVED:
        extra.pop(key, None)
    return frame_result(
        algorithm=algorithm,
        time=time,
        frame_index=frame_index,
        processed=processed,
        valid=valid,
        degraded=degraded,
        reason=reason,
        events=merged_events,
        metrics=existing_metrics if isinstance(existing_metrics, dict) else None,
        media=existing_media,
        provenance=provenance or (existing_provenance if isinstance(existing_provenance, dict) else None),
        extra=extra,
    )


def _validate_bbox(raw: Any, prefix: str) -> tuple[bool, str]:
    if not isinstance(raw, dict):
        return False, f"{prefix} bbox must be a dict"
    for key in ("x", "y", "w", "h"):
        if not _finite_number(raw.get(key)):
            return False, f"{prefix} bbox.{key} must be a finite number"
    x, y, w, h = [float(raw[k]) for k in ("x", "y", "w", "h")]
    if w < 0.0 or h < 0.0:
        return False, f"{prefix} bbox width/height must be non-negative"
    normalized = raw.get("normalized", False)
    if not isinstance(normalized, bool):
        return False, f"{prefix} bbox.normalized must be bool"
    if normalized and not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 <= w <= 1.0 and 0.0 <= h <= 1.0 and x + w <= 1.000001 and y + h <= 1.000001):
        return False, f"{prefix} normalized bbox must stay within [0,1]"
    if not normalized and (x < 0.0 or y < 0.0):
        return False, f"{prefix} pixel bbox coordinates must be non-negative"
    return True, ""


def _validate_event(item: Any, idx: int, *, strict: bool) -> tuple[bool, str]:
    prefix = f"event {idx}"
    if not isinstance(item, dict):
        return False, f"{prefix} is not a dict"
    if item.get("schema") != "media_format.ai.event.v1":
        return False, f"{prefix} missing media_format.ai.event.v1 schema"
    kind = item.get("kind")
    if kind not in EVENT_KINDS:
        return False, f"{prefix} kind is invalid: {kind!r}"
    if not isinstance(item.get("label"), str) or not item.get("label"):
        return False, f"{prefix} label must be a non-empty string"
    if not isinstance(item.get("valid"), bool):
        return False, f"{prefix} valid must be bool"
    if not isinstance(item.get("reason", ""), str):
        return False, f"{prefix} reason must be string"
    confidence = item.get("confidence")
    if not _finite_number(confidence) or not 0.0 <= float(confidence) <= 1.0:
        return False, f"{prefix} confidence must be finite and within [0,1]"
    for key in ("time", "start_time", "end_time", "value"):
        if key in item and not _finite_number(item[key]):
            return False, f"{prefix} {key} must be a finite number"
    if "bbox" in item:
        ok, reason = _validate_bbox(item["bbox"], prefix)
        if not ok:
            return ok, reason
    if kind in {"box", "track", "text", "mask"} and "bbox" not in item and "polygon" not in item:
        return False, f"{prefix} kind {kind!r} requires bbox or polygon"
    if kind == "track":
        track = item.get("track")
        if not isinstance(track, dict) or not isinstance(track.get("id"), int) or isinstance(track.get("id"), bool):
            return False, f"{prefix} track event requires integer track.id"
        if "age" in track and (not isinstance(track["age"], int) or isinstance(track["age"], bool) or track["age"] < 0):
            return False, f"{prefix} track.age must be a non-negative integer"
        if "missed" in track and (not isinstance(track["missed"], int) or isinstance(track["missed"], bool) or track["missed"] < 0):
            return False, f"{prefix} track.missed must be a non-negative integer"
    if kind == "text":
        text = item.get("text")
        if not isinstance(text, dict) or not isinstance(text.get("value"), str):
            return False, f"{prefix} text event requires text.value string"
        if "ocr_confidence" in text and (not _finite_number(text["ocr_confidence"]) or not 0.0 <= float(text["ocr_confidence"]) <= 1.0):
            return False, f"{prefix} text.ocr_confidence must be within [0,1]"
    if kind == "interval":
        if not _finite_number(item.get("start_time")) or not _finite_number(item.get("end_time")):
            return False, f"{prefix} interval requires finite start_time and end_time"
        if float(item["end_time"]) < float(item["start_time"]):
            return False, f"{prefix} interval end_time precedes start_time"
    if "polygon" in item:
        polygon = item["polygon"]
        if not isinstance(polygon, list) or len(polygon) < 2:
            return False, f"{prefix} polygon must contain at least two points"
        for point in polygon:
            if not isinstance(point, (list, tuple)) or len(point) < 2 or not _finite_number(point[0]) or not _finite_number(point[1]):
                return False, f"{prefix} polygon contains an invalid point"
    if "attributes" in item and not isinstance(item["attributes"], dict):
        return False, f"{prefix} attributes must be a dict"
    if strict and not _json_safe(item):
        return False, f"{prefix} contains non-JSON-safe or non-finite data"
    return True, ""


def validate_frame_result(result: Any, *, strict: bool = False) -> tuple[bool, str]:
    """Validate a result without ever raising, even for adversarial input."""
    try:
        if not isinstance(result, dict):
            return False, "result is not a dict"
        if result.get("schema") != "media_format.ai.frame_result.v1":
            return False, "missing media_format.ai.frame_result.v1 schema"
        if not isinstance(result.get("algorithm"), str) or not result.get("algorithm"):
            return False, "algorithm must be a non-empty string"
        if not _finite_number(result.get("time")):
            return False, "time must be a finite number"
        frame_index = result.get("frame_index")
        if not isinstance(frame_index, int) or isinstance(frame_index, bool):
            return False, "frame_index must be an integer"
        for key in ("processed", "valid", "degraded"):
            if not isinstance(result.get(key), bool):
                return False, f"{key} must be bool"
        if not isinstance(result.get("reason", ""), str):
            return False, "reason must be string"
        events = result.get("events")
        if not isinstance(events, list):
            return False, "events must be a list"
        num_events = result.get("num_events")
        if not isinstance(num_events, int) or isinstance(num_events, bool):
            return False, "num_events must be an integer"
        if num_events != len(events):
            return False, "num_events does not match events"
        if "metrics" in result and not isinstance(result["metrics"], dict):
            return False, "metrics must be a dict"
        if "provenance" in result and not isinstance(result["provenance"], dict):
            return False, "provenance must be a dict"
        for idx, item in enumerate(events):
            ok, reason = _validate_event(item, idx, strict=strict)
            if not ok:
                return ok, reason
        if strict:
            safe_copy = {k: v for k, v in result.items() if k not in {"media", "frame"}}
            if not _json_safe(safe_copy):
                return False, "frame result contains non-JSON-safe or non-finite data"
        return True, ""
    except Exception as exc:  # defensive boundary; validator must be total
        return False, f"validation_error:{type(exc).__name__}:{exc}"
