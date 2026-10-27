"""Small, lazy PaddleOCR adapter used by the existing OCR pipeline.

The adapter deliberately exposes the same text/layout shape as the previous
engine so field extraction, review, and persistence remain unchanged. Import
and model construction are lazy because local development may not have the
CPU Paddle runtime installed.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from PIL import Image

_lock = threading.Lock()
_engine: Any | None = None
_init_error: Exception | None = None


def enabled() -> bool:
    return os.getenv("PADDLEOCR_ENABLED", "true").strip().lower() not in {"0", "false", "no"}


def _create_engine() -> Any:
    from paddleocr import PaddleOCR

    language = os.getenv("PADDLEOCR_LANGUAGES", "en").strip() or "en"
    try:
        return PaddleOCR(
            lang=language,
            device=os.getenv("PADDLEOCR_DEVICE", "cpu"),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=os.getenv("PADDLEOCR_USE_ANGLE_CLASSIFIER", "true").lower() == "true",
        )
    except TypeError:
        # PaddleOCR 2.x compatibility for environments that already pin it.
        return PaddleOCR(
            lang=language,
            use_angle_cls=os.getenv("PADDLEOCR_USE_ANGLE_CLASSIFIER", "true").lower() == "true",
            show_log=False,
        )


def get_engine() -> Any:
    global _engine, _init_error
    if _engine is not None:
        return _engine
    if _init_error is not None:
        raise RuntimeError("PaddleOCR is unavailable") from _init_error
    with _lock:
        if _engine is None and _init_error is None:
            try:
                _engine = _create_engine()
            except Exception as exc:  # keep the existing fallback available
                _init_error = exc
                raise RuntimeError("PaddleOCR is unavailable") from exc
    return _engine


def available() -> bool:
    if not enabled():
        return False
    try:
        get_engine()
        return True
    except Exception:
        return False


def warm() -> bool:
    """Initialize the model once so the first user scan does not pay startup cost."""
    return available()


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raw = getattr(value, "json", None)
    if callable(raw):
        raw = raw()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, dict):
        return raw.get("res", raw)
    return {}


def _v3_lines(result: Any) -> list[tuple[str, int]]:
    data = _as_mapping(result)
    texts = data.get("rec_texts") or data.get("texts") or []
    boxes = data.get("rec_boxes") or data.get("dt_polys") or []
    lines: list[tuple[str, int]] = []
    for index, value in enumerate(texts):
        text = str(value).strip()
        if not text:
            continue
        height = 0
        if index < len(boxes):
            try:
                points = boxes[index]
                ys = [float(point[1]) for point in points]
                height = max(0, round(max(ys) - min(ys)))
            except (TypeError, ValueError, IndexError):
                height = 0
        lines.append((text, height))
    return lines


def _legacy_lines(result: Any) -> list[tuple[str, int]]:
    lines: list[tuple[str, int]] = []
    groups = result if isinstance(result, list) else []
    if groups and isinstance(groups[0], list) and groups[0] and isinstance(groups[0][0], list):
        groups = groups[0]
    for item in groups:
        try:
            box, value, _score = item
            text = str(value).strip()
            ys = [float(point[1]) for point in box]
            if text:
                lines.append((text, max(0, round(max(ys) - min(ys)))))
        except (TypeError, ValueError, IndexError):
            continue
    return lines


def read(image: Image.Image, *, language: str | None = None) -> tuple[str, dict[str, int]]:
    """Return OCR text and line heights using PaddleOCR's available API."""
    engine = get_engine()
    rgb = image.convert("RGB")
    try:
        import numpy as np
        source = np.asarray(rgb)
    except ImportError as exc:
        raise RuntimeError("PaddleOCR requires numpy") from exc

    if language and language != os.getenv("PADDLEOCR_LANGUAGES", "en"):
        # A second engine is intentionally not created per request. The caller
        # can use its configured primary language or the existing fallback.
        raise RuntimeError("PaddleOCR language is not configured")
    if hasattr(engine, "predict"):
        results = engine.predict(source)
        result = next(iter(results), None)
        lines = _v3_lines(result) if result is not None else []
    else:
        results = engine.ocr(source, cls=True)
        lines = _legacy_lines(results)
    text = "\n".join(item[0] for item in lines).strip()
    heights = {item[0].casefold(): item[1] for item in lines if item[1]}
    return text, heights
