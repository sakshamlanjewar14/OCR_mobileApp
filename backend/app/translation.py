"""Offline Hindi/Marathi-to-English translation; no runtime network requests."""
from __future__ import annotations

import os
import re
import threading
import time
import unicodedata
from functools import lru_cache
from pathlib import Path

DEVANAGARI = re.compile(r"[\u0900-\u097f]")
PROTECTED = re.compile(r"([\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:https?://|www\.)\S+|\+?\d[\d ()./-]*\d|\d+)")
_lock = threading.Lock()
_MODEL_INIT_MS = 0


def indic_language(text: str) -> str | None:
    if not DEVANAGARI.search(text):
        return None
    return "mar_Deva" if re.search(r"[ळऱऴॲॊॎ]|आम्ही|आहेत|आहे|पत्ता|करतो|विकतो|आमचे", text) else "hin_Deva"


def latin_name(text: str | None) -> str | None:
    """Render identities in Latin letters without translating their meaning."""
    if not text or not DEVANAGARI.search(text):
        return text
    from unidecode import unidecode
    return unidecode(text).strip()


def latinize_document_text(text: str) -> str:
    """Return a Latin-only review transcript when semantic translation is unavailable.

    OCR often adds isolated Indic glyphs to an otherwise English card. In that
    common case, discard the isolated glyph-only lines and remove Indic glyphs
    from mixed lines. For predominantly Indic documents, transliteration keeps
    the readable content available for review instead of mixing scripts.
    """
    if not DEVANAGARI.search(text):
        return text
    letters = re.findall(r"[A-Za-z\u0900-\u097f]", text)
    latin_share = sum(ch.isascii() and ch.isalpha() for ch in letters) / max(1, len(letters))
    if latin_share >= 0.35:
        lines: list[str] = []
        for line in text.splitlines():
            if DEVANAGARI.search(line):
                if not re.search(r"[A-Za-z0-9@.+-]", line):
                    continue
                line = DEVANAGARI.sub("", line)
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)
    return latin_name(text) or ""


def _quantize_cpu_model(torch, model):
    """Use INT8 CPU linear layers when the runtime supports them."""
    engines = getattr(getattr(torch.backends, "quantized", None), "supported_engines", ())
    if not any(engine in engines for engine in ("fbgemm", "qnnpack")):
        return model
    try:
        return torch.ao.quantization.quantize_dynamic(
            model, {torch.nn.Linear}, dtype=torch.qint8
        )
    except Exception:
        # Keep translation available on PyTorch builds whose CPU backend does
        # not support dynamic quantization for this model architecture.
        return model


@lru_cache(maxsize=1)
def _load_model():
    global _MODEL_INIT_MS
    started = time.perf_counter()
    configured = os.getenv("TRANSLATION_MODEL_DIR", "").strip()
    if not configured or not Path(configured).is_dir():
        raise RuntimeError("Local translation model is not configured")
    import torch
    from transformers import M2M100ForConditionalGeneration, M2M100Tokenizer
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    tokenizer = M2M100Tokenizer.from_pretrained(configured, local_files_only=True)
    model = M2M100ForConditionalGeneration.from_pretrained(configured, local_files_only=True)
    model = _quantize_cpu_model(torch, model)
    model.eval()
    _MODEL_INIT_MS = round((time.perf_counter() - started) * 1000)
    return torch, tokenizer, model


def model_initialization_time_ms() -> int:
    """Return the model load/quantization time for this process, if loaded."""
    return _MODEL_INIT_MS


def warm_translation_model() -> None:
    """Load a configured local translation model in the background at startup."""
    configured = os.getenv("TRANSLATION_MODEL_DIR", "").strip()
    if not configured or not Path(configured).is_dir():
        return
    try:
        # Use the same lock as foreground translation so an early scan waits
        # for the warmup instead of loading a second model copy in parallel.
        with _lock:
            _load_model()
    except Exception:
        # OCR remains available through the Latin transliteration fallback.
        return


def _translate_parts(parts: list[str], language: str = "hi") -> list[str]:
    torch, tokenizer, model = _load_model()
    tokenizer.src_lang = language
    results = []
    for start in range(0, len(parts), 8):
        batch = parts[start:start + 8]
        inputs = tokenizer(batch, padding=True, truncation=False, return_tensors="pt")
        if inputs.input_ids.shape[1] > model.config.max_position_embeddings:
            raise ValueError("Source segment exceeds local model capacity")
        with torch.inference_mode():
            # Card lines are short. Beam=1 and a bounded output avoid spending
            # seconds exploring long translations for every brief field.
            output = model.generate(**inputs, num_beams=1, max_new_tokens=128, forced_bos_token_id=tokenizer.get_lang_id("en"))
        results.extend(tokenizer.batch_decode(output, skip_special_tokens=True))
    return results


def translate_document_text(text: str) -> tuple[str | None, str]:
    # A single misread glyph in an otherwise English card is OCR noise, not a
    # multilingual document. Translating it can corrupt already-correct names.
    if len(DEVANAGARI.findall(text)) < 3 or not indic_language(text):
        return text, "not_needed"
    normalized = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in text)
    pieces: list[str] = []
    indexes: list[int] = []
    for line in normalized.splitlines(keepends=True):
        # Proper location names can be mistranslated by a general translation model.
        # Keep address lines in Latin letters rather than inventing another location.
        if DEVANAGARI.search(line) and re.search(r"पता\s*:|पत्ता\s*:|पुणे|मुंबई|महाराष्ट्र|कानपुर|सड़क|रस्ता|रोड|मार्ग|नगर|चौक|निकट|बाजार|मार्केट", line):
            rendered = latin_name(re.sub(r"^\s*(?:पता|पत्ता)\s*:", "Address:", line.rstrip())) or ""
            pieces.append(rendered + ("\n" if line.endswith("\n") else ""))
            continue
        for part in PROTECTED.split(line):
            # Bound source chunks without silently truncating document contents.
            for chunk in re.findall(r".{1,250}(?:\s+|$)|\S{1,250}", part, re.S):
                if DEVANAGARI.search(chunk):
                    indexes.append(len(pieces))
                pieces.append(chunk)
    try:
        with _lock:
            translations = _translate_parts([pieces[index].strip() for index in indexes], "mr" if indic_language(text) == "mar_Deva" else "hi")
        if len(translations) != len(indexes):
            raise ValueError("Incomplete translation")
        for index, value in zip(indexes, translations):
            if not value.strip() or DEVANAGARI.search(value):
                raise ValueError("Incomplete English output")
            original = pieces[index]
            prefix = original[:len(original) - len(original.lstrip())]
            suffix = original[len(original.rstrip()):]
            pieces[index] = prefix + value.strip() + suffix
        return "".join(pieces), "translated"
    except Exception:
        return None, "unavailable"
