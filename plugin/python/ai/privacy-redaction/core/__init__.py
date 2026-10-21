"""Only the contracts and preprocessing needed by the privacy plugin."""

from .contracts import bbox, event, frame_result, normalize_frame_result, process_meta, validate_frame_result


def __getattr__(name):
    if name in {"AIPreprocessProfile", "StableConditionController", "ensure_frame_context"}:
        from . import preprocess
        value = getattr(preprocess, name)
        globals()[name] = value
        return value
    raise AttributeError(name)
