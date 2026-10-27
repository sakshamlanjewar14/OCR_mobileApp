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
import time
from typing import Any

from PIL import Image

_lock = threading.Lock()
_engines: dict[str, Any] = {}
_errors: dict[str, Exception] = {}
_init_times: dict[str, int] = {}
_inference_lock = threading.Lock()


def _language(value: str | None) -> str:
    language = value or os.getenv("PADDLEOCR_LANGUAGES", "en").strip() or "en"
    if language in {"hi", "mr", "hin", "mar", "hin+mar", "devanagari"}: return "devanagari"
    if language in {"eng", "en"}: return "en"
    raise ValueError("Configure a supported OCR language: en or devanagari")


def enabled() -> bool:
    return os.getenv("PADDLEOCR_ENABLED", "true").strip().lower() not in {"0", "false", "no"}


def _create_engine(language: str | None = None) -> Any:
    from paddleocr import PaddleOCR

    language = _language(language)
    runtime_language = "hi" if language == "devanagari" else "en"
    model = "devanagari_PP-OCRv5_mobile_rec" if language == "devanagari" else "en_PP-OCRv5_mobile_rec"
    model = os.getenv("PADDLEOCR_INDIC_RECOGNITION_MODEL" if language == "devanagari" else "PADDLEOCR_RECOGNITION_MODEL", model)
    paths = {}
    for option, variable in [("text_detection_model_dir", "PADDLEOCR_DETECTION_MODEL_DIR"), ("text_recognition_model_dir", "PADDLEOCR_INDIC_MODEL_DIR" if language == "devanagari" else "PADDLEOCR_RECOGNITION_MODEL_DIR")]:
        if os.getenv(variable): paths[option] = os.environ[variable]
    try:
        return PaddleOCR(
            lang=runtime_language,
            device=os.getenv("PADDLEOCR_DEVICE", "cpu"),
            ocr_version="PP-OCRv5",
            text_detection_model_name=os.getenv("PADDLEOCR_DETECTION_MODEL", "PP-OCRv5_mobile_det"),
            text_recognition_model_name=model,
            cpu_threads=max(1, int(os.getenv("PADDLEOCR_CPU_THREADS", "2"))),
            use_doc_orientation_classify=os.getenv("PADDLEOCR_DOC_ORIENTATION", "true").lower() == "true",
            use_doc_unwarping=False,
            use_textline_orientation=os.getenv("PADDLEOCR_USE_ANGLE_CLASSIFIER", "true").lower() == "true",
            **paths,
        )
    except TypeError:
        # PaddleOCR 2.x compatibility for environments that already pin it.
        return PaddleOCR(
            lang=runtime_language,
            use_angle_cls=os.getenv("PADDLEOCR_USE_ANGLE_CLASSIFIER", "true").lower() == "true",
            show_log=False,
        )


def get_engine(language: str | None = None) -> Any:
    language = _language(language)
    if language in _engines: return _engines[language]
    if language in _errors: raise RuntimeError("PaddleOCR is unavailable") from _errors[language]
    with _lock:
        if language not in _engines and language not in _errors:
            started = time.perf_counter()
            try:
                _engines[language] = _create_engine(language)
            except Exception as exc:  # keep the existing fallback available
                _errors[language] = exc
                raise RuntimeError("PaddleOCR is unavailable") from exc
            finally:
                _init_times[language] = round((time.perf_counter() - started) * 1000)
    if language in _errors: raise RuntimeError("PaddleOCR is unavailable") from _errors[language]
    return _engines[language]


def diagnostics() -> dict[str, Any]:
    return {"loaded_languages": list(_engines), "failed_languages": list(_errors), "initialization_ms": dict(_init_times),
            "detection_model": os.getenv("PADDLEOCR_DETECTION_MODEL", "PP-OCRv5_mobile_det"),
            "recognition_model": os.getenv("PADDLEOCR_RECOGNITION_MODEL", "en_PP-OCRv5_mobile_rec"),
            "indic_model": os.getenv("PADDLEOCR_INDIC_RECOGNITION_MODEL", "devanagari_PP-OCRv5_mobile_rec")}


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
    ready = available()
    if ready and os.getenv("PADDLEOCR_PRELOAD_INDIC", "false").lower() == "true":
        try: get_engine("devanagari")
        except RuntimeError: return False
    return ready


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value.get("res", value)
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
    texts = data.get("rec_texts")
    if texts is None: texts = data.get("texts")
    if texts is None: texts = []
    boxes = data.get("rec_boxes")
    if boxes is None: boxes = data.get("dt_polys")
    if boxes is None: boxes = []
    lines: list[tuple[str, int]] = []
    for index, value in enumerate(texts):
        text = str(value).strip()
        if not text:
            continue
        height = 0
        if index < len(boxes):
            try:
                points = boxes[index]
                ys = [float(points[1]), float(points[3])] if len(points) == 4 and not hasattr(points[0], "__len__") else [float(point[1]) for point in points]
                height = max(0, round(max(ys) - min(ys)))
            except (TypeError, ValueError, IndexError):
                height = 0
        lines.append((text, height))
    return lines


def _legacy_lines(result: Any) -> list[tuple[str, int]]:
    lines: list[tuple[str, int]] = []
    groups = result if isinstance(result, list) else []
    if groups and isinstance(groups[0], list) and groups[0] and isinstance(groups[0][0], list) and len(groups[0][0]) == 2:
        groups = groups[0]
    for item in groups:
        try:
            box, (value, _score) = item
            text = str(value).strip()
            ys = [float(point[1]) for point in box]
            if text:
                lines.append((text, max(0, round(max(ys) - min(ys)))))
        except (TypeError, ValueError, IndexError):
            continue
    return lines


def read(image: Image.Image, *, language: str | None = None) -> tuple[str, dict[str, int]]:
    """Return OCR text and line heights using PaddleOCR's available API."""
    requested = "devanagari" if language in {"hin+mar", "hi", "mr", "devanagari"} else language
    engine = get_engine(requested)
    rgb = image.convert("RGB")
    try:
        import numpy as np
        # Paddle's ndarray input follows OpenCV's BGR convention.
        source = np.asarray(rgb)[:, :, ::-1].copy()
    except ImportError as exc:
        raise RuntimeError("PaddleOCR requires numpy") from exc

    with _inference_lock:
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
