import re
import os
import shutil
import subprocess
import tempfile
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import pytesseract
import pymupdf
try:
    import cv2
    import numpy as np
except ImportError:  # QR scanning remains optional for lightweight local installs.
    cv2 = None
    np = None
from PIL import Image, ImageChops, ImageEnhance, ImageOps
from .translation import translate_document_text, latin_name, latinize_document_text
from .qr import parse_qr_payload


configured_tesseract = os.getenv("TESSERACT_CMD", "").strip()
if configured_tesseract:
    # Ignore machine-specific paths copied from a local .env (for example a
    # Windows .exe path on a Linux deployment) and use the server PATH instead.
    configured_path = Path(configured_tesseract)
    if configured_path.is_file() or shutil.which(configured_tesseract):
        pytesseract.pytesseract.tesseract_cmd = configured_tesseract

configured_tessdata = os.getenv("TESSDATA_PREFIX", "").strip()
if configured_tessdata and not Path(configured_tessdata).exists():
    # Tesseract can use its platform default tessdata directory. An invalid
    # local path makes an otherwise installed server engine look unavailable.
    os.environ.pop("TESSDATA_PREFIX", None)


class OCRUnavailableError(RuntimeError):
    """Raised when neither the configured OCR engine nor Windows OCR can run."""


def _tesseract_available() -> bool:
    command = str(pytesseract.pytesseract.tesseract_cmd)
    return Path(command).is_file() or shutil.which(command) is not None


def _uses_windows_ocr_fallback() -> bool:
    return os.name == "nt" and not _tesseract_available()


def _windows_ocr(image: Image.Image) -> str:
    script = Path(__file__).with_name("windows_ocr.ps1")
    powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise OCRUnavailableError("Windows PowerShell OCR runtime was not found.")

    def recognize(candidate: Image.Image) -> str:
        image_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temporary_image:
                image_path = temporary_image.name
            candidate.save(image_path, format="PNG")
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), image_path],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=90,
            )
            return result.stdout.strip()
        finally:
            if image_path:
                Path(image_path).unlink(missing_ok=True)

    try:
        best_image = image
        best_text = recognize(image)
        # extract_fields already rotates portrait phone captures to make the
        # card landscape. Only retry a portrait page, where a 90-degree turn is
        # plausible; spawning three more PowerShell processes for every image
        # made a normal scan take several seconds on Windows.
        readable = lambda value: sum(char.isalnum() for char in value)
        if readable(best_text) < 80 and image.height > image.width * 1.25:
            rotated_image = image.rotate(90, expand=True)
            rotated_text = recognize(rotated_image)
            if readable(rotated_text) > readable(best_text):
                best_text = rotated_text
                best_image = rotated_image

        # Faded card-print often turns the letters PVT into ``.17.``. Run a
        # contrast-enhanced pass only for that suspicious company-line pattern;
        # when the second pass confirms the matching ``LTD`` ending, combine
        # both readings into the full legal suffix.
        for line in best_text.splitlines():
            noisy_suffix = re.fullmatch(r"\s*(.+?)\s*[.]\s*17\s*[.]?\s*", line, re.I)
            if not noisy_suffix or not re.search(r"\b(shipping|logistics|industries|technologies|services|company|group)\b", noisy_suffix.group(1), re.I):
                continue
            enhanced_image = ImageEnhance.Contrast(ImageOps.grayscale(best_image)).enhance(2.2).convert("RGB")
            enhanced = recognize(enhanced_image)
            prefix_key = re.sub(r"[^a-z0-9]", "", noisy_suffix.group(1).lower())
            for enhanced_line in enhanced.splitlines():
                enhanced_key = re.sub(r"[^a-z0-9]", "", enhanced_line.lower())
                if enhanced_key.startswith(prefix_key) and re.search(r"\b(?:ltd\.?|limited)\b", enhanced_line, re.I):
                    corrected_line = f"{noisy_suffix.group(1).strip()} PVT. LTD."
                    return enhanced.replace(enhanced_line, corrected_line, 1)
        return best_text
    except (OSError, subprocess.SubprocessError) as exc:
        raise OCRUnavailableError(
            "Windows OCR could not run. Check that an OCR language is installed for your Windows account."
        ) from exc


def _read_image(image: Image.Image) -> str:
    try:
        return pytesseract.image_to_string(image, lang=os.getenv("TESSERACT_LANGUAGES", "eng+hin+mar"))
    except pytesseract.TesseractError:
        # Missing Hindi/Marathi traineddata should not make English card scanning fail.
        return pytesseract.image_to_string(image, lang="eng")
    except pytesseract.TesseractNotFoundError:
        if os.name != "nt":
            raise
        return _windows_ocr(image)


def _read_image_with_layout(image: Image.Image) -> tuple[str, dict[str, int]]:
    """Read OCR lines and their text heights so prominent brand text can be preferred."""
    # Windows OCR is the local fallback when Tesseract is not installed. It
    # cannot provide word boxes, so avoid calling it again for synthetic page
    # segmentation passes.
    if _uses_windows_ocr_fallback():
        return _windows_ocr(image), {}

    def read(config: str = "") -> tuple[str, dict[str, int]]:
        try:
            data = pytesseract.image_to_data(image, lang=os.getenv("TESSERACT_LANGUAGES", "eng+hin+mar"), config=config, output_type=pytesseract.Output.DICT)
        except pytesseract.TesseractError:
            data = pytesseract.image_to_data(image, lang="eng", config=config, output_type=pytesseract.Output.DICT)
        except pytesseract.TesseractNotFoundError:
            if os.name != "nt":
                raise
            return _windows_ocr(image), {}
        grouped: dict[tuple[int, int, int], dict[str, object]] = {}
        for index, word in enumerate(data.get("text", [])):
            token = str(word).strip()
            if not token:
                continue
            key = (int(data["block_num"][index]), int(data["par_num"][index]), int(data["line_num"][index]))
            line = grouped.setdefault(key, {"words": [], "height": 0})
            line["words"].append(token)  # type: ignore[union-attr]
            line["height"] = max(int(line["height"]), int(data["height"][index]))
        lines = [" ".join(line["words"]) for line in grouped.values()]
        heights = {line.casefold(): int(grouped[key]["height"]) for key, line in zip(grouped, lines)}
        return "\n".join(lines).strip(), heights

    text, heights = read()
    # Stylized Hindi/Marathi cards often lose the prominent brand line in the
    # default page segmentation. A contrast-enhanced sparse block pass is used
    # only when no email or website was found.
    if not re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|(?:https?://|www\.)\S+", text, re.I):
        enhanced = ImageEnhance.Contrast(ImageOps.grayscale(image)).enhance(1.8).convert("RGB")
        previous = image
        image = enhanced
        alternate, alternate_heights = read("--psm 12")
        image = previous
        alternate_score = len(re.findall(r"[\u0900-\u097F]", alternate)) + 3 * len(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\+?\d[\d ()-]{7,}", alternate, re.I))
        text_score = len(re.findall(r"[\u0900-\u097F]", text)) + 3 * len(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\+?\d[\d ()-]{7,}", text, re.I))
        if alternate_score > text_score:
            text = _merge_ocr_transcripts(text, alternate)
            heights = {**heights, **alternate_heights}
    # Reuse the sparse pass above when it already ran; a second identical
    # Tesseract call added several seconds to cards without improving coverage.
    if (re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|(?:https?://|www\.)\S+", text, re.I)
            and len(re.findall(r"[\u0900-\u097F]", text)) < 3):
        indic_text, indic_heights = read("--psm 12")
        if len(re.findall(r"[\u0900-\u097F]", indic_text)) >= 3:
            text = _merge_ocr_transcripts(text, indic_text)
            heights = {**heights, **indic_heights}
    return (text or _read_image(image)), heights


def _merge_ocr_transcripts(primary: str, additional: str) -> str:
    """Union OCR lines from recovery passes without discarding the first read."""
    lines = [line.strip() for line in primary.splitlines() if line.strip()]
    keys = {re.sub(r"[^a-z0-9\u0900-\u097f]", "", line.casefold()) for line in lines}
    for line in additional.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        key = re.sub(r"[^a-z0-9\u0900-\u097f]", "", candidate.casefold())
        if not key or key in keys:
            continue
        if any(
            min(len(key), len(existing)) / max(len(key), len(existing)) >= 0.82
            and SequenceMatcher(None, key, existing).ratio() >= 0.88
            for existing in keys
        ):
            continue
        lines.append(candidate)
        keys.add(key)
    return "\n".join(lines)


_INDIC_MEANING_MARKERS = re.compile(
    r"(?:सभी|प्रकार|कपड़े|बुटीक|बूटीक|फैशन|व्यवसाय|कंपनी|लिमिटेड|उद्योग|सेवा|दुरुस्ती|काम|करून|मिळतील|"
    r"केली|जातील|आमच्याकडे|फिटिंग|मेंटेन|फ्रीज|कुलर|रेफ्रिजरेशन|विद्युत|लाईट|प्रकाश|हॉटेल|सलवार|डिजाइनर|"
    r"संचालक|मालकीन|मालक|प्रो\.?|निर्देशक)"
)


def _translation_focus_text(text: str) -> str:
    """Translate only meaningful Indic business lines; transliterate the rest.

    Names and addresses are better kept phonetically stable, while service and
    category lines benefit from semantic translation. This avoids sending OCR
    fragments, duplicate logo text, and every address line through the local
    translation model for each card.
    """
    focused = []
    for line in text.splitlines(keepends=True):
        if re.search(r"[\u0900-\u097f]", line) and _INDIC_MEANING_MARKERS.search(line):
            focused.append(line)
        else:
            focused.append(latinize_document_text(line))
    return "".join(focused)


def _read_recovery_crops(image: Image.Image, contact_details: bool = False) -> list[str]:
    """Read bounded information regions when the full-card pass missed fields."""
    width, height = image.size
    ratio = width / max(height, 1)
    if contact_details and ratio > 1.15:
        # Contact lines sit beneath the name on many cards. A focused crop
        # avoids spending another full-card pass and reads each line cleanly.
        boxes = [((0, int(height * 0.40), int(width * 0.62), int(height * 0.92)), "--psm 6", 2.0)]
    elif 0.85 <= ratio <= 1.15:
        # Include both upper brand/title and lower address panels; the previous
        # narrow center crop clipped the brand on square Marathi cards.
        boxes = [((int(width * 0.03), int(height * 0.10), int(width * 0.97), int(height * 0.87)), "--psm 6", 1.7)]
    elif ratio > 1.15:
        # The two overlapping half-card scans used to run serially and cost
        # roughly two seconds on a mobile-sized card. A single modestly
        # enlarged sparse-text pass covers company, person, and contact lines
        # together, with no crop edge able to cut off a field.
        boxes = [((0, 0, width, height), "--psm 11", 1.25)]
    else:
        boxes = [
            # The contact/name panel on many cards occupies the left half. A
            # tighter top crop keeps the name and title together instead of
            # letting nearby QR/CTA artwork confuse line segmentation.
            ((0, 0, int(width * 0.70), int(height * 0.45)), "--psm 6", 2.0),
            ((0, int(height * 0.25), int(width * 0.75), height), "--psm 6", 1.5),
        ]
    results: list[str] = []
    crop_language = os.getenv("TESSERACT_LANGUAGES", "eng+hin+mar")
    for box, config, scale in boxes:
        crop = image.crop(box)
        if scale > 1:
            crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.Resampling.LANCZOS)
        if _uses_windows_ocr_fallback():
            results.append(_windows_ocr(crop))
            continue
        try:
            result = pytesseract.image_to_string(crop, lang=crop_language, config=config)
        except pytesseract.TesseractError:
            result = pytesseract.image_to_string(crop, lang="eng", config=config)
        except pytesseract.TesseractNotFoundError:
            if os.name != "nt":
                raise
            result = _windows_ocr(crop)
        results.append(result)
    return results


def _read_card_text(image: Image.Image) -> tuple[str, dict[str, int]]:
    text, heights = _read_image_with_layout(image)
    fields = _parse_fields(text, heights)
    needs_recovery = (
        not fields.get("company_name")
        or not fields.get("owner_name")
        or (not fields.get("contact_numbers") and not fields.get("email_addresses") and not fields.get("website"))
        or (bool(fields.get("contact_numbers")) and not fields.get("email_addresses") and not fields.get("website"))
        or any(re.search(r"[\u0900-\u097f]", str(fields.get(key) or "")) for key in ("company_name", "owner_name"))
        or bool(re.search(r"\b(?:logis|technolog|corpor|industri)['’]?$", str(fields.get("company_name") or ""), re.I))
    )
    if needs_recovery:
        contact_recovery = bool(fields.get("contact_numbers")) and not fields.get("email_addresses") and not fields.get("website")
        focused_contact_pass = contact_recovery and not (
            not fields.get("company_name") or not fields.get("owner_name")
        )
        for recovery in _read_recovery_crops(image, contact_details=focused_contact_pass):
            if recovery.strip():
                text = _merge_ocr_transcripts(text, recovery)
        recovered_fields = _parse_fields(text, heights)
        if (not focused_contact_pass and recovered_fields.get("contact_numbers")
                and not recovered_fields.get("email_addresses") and not recovered_fields.get("website")):
            for recovery in _read_recovery_crops(image, contact_details=True):
                if recovery.strip():
                    text = _merge_ocr_transcripts(text, recovery)
    return text, heights


def _extract_text(path: Path, content_type: str) -> str:
    chunks: list[str] = []
    if content_type == "application/pdf":
        with pymupdf.open(path) as pdf:
            for page in pdf:
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                chunks.append(_read_image(image))
    else:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            chunks.append(_read_image(image))
    return "\n".join(chunks)


def _card_aspect_ratio(image: Image.Image) -> bool:
    ratio = image.width / max(image.height, 1)
    return 0.9 <= ratio <= 2.8 or 0.4 <= ratio <= 0.85


def _analysis_image(image: Image.Image) -> tuple[Image.Image, float]:
    """Keep layout detection bounded for large phone photos."""
    longest_side = max(image.size)
    if longest_side <= 1400:
        return image, 1.0
    scale = 1400 / longest_side
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.BOX), scale


def _blank_gutters(image: Image.Image, horizontal: bool) -> list[tuple[int, int]]:
    analysis, scale = _analysis_image(image)
    gray = ImageOps.grayscale(analysis)
    ink = gray.point(lambda value: 255 if value < 220 else 0)
    if horizontal:
        projection = ink.resize((1, analysis.height), Image.Resampling.BOX)
        length = analysis.height
    else:
        projection = ink.resize((analysis.width, 1), Image.Resampling.BOX)
        length = analysis.width
    values = list(projection.getdata())
    minimum_gap = max(5, round(length * 0.008))
    maximum_ink = max(3, round(255 * 0.004))
    gutters: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(values + [255]):
        if value <= maximum_ink:
            if start is None:
                start = index
        elif start is not None:
            if index - start >= minimum_gap and length * 0.12 <= start and index <= length * 0.88:
                gutters.append((round(start / scale), round(index / scale)))
            start = None
    return gutters


def _edge_split(image: Image.Image, horizontal: bool) -> tuple[int, int] | None:
    """Find a strong, nearly centered card seam when the cards touch."""
    analysis, scale = _analysis_image(image)
    gray = ImageOps.grayscale(analysis)
    if horizontal:
        length, cross_length = analysis.height, analysis.width
        before = gray.crop((0, 0, cross_length, length - 1))
        after = gray.crop((0, 1, cross_length, length))
    else:
        length, cross_length = analysis.width, analysis.height
        before = gray.crop((0, 0, length - 1, cross_length))
        after = gray.crop((1, 0, length, cross_length))
    if length < 20 or cross_length < 20:
        return None

    differences = ImageChops.difference(before, after)
    if horizontal:
        mean_projection = differences.resize((1, length - 1), Image.Resampling.BOX)
        coverage_projection = differences.point(lambda value: 255 if value >= 18 else 0).resize(
            (1, length - 1), Image.Resampling.BOX
        )
    else:
        mean_projection = differences.resize((length - 1, 1), Image.Resampling.BOX)
        coverage_projection = differences.point(lambda value: 255 if value >= 18 else 0).resize(
            (length - 1, 1), Image.Resampling.BOX
        )
    mean_values = list(mean_projection.getdata())
    coverage_values = list(coverage_projection.getdata())
    candidates: list[tuple[float, int]] = []
    for index, (edge_strength, coverage) in enumerate(zip(mean_values, coverage_values), start=1):
        # Touching business cards are generally similar in size. Restrict the
        # no-gap fallback to the middle of an overlong image so text strokes
        # elsewhere cannot be mistaken for a card boundary.
        if not length * 0.40 <= index <= length * 0.60:
            continue
        first_length, second_length = index, length - index
        if horizontal:
            first_ratio = cross_length / first_length
            second_ratio = cross_length / second_length
        else:
            first_ratio = first_length / cross_length
            second_ratio = second_length / cross_length
        if not (0.9 <= first_ratio <= 2.8 or 0.4 <= first_ratio <= 0.85):
            continue
        if not (0.9 <= second_ratio <= 2.8 or 0.4 <= second_ratio <= 0.85):
            continue
        coverage_fraction = coverage / 255
        if edge_strength >= 7 and coverage_fraction >= 0.18:
            candidates.append((edge_strength * coverage_fraction, index))
    if not candidates:
        return None
    _, cut = max(candidates)
    return (round(cut / scale), round((cut + 1) / scale))


def _split_card_image_recursive(image: Image.Image, depth: int = 0) -> list[Image.Image] | None:
    if depth >= 3:
        return [image] if _card_aspect_ratio(image) else None

    best: list[Image.Image] | None = None
    for horizontal in (False, True):
        dimension = image.height if horizontal else image.width
        gutters = _blank_gutters(image, horizontal)
        split_options = [(start, end) for start, end in gutters
                         if start >= dimension * 0.12 and dimension - end >= dimension * 0.12]
        # Prefer the widest genuine gap and try at most one split per axis.
        # This bounds recursive work even when a textured background creates
        # many small dark runs in a high-resolution photo.
        if split_options:
            start, end = max(split_options, key=lambda gap: gap[1] - gap[0])
        elif not _card_aspect_ratio(image) and dimension > max(image.width, image.height) * 0.5:
            edge = _edge_split(image, horizontal)
            if edge is None:
                continue
            start, end = edge
        else:
            continue
        if horizontal:
            parts = [image.crop((0, 0, image.width, start)), image.crop((0, end, image.width, image.height))]
        else:
            parts = [image.crop((0, 0, start, image.height)), image.crop((end, 0, image.width, image.height))]
        leaves: list[Image.Image] = []
        for part in parts:
            split_part = _split_card_image_recursive(part, depth + 1)
            if split_part is None:
                leaves = []
                break
            leaves.extend(split_part)
        if leaves and len(leaves) <= 12 and (best is None or len(leaves) > len(best)):
            best = leaves

    if best:
        return best
    return [image] if _card_aspect_ratio(image) else None


def _split_card_image(image: Image.Image, depth: int = 0) -> list[Image.Image] | None:
    if depth:
        return _split_card_image_recursive(image, depth)

    # Many phone photos are portrait shots of two landscape cards placed next
    # to each other. Rotate only for layout detection so the OCR receives cards
    # in a more readable orientation.
    detection_image = image.rotate(90, expand=True) if image.height > image.width * 2.5 else image
    regions = _split_card_image_recursive(detection_image)
    if regions and len(regions) > 1:
        return regions
    return regions


def _clean_lines(text: str) -> list[str]:
    source_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        normalized = re.sub(r"\s+", " ", line.replace("\ufffd", "").replace("•", "")).strip(" ,;|\t@()©®")
        normalized = re.sub(r"\bwww(?=[A-Za-z0-9-])", "www.", normalized, flags=re.I)
        normalized = re.sub(r"\s+seen$", "", normalized, flags=re.I)
        fragments = re.split(r"(?=\b(?:Mob(?:ile)?|Tel(?:ephone)?|Fax|Email|Website|Web|Office\s+No\.?|Address)\s*[:#\-])", normalized, flags=re.I)
        for fragment in fragments:
            source_lines.extend(part.strip(" ,;|\t") for part in fragment.split("|") if part.strip(" ,;|\t"))
    label_names = (
        r"company(?:\s+(?:full\s+)?name)?|business(?:\s+name)?|firm(?:\s+name)?|organization(?:\s+name)?|"
        r"owner(?:\s+name)?|contact\s+(?:person|name)|person\s+name|name|designation|job\s+title|"
        r"position|address|company\s+address|office\s+address|website|web\s+site|url|"
        r"phone|mobile|telephone|contact\s+(?:no|number)|e-?mail(?:\s+id)?"
    )
    labelled_line = re.compile(rf"^(?P<label>{label_names})(?:\s*\d+)?\s*[:#-]\s*(?P<value>.*)$", re.I)
    bare_label = re.compile(rf"^(?:{label_names})(?:\s*\d+)?$", re.I)
    cleaned: list[str] = []
    index = 0
    while index < len(source_lines):
        line = source_lines[index]
        match = labelled_line.fullmatch(line)
        if match:
            value = match.group("value").strip()
            if value:
                cleaned.append(value)
        elif bare_label.fullmatch(line) and index + 1 < len(source_lines):
            cleaned.append(source_lines[index + 1])
            index += 1
        else:
            cleaned.append(line)
        index += 1
    return cleaned


def _format_labeled_text(text: str) -> str:
    """Keep recognized label/value pairs together in the editable source text."""
    lines = [re.sub(r"\s+", " ", line.replace("\ufffd", "").replace("•", "")).strip() for line in text.splitlines() if line.strip()]
    label_names = (
        r"company(?:\s+(?:full\s+)?name)?|business(?:\s+name)?|firm(?:\s+name)?|organization(?:\s+name)?|owner(?:\s+name)?|"
        r"contact\s+(?:person|name)|person\s+name|name|designation|job\s+title|position|address|"
        r"company\s+address|office\s+address|website|web\s+site|url|phone|mobile|telephone|"
        r"contact\s+(?:no|number)|e-?mail(?:\s+id)?"
    )
    bare_label = re.compile(rf"^(?P<label>{label_names})(?:\s*\d+)?\s*:?$", re.I)
    empty_value_label = re.compile(rf"^(?P<label>{label_names})(?:\s*\d+)?\s*:\s*$", re.I)
    formatted: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = bare_label.fullmatch(line) or empty_value_label.fullmatch(line)
        if match and index + 1 < len(lines):
            formatted.append(f"{match.group('label').strip()}: {lines[index + 1]}")
            index += 2
        else:
            formatted.append(line)
            index += 1
    return "\n".join(formatted)


_DOMAIN_NAME_PARTS = (
    "software", "exports", "export", "industries", "industry", "technology", "technologies",
    "solutions", "solution", "services", "service", "systems", "system", "electronics",
    "electricals", "electrical", "refrigeration", "logistics", "shipping", "trading",
    "enterprise", "enterprises", "consulting", "consultancy", "corporation", "corporate",
    "marine", "power", "green", "globe", "firm", "group", "india", "global", "digital",
    "network", "engineering", "industrials", "automation",
)
_DOMAIN_NAME_PARTS = tuple(sorted(set(_DOMAIN_NAME_PARTS), key=len, reverse=True))


def _domain_from_url(value: str | None) -> str:
    host = re.sub(r"^https?://", "", value or "", flags=re.I).split("/", 1)[0]
    return re.sub(r"^www\.", "", host, flags=re.I).rstrip(".").lower()


def _company_name_from_domain(host: str) -> str:
    """Format a domain-derived company suggestion without pretending it is verified."""
    compact = re.sub(r"[^a-z0-9]", "", host.casefold())
    tokens: list[str] = []
    cursor = 0
    while cursor < len(compact):
        match = next((word for word in _DOMAIN_NAME_PARTS if compact.startswith(word, cursor)), None)
        if match:
            tokens.append(match.title())
            cursor += len(match)
            continue
        next_match = min((position for word in _DOMAIN_NAME_PARTS
                          if (position := compact.find(word, cursor + 1)) >= 0), default=len(compact))
        tokens.append(compact[cursor:next_match].title())
        cursor = next_match
    return " ".join(token for token in tokens if token)


def _explicit_fields(text: str) -> dict[str, list[str]]:
    """Read labelled values before heuristic classification can mix fields."""
    labels = {
        "company name": "company_name", "business name": "company_name", "firm name": "company_name",
        "organization": "company_name", "organisation": "company_name", "company": "company_name",
        "owner name": "owner_name", "owner": "owner_name", "contact person": "owner_name",
        "person name": "owner_name", "director name": "owner_name", "founder name": "owner_name",
        "proprietor name": "owner_name",
        "designation": "designation", "job title": "designation", "position": "designation", "title": "designation",
        "description": "business_description", "business description": "business_description",
        "company description": "business_description", "services": "business_description",
        "address": "company_address", "company address": "company_address", "office address": "company_address",
        "registered office": "company_address", "location": "company_address",
        "website": "website", "web site": "website", "url": "website",
        "email": "email_addresses", "email id": "email_addresses", "e-mail": "email_addresses", "gmail": "email_addresses",
        "phone": "contact_numbers", "mobile": "contact_numbers", "contact no": "contact_numbers", "contact number": "contact_numbers",
        "telephone": "telephone_numbers", "tel": "telephone_numbers", "telephone number": "telephone_numbers",
        "fax": "fax_numbers", "fax number": "fax_numbers", "telefax": "fax_numbers",
        "whatsapp": "whatsapp_link", "whatsapp link": "whatsapp_link", "facebook": "facebook_link",
        "facebook link": "facebook_link", "instagram": "instagram_link", "instagram link": "instagram_link",
        "city": "city", "state": "state", "pincode": "pincode", "pin code": "pincode",
        "gst": "gst_number", "gst number": "gst_number", "registration number": "registration_number",
    }
    source = [re.sub(r"\s+", " ", line.replace("\ufffd", "")).strip() for line in text.splitlines()]
    found: dict[str, list[str]] = {}
    for index, line in enumerate(source):
        match = re.match(r"^\s*((?:mobile|mob|telephone|tel\.?|telefax|fax|phone))\s*(?:[:#-]\s*|\s+)(.*)$", line, re.I)
        if not match:
            match = re.match(r"^\s*([A-Za-z][A-Za-z .]{1,35}?)\s*[:#]\s*(.*)$", line)
        if not match:
            match = re.match(r"^\s*([A-Za-z][A-Za-z .]{1,35}?)(?:\s*\d+)?\s*$", line)
            value = source[index + 1].strip() if match and index + 1 < len(source) else ""
        else:
            value = match.group(2).strip()
        if not match:
            continue
        key = re.sub(r"\s+", " ", match.group(1).strip(" .").lower())
        target = labels.get(key)
        if target and value:
            if target in {"contact_numbers", "telephone_numbers"}:
                if target == "telephone_numbers":
                    value = re.split(r"\b(?:fax|telefax)\b", value, maxsplit=1, flags=re.I)[0]
                phone_values = [re.sub(r"\s+", " ", item.group(0)).strip(" ,;:-") for item in
                                re.finditer(r"(?<!\d)\+?\d(?:[ \t().-]*\d){6,14}(?!\d)", value)]
                found.setdefault(target, []).extend(phone_values or [value])
            elif target == "email_addresses":
                found.setdefault(target, []).extend(
                    item.group(0).rstrip(".,;") for item in
                    re.finditer(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", value, re.I)
                )
            elif target == "company_address":
                address_parts = [value]
                for continuation in source[index + 1:index + 5]:
                    if (not continuation or re.search(r"@|(?:https?://|www\.)|\+?\d[\d ()-]{7,}|\b(?:tel|mobile|phone|fax|email|website)\b", continuation, re.I)
                            or not re.search(r"\b(?:road|rd|street|near|plot|shop|floor|complex|market|pune|india|maharashtra|kanpur|411\d{3}|208\d{3}|412\d{3})\b", continuation, re.I)):
                        break
                    address_parts.append(continuation)
                found.setdefault(target, []).append(", ".join(address_parts))
            else:
                found.setdefault(target, []).append(value)
    return found


def _parse_fields(text: str, line_heights: dict[str, int] | None = None) -> dict[str, object]:
    text = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in text)
    explicit = _explicit_fields(text)
    lines = _clean_lines(text)
    full_text = "\n".join(lines)

    email_pattern = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
    email_addresses = list(dict.fromkeys(match.group(0).rstrip(".,;") for match in email_pattern.finditer(full_text)))
    # Tesseract sometimes drops the dot after the conventional ``www`` prefix.
    full_text = re.sub(r"\bwww(?=[A-Za-z0-9-])", "www.", full_text, flags=re.I)
    url_pattern = re.compile(
        r"(?<![@\w.-])(?:https?://)?(?:www\.)?[A-Z0-9-]+(?:\.[A-Z0-9-]+)*\.[A-Z]{2,}(?:/[^\s]*)?(?![\w@.-])",
        re.I,
    )
    social_links: dict[str, str] = {}
    website = None
    for url_match in url_pattern.finditer(full_text):
        candidate_url = url_match.group(0).rstrip(".,;)")
        parsed_url = parse_qr_payload(candidate_url if re.match(r"https?://", candidate_url, re.I) else f"https://{candidate_url}")
        for social_key in ("facebook_link", "instagram_link", "whatsapp_link"):
            if parsed_url.get(social_key):
                social_links.setdefault(social_key, str(parsed_url[social_key]))
        if not any(parsed_url.get(key) for key in ("facebook_link", "instagram_link", "whatsapp_link")) and website is None:
            website = candidate_url
    # OCR sometimes drops the dot before a familiar TLD (for example,
    # ``name@companycom``). Repair it only when the candidate domain exactly
    # matches the website printed on the same card after punctuation removal.
    website_host = _domain_from_url(website)
    recovered_email = False
    if website_host:
        compact_website_host = re.sub(r"[^a-z0-9]", "", website_host)
        broken_email_pattern = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+)@([A-Z0-9-]+)(?![A-Z0-9.-])", re.I)
        for match in broken_email_pattern.finditer(full_text):
            if re.sub(r"[^a-z0-9]", "", match.group(2).lower()) == compact_website_host:
                corrected = f"{match.group(1)}@{website_host}"
                if corrected not in email_addresses:
                    email_addresses.append(corrected)
                    recovered_email = True
    contact_numbers: list[str] = []
    phone_pattern = re.compile(r"(?<!\d)\+?\d(?:[ \t().-]*\d){6,14}(?!\d)")
    for line in lines:
        if re.match(r"^\s*fax\b", line, re.I) or re.search(r"(?:https?://|www\.)", line, re.I):
            continue
        contact_line = re.split(r"\b(?:fax|telefax)\b", line, maxsplit=1, flags=re.I)[0]
        for match in phone_pattern.finditer(contact_line):
            number = re.sub(r"\s+", " ", match.group(0)).strip()
            digits = re.sub(r"\D", "", number)
            if digits.startswith("91") and len(digits) > 12 and digits[2] in "6789":
                # A nearby OCR artifact such as a short card serial after an
                # Indian mobile number must not become part of a dial link.
                digits = digits[:12]
                number = f"+91 {digits[2:7]} {digits[7:12]}"
            indian_mobile = len(digits) == 10 and digits[0] in "6789"
            country_prefixed_mobile = len(digits) == 12 and digits.startswith("91") and digits[2] in "6789"
            indian_landline = len(digits) in {11, 12} and digits.startswith("0")
            international = number.lstrip().startswith("+") and 10 <= len(digits) <= 15
            if (indian_mobile or country_prefixed_mobile or indian_landline or international) and number not in contact_numbers:
                contact_numbers.append(number)

    email = email_addresses[0] if email_addresses else None

    excluded = {value.lower() for value in (email, website) if value}
    candidates = [
        line for line in lines
        if len(line) > 1
        and line.lower() not in excluded
        and not re.search(r"@|\d{5,}|https?://|www\.", line, re.I)
    ]
    company_markers = re.compile(
        r"\b(incorporated|inc\.?|llc|ltd\.?|limited|private|corp\.?|corporation|technologies?|tech|"
        r"solutions?|services?|company|co\.?|groups?|industr(?:y|ies)|shipping|logistics|foundation|trust|"
        r"association|societ(?:y|ies|y)|scociety|institutes?|universit(?:y|ies)|bank|hospitals?|clinic|schools?|electricals?|refrigeration|"
        r"colleges?|academ(?:y|ies)|estate|estates|real\s+estate|realstates?|pharma(?:ceutical)?s?|pharamasetical|"
        r"business|innovations?|inovation|advertising|enterprises?|spaces?|hotels?|lodges?|laudge|fashion|boutiques?|systems?|"
        r"restaurants?|restorents|finance|loans?|developers?|associates?|education|consultan(?:t|cy|ts|cies)|"
        r"movers?|packers?|packagers?|automation|ngo|classes|pvt\.?)\b",
        re.I,
    )
    indic_business_markers = re.compile(r"(?:फैशन|फेशन|बुटीक|बूटीक|कंपनी|लिमिटेड|उद्योग|सेवाएँ|व्यवसाय|रेफ्रिजरेशन|इलेक्ट्रिकल्स?)")
    designation_markers = re.compile(
        r"\b(director|manager|owner|founder|ceo|cto|cfo|president|consultant|engineer|officer|executive|"
        r"specialist|analyst|designer|architect|administrator|coordinator|developer|accountant|partner|head)\b|\bsales\s+(?:manager|executive|representative|head|director)\b",
        re.I,
    )
    honorific_owner = next((
        line for line in candidates
        if re.match(r"^(?:mr|mrs|ms|miss|dr|prof)\.?\s+[\w\u00c0-\u024f]", line, re.I)
    ), None)
    indic_owner_candidates = [
        " ".join(match.group("name").split())
        for line in candidates
        if (match := re.search(
            r"(?:(?:श्री|श्रीमती|कुमारी|डॉ\.?|डॉक्टर|प्रो\.?|प्रा\.?)\s*)+"
            r"(?P<name>[\u0900-\u097f]{2,}(?:\s+[\u0900-\u097f]{2,}){1,3})",
            line,
        ))
    ]
    indic_owner_keys = {re.sub(r"[^\u0900-\u097f]", "", name) for name in indic_owner_candidates}
    indic_honorific_owner = indic_owner_candidates[0] if len(indic_owner_keys) == 1 and indic_owner_candidates else None
    company_from_domain = False
    company = next((
        line for line in candidates
        if company_markers.search(line) and line != honorific_owner and not designation_markers.search(line)
    ), None)
    # Registered-name lines are stronger evidence than a product/partner brand
    # printed elsewhere on a brochure or authorised-dealer card.
    legal_company = re.compile(r"\b(?:pvt\.?\s*ltd\.?|private\s+limited|limited|ltd\.?|llp|inc\.?|corporation|corp\.?)\b", re.I)
    raw_company_line = next((re.sub(r"\s+", " ", line).strip() for line in text.splitlines()
                             if legal_company.search(line) and not re.search(r"@|https?://|www\.", line, re.I)), None)
    if raw_company_line:
        company = raw_company_line
    legal_candidates = [line for line in candidates if legal_company.search(line) and line != honorific_owner]
    if legal_candidates:
        company = next((line for line in legal_candidates if re.search(r"pvt|private", line, re.I)), legal_candidates[0])
    fuzzy_company = next((line for line in candidates if re.search(r"corpor|industr|technolog|logistic", line, re.I)
                          and line != honorific_owner), None)
    if fuzzy_company and not legal_candidates and not raw_company_line:
        company = fuzzy_company
    indic_company_line = next((line for line in candidates if indic_business_markers.search(line)), None)
    if indic_company_line:
        indic_index = lines.index(indic_company_line)
        previous = lines[indic_index - 1] if indic_index > 0 else ""
        company_candidate = f"{previous} {indic_company_line}" if re.search(r"[\u0900-\u097f]", previous) else indic_company_line
        # A one-word fragment such as only "Boutique" is not a business-name
        # line. Leave it for review instead of saving a partial company value.
        fragmentary_business_text = bool(re.search(r"(?:^|\s)शन\s+(?:बुटीक|बूटीक)\b", company_candidate))
        company = company_candidate if len(company_candidate.split()) >= 2 and not fragmentary_business_text else None
    if company is None:
        # A plain person-like or slogan-like line is not enough evidence for a
        # company name. Typography is intentionally not used as a classifier.
        company_candidates = [
            line for line in candidates
            if line != honorific_owner and not designation_markers.search(line)
            and not re.match(r"^(?:mr|mrs|ms|miss|dr|prof)\.?\s+", line, re.I)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9&'.-]*(?:\s+[A-Za-z][A-Za-z0-9&'.-]*){0,5}", line)
        ]
        explicit_company = next(iter(explicit.get("company_name", [])), None)
        if explicit_company:
            company = explicit_company
        if website:
            host = _domain_from_url(website).split(".")[0]
            matching_brand = next((line for line in company_candidates
                                   if re.sub(r"[^a-z0-9]", "", line.casefold()).startswith(re.sub(r"[^a-z0-9]", "", host.casefold()))
                                   or re.sub(r"[^a-z0-9]", "", host.casefold()).startswith(re.sub(r"[^a-z0-9]", "", line.casefold()))), None)
            if matching_brand:
                company = matching_brand
                brand_index = lines.index(matching_brand) if matching_brand in lines else -1
                if brand_index >= 0:
                    for end in range(brand_index + 2, min(len(lines), brand_index + 4) + 1):
                        combined = " ".join(lines[brand_index:end])
                        combined_key = re.sub(r"[^a-z0-9]", "", combined.casefold())
                        if combined_key == re.sub(r"[^a-z0-9]", "", host.casefold()):
                            company = combined
                            break
            elif host and host.casefold() not in {"gmail", "yahoo", "google", "facebook", "instagram"}:
                company = _company_name_from_domain(host)
                company_from_domain = True
        if company is None:
            company = next((line for line in company_candidates if company_markers.search(line)), None)
        if company is None:
            # A clean brand line immediately above a separate slogan is
            # business-context evidence; font size is not used for selection.
            tagline_index = next((i for i, line in enumerate(lines)
                                  if re.search(r"\bchanging\s+(?:lives|karma)\b", line, re.I)), None)
            if tagline_index is not None:
                brand_lines = lines[max(0, tagline_index - 2):tagline_index]
                if len(brand_lines) == 2 and all(re.fullmatch(r"[A-Z][A-Z0-9&'.-]*", line) for line in brand_lines):
                    company = " ".join(brand_lines)

    # OCR commonly places legal suffixes on their own line (for example,
    # ``Example Industries Pvt.`` followed by ``Ltd.``). Keep those tokens in
    # the company name instead of losing the complete registered name.
    legal_suffix_only = re.compile(
        r"^(?:(?:private|pvt\.?|limited|ltd\.?|llp|incorporated|inc\.?|"
        r"corporation|corp\.?|company|co\.?)\s*)+$",
        re.I,
    )
    if company:
        # Some valid values (such as a normalized website host) are synthesized
        # from non-line evidence and therefore won't be present in candidates.
        if company in candidates:
            company_index = candidates.index(company)
            if company_index > 0 and legal_suffix_only.fullmatch(company):
                company = f"{candidates[company_index - 1]} {company}"
            elif company_index + 1 < len(candidates) and legal_suffix_only.fullmatch(candidates[company_index + 1]):
                company = f"{company} {candidates[company_index + 1]}"
        # Recover OCR-truncated company endings only when the business email
        # local-part contains the complete visible company prefix verbatim.
        if email:
            local = email.split("@", 1)[0]
            normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
            company_key = normalize(company)
            local_key = normalize(local)
            if company_key and len(company_key) >= 8 and local_key.startswith(company_key) and len(local_key) > len(company_key):
                suffix = local_key[len(company_key):]
                company = f"{company}{suffix}"
    if not company and email:
        local = re.sub(r"[^a-z0-9]", "", email.split("@", 1)[0].casefold())
        corporation = re.fullmatch(r"([a-z]+?)(sales)?corporation", local)
        if corporation:
            company = f"{corporation.group(1).capitalize()} {((corporation.group(2) or '').capitalize() + ' ') if corporation.group(2) else ''}Corporation"
    if company and website:
        host_key = re.sub(r"[^a-z0-9]", "", _domain_from_url(website).split(".")[0])
        company_key = re.sub(r"[^a-z0-9]", "", company.casefold())
        if host_key and company_key.startswith(host_key):
            for partial, complete in (("logis", "logistics"), ("corpor", "corporation"), ("industri", "industries"), ("technolog", "technologies")):
                if company_key.endswith(partial):
                    company = re.sub(rf"{re.escape(partial)}['’]?$", complete, company, flags=re.I)
                    break
    if company and not company[0].isalnum():
        company = re.sub(r"^[^A-Za-z\u0900-\u097f]*", "", company).strip(" |,;:-") or None
    if company and re.match(r"^[A-Za-z]\s+(?=[A-Z]{2,}\s+[A-Z][a-z])", company):
        company = re.sub(r"^[A-Za-z]\s+", "", company, count=1)
    promotional_text = re.compile(
        r"\b(certified|authori[sz]ed|official|channel|solutions?|provider|dealer|distributor|welcome|quality|trusted|leading|serving|"
        r"join\s+as|download|customers?|book\s+your|check\s+estimated|alert\s+before|waiting\s+time|appointment)\b",
        re.I,
    )
    non_person_text = re.compile(r"\b(salon|partners?|customers?|download|app|play|appointment|estimated|waiting|boutique|fashion|blouse|saree|services?|seen)\b", re.I)
    indic_proprietor = any(re.search(r"प्रो\.?\s*प्रा\.?", line) for line in lines)
    designation = next(
        (
            line for line in lines
            if (designation_markers.search(line) or re.fullmatch(r"(?:managing\s+)?partner", line, re.I)
                or re.match(r"^marketing\s+exec\w*", line, re.I))
            and not promotional_text.search(line) and line != company
        ),
        "Proprietor" if indic_proprietor else None,
    )
    if designation:
        designation = re.split(r"\s+(?:[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|(?:https?://|www\.)\S+|\+?\d[\d ()-]{7,})", designation, maxsplit=1, flags=re.I)[0].strip(" ,;:-")
        designation = re.sub(r"[\u0900-\u097f].*$", "", designation).strip(" ,;:-")
        recognized_titles = list(designation_markers.finditer(designation))
        if recognized_titles:
            designation = designation[:recognized_titles[-1].end()].strip(" ,;:-")
        if re.match(r"^marketing\s+exec\w*\b", designation, re.I):
            designation = "Marketing Executive"
    description = next((
        line for line in lines
        if line not in {company, designation}
        and not re.search(r"@|https?://|www\.|\+?\d[\d ()-]{7,}", line, re.I)
        and re.search(r"\b(?:all\s+types?\s+of|all\s+kinds?\s+of|we\s+(?:provide|offer|undertake|have)|our\s+services|installation|maintenance|repairs?|tailoring|refrigeration|freezers?|coolers?|air\s*condition(?:ing|er)?|light(?:ing)?\s+fitting|electrical\s+work|wiring|blouses?|salwar|sewing|stitching)\b", line, re.I)
    ), None)
    if not description:
        indic_copy = " ".join(lines)
        if (re.search(r"(?:लाईट|लाइट)", indic_copy) and re.search(r"(?:फिटींग|फिटिंग)", indic_copy)
                and re.search(r"(?:मेंटेन|देखभाल|कामे)", indic_copy)):
            description = "Lighting fitting and maintenance services"
        elif (re.search(r"(?:फ्रीज|फ्रिज|कुलर|कूलर)", indic_copy)
                and re.search(r"(?:एसी|व्हीआरव्ही|व्हीआरएफ|वीआरवी|वीआरएफ|कामे)", indic_copy)):
            description = "Refrigeration, freezer, cooler and air-conditioning services"
        elif (re.search(r"(?:सलवार|ब्लाउज|लहंगा|कुर्ती)", indic_copy)
                and re.search(r"(?:सिलाई|कार्य|काम)", indic_copy)):
            description = "Designer clothing, blouse and tailoring services"
    # When a card has no explicit company label or legal suffix, a branded
    # website is safer evidence than treating a person's prominent name as a
    # company. Use the registered domain as a review candidate.
    if company and not company_markers.search(company) and website and (len(company.split()) == 1 or company.isupper()):
        host = _domain_from_url(website).split(".")[0]
        if (host and host.casefold() not in {"gmail", "yahoo", "google", "facebook", "instagram"}
                and re.sub(r"[^a-z0-9]", "", company.casefold()) != re.sub(r"[^a-z0-9]", "", host.casefold())):
            company = _company_name_from_domain(host)
            company_from_domain = True
    before_designation = candidates[:candidates.index(designation)] if designation in candidates else candidates
    indic_context_owner = next((
        line for index, line in enumerate(lines)
        if line not in {company, designation}
        and re.fullmatch(r"[\u0900-\u097f]{2,}(?:\s+[\u0900-\u097f]{2,}){1,3}(?:\s*[-–]\s*[\u0900-\u097f]{2,})?", line)
        and not indic_business_markers.search(line)
        and any(phone_pattern.search(context) for context in lines[max(0, index - 2):index + 3])
    ), None)
    owner = honorific_owner or indic_honorific_owner or indic_context_owner or next(
        (
            line for line in candidates
            if line not in {company, designation}
            and not promotional_text.search(line)
            and not non_person_text.search(line)
            and not re.search(r"\b(partner|director|manager|owner|founder|ceo|cto|cfo|president|sales|consultant|engineer|officer|executive)\b", line, re.I)
            and 2 <= len(line.split()) <= 4
            and all(len(word.strip("'.-")) >= 2 for word in line.split())
            and all(word.isupper() or (word[0].isupper() and word[1:].islower()) for word in line.split())
            and re.fullmatch(r"[A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){1,3}", line)
        ),
        None,
    )
    if not honorific_owner and not indic_honorific_owner and not indic_context_owner:
        person_candidates = [line for line in before_designation if line not in {company, designation}
                      and not promotional_text.search(line)
                      and not non_person_text.search(line)
                      and 2 <= len(line.split()) <= 4
                      and all(len(word.strip("'.-")) >= 2 for word in line.split())
                      and all(word.isupper() or (word[0].isupper() and word[1:].islower()) for word in line.split())
                      and re.fullmatch(r"[A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){1,3}", line)]
        owner = next((line for line in person_candidates if not line.isupper()), person_candidates[0] if person_candidates else owner)
        if owner and owner.isupper() and not designation:
            # All-caps OCR fragments without an honorific or a nearby title are
            # too ambiguous to treat as a person's name. Leave them for review.
            owner = None

    address_label = re.compile(r"^\s*(?:(?:office|registered|company)\s+)*address\s*[:#-]*\s*", re.I)
    explicit_address_indexes = [index for index, line in enumerate(lines) if address_label.search(line)]
    address_marker = re.compile(
        r"\b(road|rd\.?|street|st\.?|avenue|ave\.?|lane|ln\.?|suite|floor|block|sector|"
        r"near|complex|building|tower|plot|shop|area|landmark|district|state|province|region|city|town|pune|mumbai|kanpur|belapur|pimpri|"
        r"रोड|रस्ता|मार्ग|गली|नगर|बाजार|मार्केट|निकट|ऊपर|चौक|कानपुर|पुणे|महाराष्ट्र|पता|"
        r"\d{3}\s?\d{3}|\d{5,6})\b",
        re.I,
    )
    excluded_address_lines = {value.strip().casefold() for value in (company, owner, designation) if value}
    address_indexes = explicit_address_indexes or [
        index for index, line in enumerate(lines)
        if line.strip().casefold() not in excluded_address_lines
        and not re.match(r"^\s*(?:mob(?:ile)?|tel(?:ephone)?|fax|email)\b", line, re.I)
        and not phone_pattern.search(line) and not re.search(r"@|(?:https?://|www\.)", line, re.I)
        and address_marker.search(line)
    ]
    address_parts: list[str] = []
    if address_indexes:
        start = address_indexes[0]
        boundary = re.compile(r"@|https?://|www\.|\b(mob|tel\.?|telephone|mobile|phone|email|e-mail|web|website|fax|director|manager|ceo|owner|founder|mto|iso)\b", re.I)
        for index in range(start, min(len(lines), start + 5)):
            line = address_label.sub("", lines[index], count=1).strip(" ,;:-") if index == start else lines[index]
            if not line or line.strip().casefold() in excluded_address_lines or boundary.search(line):
                break
            phone_match = phone_pattern.search(line)
            if phone_match:
                line = line[:phone_match.start()].strip(" ,;:-")
                if not line:
                    break
            if index > start and not address_marker.search(line) and not re.search(r"\d", line):
                break
            address_parts.append(line)
    # Explicit labels always win over heuristic guesses. This is the key guard
    # against putting a labelled person/address/contact into the company field.
    labeled_contacts = explicit.get("contact_numbers", [])
    labeled_emails = explicit.get("email_addresses", [])
    # When a card labels mobile/contact numbers, do not pull digits from its
    # telephone, fax, WhatsApp URL, pincode, or registration number into it.
    contacts = list(dict.fromkeys(
        labeled_contacts if labeled_contacts else explicit.get("telephone_numbers", []) + contact_numbers
    ))
    emails = list(dict.fromkeys(labeled_emails + email_addresses))
    explicit_address = ", ".join(explicit.get("company_address", [])).strip()
    parsed = {
        "company_name": (explicit.get("company_name") or [company])[0],
        "owner_name": (explicit.get("owner_name") or [owner])[0],
        "designation": (explicit.get("designation") or [designation])[0],
        "business_description": (explicit.get("business_description") or [description])[0],
        "company_address": explicit_address or ", ".join(address_parts) or None,
        "contact_number": contacts[0] if contacts else None,
        "contact_numbers": contacts,
        "email": emails[0] if emails else None,
        "email_addresses": emails,
        "website": (explicit.get("website") or [website])[0],
        "telephone_numbers": explicit.get("telephone_numbers", []),
        "fax_numbers": explicit.get("fax_numbers", []),
        "facebook_link": (explicit.get("facebook_link") or [social_links.get("facebook_link")])[0],
        "instagram_link": (explicit.get("instagram_link") or [social_links.get("instagram_link")])[0],
        "whatsapp_link": (explicit.get("whatsapp_link") or [social_links.get("whatsapp_link")])[0],
        "city": (explicit.get("city") or [None])[0],
        "state": (explicit.get("state") or [None])[0],
        "pincode": (explicit.get("pincode") or [None])[0],
        "gst_number": (explicit.get("gst_number") or [None])[0],
        "registration_number": (explicit.get("registration_number") or [None])[0],
        "raw_text": _format_labeled_text(text),
    }
    if company_from_domain:
        parsed.setdefault("review_warnings", []).append(
            "Company name is a suggestion from the website domain; verify its spelling and complete registered name."
        )
    if recovered_email:
        parsed.setdefault("review_warnings", []).append(
            "An email domain separator was restored using the matching website; verify the email against the card."
        )
    # Stock business-card mockups print sample values that look like real
    # contacts (Lorem Ipsum, NAME SURNAME, sitename.domain, sample address).
    # Keep the transcript for verification, but never expose those values as
    # a lead preview. Two independent template markers are required so a
    # legitimate card with one unusual phrase is not suppressed.
    template_markers = (
        r"\blorem\s+ips\w*\b",
        r"\bname\s+surname\b",
        r"\bbusiness\s+company\b",
        r"\bsitename\.domain\b",
        r"\b123\s+address\s+avenue\b",
        r"\bsample\s+city\b",
        r"\blorem@ipsum\.com\b",
    )
    if sum(bool(re.search(marker, text, re.I)) for marker in template_markers) >= 2:
        for key in (
            "company_name", "owner_name", "designation", "company_address",
            "business_description",
            "contact_number", "contact_numbers", "email", "email_addresses",
            "website", "telephone_numbers", "fax_numbers", "facebook_link",
            "instagram_link", "whatsapp_link", "city", "state", "pincode",
            "gst_number", "registration_number",
        ):
            parsed[key] = [] if key in {"contact_numbers", "email_addresses", "telephone_numbers", "fax_numbers"} else None
        parsed["template_detected"] = True
        parsed["review_warnings"] = [
            "This image appears to be a business-card template with placeholder details. No lead fields were filled; verify with a real card."
        ]
    return parsed


def _decode_qr_payloads(image: Image.Image) -> list[str]:
    """Decode all visible QR codes without opening their URLs."""
    if cv2 is None or np is None:
        return []
    try:
        detector = cv2.QRCodeDetector()
        pixels = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
        decoded: list[str] = []
        ok, values, _, _ = detector.detectAndDecodeMulti(pixels)
        if ok and values is not None:
            decoded.extend(str(value).strip() for value in values if str(value).strip())
        # Printed card QR codes are often only 70–100 pixels wide in a phone
        # photo. OpenCV detects the finder pattern at native size but fails to
        # decode its payload; a single 2x interpolation pass fixes that case.
        if max(pixels.shape[:2]) < 2600:
            enlarged = cv2.resize(pixels, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            ok, values, _, _ = detector.detectAndDecodeMulti(enlarged)
            if ok and values is not None:
                decoded.extend(str(value).strip() for value in values if str(value).strip())
            pixels = enlarged
        if not decoded:
            value, _, _ = detector.detectAndDecode(pixels)
            if value and value.strip():
                decoded.append(value.strip())
        return list(dict.fromkeys(decoded))
    except (cv2.error, ValueError, TypeError):
        return []


def _merge_qr_fields(fields: dict[str, object], payloads: list[str]) -> dict[str, object]:
    if not payloads:
        fields["field_sources"] = _field_source_evidence(fields)
        return fields
    merged = dict(fields)
    parsed = [parse_qr_payload(payload) for payload in payloads]
    for key in ("company_name", "owner_name", "designation", "company_address", "website", "facebook_link", "instagram_link", "whatsapp_link"):
        values = [str(item[key]).strip() for item in parsed if item.get(key) and str(item[key]).strip()]
        # Structured QR payloads are explicit contact data; prefer them over
        # an ambiguous OCR guess when both sources contain a value.
        if values:
            merged[key] = values[0]
    for key, result_key in (("contact_numbers", "contacts"), ("email_addresses", "emails")):
        existing = list(merged.get(key) or [])
        for item in parsed:
            existing.extend(str(value).strip() for value in item.get(result_key, []) if str(value).strip())
        if existing:
            merged[key] = list(dict.fromkeys(existing))
            merged["contact_number" if key == "contact_numbers" else "email"] = merged[key][0]
    merged["qr_payloads"] = payloads
    merged["qr_data"] = [item for item in parsed if not item.get("unmapped")]
    merged["field_sources"] = _field_source_evidence(merged)
    return merged


def _field_source_evidence(fields: dict[str, object]) -> dict[str, dict[str, str]]:
    """Attach the OCR line or QR payload behind each populated preview value."""
    original_lines = [line.strip() for line in str(fields.get("original_ocr_text") or "").splitlines() if line.strip()]
    qr_data = fields.get("qr_data") if isinstance(fields.get("qr_data"), list) else []
    qr_keys = {
        "company_name": "company_name", "owner_name": "owner_name", "designation": "designation",
        "company_address": "company_address", "website": "website", "facebook_link": "facebook_link",
        "instagram_link": "instagram_link", "whatsapp_link": "whatsapp_link",
        "contact_numbers": "contacts", "email_addresses": "emails",
    }
    evidence: dict[str, dict[str, str]] = {}
    for key, qr_key in qr_keys.items():
        value = fields.get(key)
        values = value if isinstance(value, list) else [value]
        rendered = [str(item).strip() for item in values if item and str(item).strip()]
        if not rendered:
            continue
        matched_qr = next((
            item for item in qr_data
            if isinstance(item, dict) and any(str(candidate).strip() in rendered
               for candidate in ([item.get(qr_key)] if isinstance(item.get(qr_key), str) else item.get(qr_key, [])))
        ), None)
        if matched_qr:
            source_text = str(matched_qr.get("source_text") or "")
            evidence[key] = {"source_type": "QR", "source_text": source_text, "confidence": "high"}
            continue
        match = None
        span_size = 3 if key in {"company_name", "owner_name", "designation", "business_description", "company_address"} else 1
        for start in range(len(original_lines)):
            for end in range(start, min(len(original_lines), start + span_size)):
                source_span = "\n".join(original_lines[start:end + 1])
                latin_span = latin_name(source_span) or source_span
                if any(candidate.casefold() in source_span.casefold()
                       or (key in {"company_name", "owner_name", "designation", "business_description", "company_address"}
                           and candidate.casefold() in latin_span.casefold()) for candidate in rendered):
                    match = source_span
                    break
            if match:
                break
        if not match and key in {"contact_numbers", "email_addresses"}:
            match = next((line for line in original_lines if any(
                re.sub(r"\D", "", candidate) in re.sub(r"\D", "", line)
                if key == "contact_numbers" else candidate.casefold() in line.casefold()
                for candidate in rendered
            )), None)
        evidence[key] = {
            "source_type": "OCR",
            "source_text": match or "",
            # OCR line matching proves provenance, not visual recognition
            # confidence; request a quick check against the image.
            "confidence": "review",
        }
    return evidence


def _record_processing_times(fields: dict[str, object], started: float, ocr_ms: float) -> dict[str, object]:
    times = fields.get("processing_times_ms")
    if not isinstance(times, dict):
        times = {}
    times["ocr_ms"] = round(ocr_ms)
    times["total_ms"] = round((time.perf_counter() - started) * 1000)
    fields["processing_times_ms"] = times
    return fields


def extract_fields(path: Path, content_type: str) -> dict[str, object]:
    started = time.perf_counter()
    if content_type == "application/pdf":
        ocr_started = time.perf_counter()
        fields = _fields_from_ocr_text(_extract_text(path, content_type))
        return _record_processing_times(fields, started, (time.perf_counter() - ocr_started) * 1000)

    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        regions = _split_card_image(image)
        if regions and len(regions) > 1:
            cards: list[dict[str, object]] = []
            ocr_elapsed_ms = 0.0
            for region in regions:
                ocr_started = time.perf_counter()
                text, line_heights = _read_card_text(region)
                ocr_elapsed_ms += (time.perf_counter() - ocr_started) * 1000
                card = _merge_qr_fields(_fields_from_ocr_text(text, line_heights), _decode_qr_payloads(region))
                cards.append(_record_processing_times(card, started, ocr_elapsed_ms))
            cards = [card for card in cards if card.get("company_name") and (card.get("contact_numbers") or card.get("email_addresses") or card.get("website") or card.get("company_address"))]
            if len(cards) > 1:
                primary = cards[0].copy()
                primary["cards"] = cards
                return _record_processing_times(primary, started, ocr_elapsed_ms)
        # Card photos are frequently portrait-oriented because the phone was
        # held upright while the physical landscape card was sideways. OCR on
        # the unrotated pixels causes names and labels to swap fields.
        ocr_image = image.rotate(90, expand=True) if image.height > image.width * 1.25 else image
        # Tesseract cost grows sharply with pixel count. Keep crisp card text
        # while bounding memory and recognition time for full-resolution phone
        # captures; the original image remains stored untouched.
        longest_side = max(ocr_image.size)
        if longest_side > 2200:
            scale = 2200 / longest_side
            ocr_image = ocr_image.resize(
                (max(1, round(ocr_image.width * scale)), max(1, round(ocr_image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        ocr_started = time.perf_counter()
        text, line_heights = _read_card_text(ocr_image)
        ocr_ms = (time.perf_counter() - ocr_started) * 1000
        fields = _merge_qr_fields(_fields_from_ocr_text(text, line_heights), _decode_qr_payloads(image) + _decode_qr_payloads(ocr_image))
        return _record_processing_times(fields, started, ocr_ms)


def _fields_from_ocr_text(original_text: str, line_heights: dict[str, int] | None = None) -> dict[str, object]:
    fields = _parse_fields(original_text, line_heights)
    translation_source = _translation_focus_text(original_text)
    translation_started = time.perf_counter()
    translated, status = translate_document_text(translation_source)
    translation_ms = round((time.perf_counter() - translation_started) * 1000)
    if translated and status == "translated":
        heights = {after.casefold(): (line_heights or {}).get(before.casefold(), 0)
                   for before, after in zip(original_text.splitlines(), translated.splitlines())}
        english = _parse_fields(translated, heights if any(heights.values()) else None)
        # Do not let a translation model invent or modify a contact identifier.
        for key in ("contact_numbers", "contact_number", "email_addresses", "email", "website"):
            english[key] = fields.get(key)
        source_lines, english_lines = _clean_lines(original_text), _clean_lines(translated)
        for key in ("company_name", "owner_name", "designation", "business_description", "company_address"):
            original_value = fields.get(key)
            translated_value = english.get(key)
            # A translated value is usable only when it maps back to the same
            # source line. This prevents a translation/layout change from
            # moving an address, slogan, or business category into a name field.
            mapped_source = None
            if translated_value and len(source_lines) == len(english_lines):
                target = re.sub(r"\s+", " ", str(translated_value).casefold()).strip()
                for start in range(len(english_lines)):
                    for end in range(start + 1, min(len(english_lines), start + 4) + 1):
                        candidate = re.sub(r"\s+", " ", " ".join(english_lines[start:end]).casefold()).strip()
                        if candidate == target:
                            mapped_source = " ".join(source_lines[start:end])
                            break
                    if mapped_source:
                        break
            has_indic_source = bool(mapped_source and re.search(r"[\u0900-\u097f]", mapped_source))
            if key == "business_description" and original_value and not re.search(r"[\u0900-\u097f]", str(original_value)):
                # A service description assembled from clearly recognized
                # source terms is more reliable than a noisy machine sentence.
                english[key] = original_value
            elif key in {"business_description", "designation"} and translated_value and has_indic_source:
                english[key] = translated_value
            elif original_value and not re.search(r"[\u0900-\u097f]", str(original_value)):
                english[key] = original_value
            elif original_value and re.search(r"[\u0900-\u097f]", str(original_value)):
                # A company/person name is an identity, not copy to translate.
                # Keep a Latin transliteration for it while retaining the exact
                # original in original_ocr_text.
                rendered = latin_name(str(original_value)) or ""
                english[key] = " ".join(word.capitalize() for word in rendered.split())
                if translated_value and key == "company_name" and has_indic_source:
                    # Translate business-category words, but keep the leading
                    # brand token phonetic (for example Anmol, not Priceless).
                    source_tokens = re.findall(r"[\u0900-\u097f]+", mapped_source or "")
                    translated_tokens = str(translated_value).split()
                    if source_tokens and translated_tokens:
                        brand = latin_name(source_tokens[0]) or translated_tokens[0]
                        english[key] = " ".join([brand.capitalize(), *translated_tokens[1:]])
            elif translated_value and has_indic_source:
                if key == "company_name":
                    source_tokens = re.findall(r"[\u0900-\u097f]+", mapped_source or "")
                    translated_tokens = str(translated_value).split()
                    if not original_value:
                        english[key] = None
                    elif source_tokens and translated_tokens:
                        brand = latin_name(source_tokens[0]) or translated_tokens[0]
                        english[key] = " ".join([brand.capitalize(), *translated_tokens[1:]])
                    else:
                        english[key] = translated_value
                else:
                    english[key] = translated_value
            elif original_value and re.search(r"[\u0900-\u097f]", str(original_value)):
                english[key] = latin_name(str(original_value))
            elif translated_value:
                english[key] = None
            if english.get(key) != translated_value and translated_value:
                english["raw_text"] = str(english["raw_text"]).replace(str(translated_value), str(english.get(key) or ""))
        fields = english
        fields["translation_message"] = "Converted to English locally. Names use Latin-letter transliteration; check spelling and every field against the original before saving."
    elif re.search(r"[\u0900-\u097f]", original_text):
        # A production server may have Tesseract language data but not the
        # optional 2 GB translation model. Keep the review usable and prevent
        # mixed-script values from entering structured fields by using a
        # Latin-only transcript as a safe fallback. The untouched OCR remains
        # available in original_ocr_text.
        latin_text = latinize_document_text(original_text)
        if latin_text and not re.search(r"[\u0900-\u097f]", latin_text):
            latin_fields = _parse_fields(latin_text, line_heights)
            for key in ("contact_numbers", "contact_number", "email_addresses", "email", "website"):
                latin_fields[key] = fields.get(key)
            if not fields.get("company_name"):
                latin_fields["company_name"] = None
            if not fields.get("owner_name"):
                latin_fields["owner_name"] = None
            fields = latin_fields
            fields["translation_message"] = (
                "Hindi/Marathi business text was transliterated to Latin because semantic translation is unavailable. "
                "Check names and address before saving."
                if status != "unavailable" else
                "English translation is unavailable because the model is not installed; the preview uses a Latin-only transliteration/cleaned transcript. Verify names and address before saving."
            )
    contaminated_identity_fields = []
    for key in ("company_name", "owner_name", "designation"):
        value = str(fields.get(key) or "")
        if re.search(r"@|(?:https?://|www\.)|(?<!\d)\+?\d(?:[\s().-]*\d){6,14}(?!\d)", value, re.I):
            fields[key] = None
            contaminated_identity_fields.append(key.replace("_", " ").title())
    fields["original_ocr_text"] = original_text
    fields["translation_status"] = status
    field_review_warnings = list(fields.get("review_warnings") or [])
    if not fields.get("company_name"):
        field_review_warnings.append("Company name was not confidently identified. Enter it after checking the card.")
    if not fields.get("owner_name"):
        field_review_warnings.append("Owner/contact name was not confidently identified. Check the original card.")
    if not fields.get("designation"):
        field_review_warnings.append("No designation was confidently identified. Confirm whether one is printed on the card.")
    if not fields.get("email_addresses") and not fields.get("website"):
        field_review_warnings.append("No email address or website was confidently identified. Check the source card.")
    if (fields.get("company_name") and not re.search(r"\b(?:pvt\.?\s*ltd\.?|private\s+limited|limited|ltd\.?|llp|inc\.?|corporation|corp\.?)\b", str(fields["company_name"]), re.I)
            and re.search(r"\b(?:corpor|logis|industri|technolog)['’](?:\s|$)", original_text, re.I)):
        field_review_warnings.append("OCR appears to have truncated the company line; verify its complete registered name and legal suffix.")
    if contaminated_identity_fields:
        field_review_warnings.append(
            "OCR mixed contact or web data into " + ", ".join(contaminated_identity_fields) + "; those preview fields were left blank for review."
        )
    if re.search(r"[\u0900-\u097f]", original_text) and any(fields.get(key) for key in ("company_name", "owner_name")):
        field_review_warnings.append("Check transliterated company and person names against the original card before saving.")
    if field_review_warnings:
        fields["review_warnings"] = field_review_warnings
    if status == "unavailable" and not fields.get("translation_message"):
        fields["translation_message"] = "English translation is unavailable or incomplete. Retry after configuring the local translation model, or enter English values manually before saving. The original OCR text is preserved."
    processing_times = fields.get("processing_times_ms")
    if not isinstance(processing_times, dict):
        processing_times = {}
    processing_times["translation_ms"] = translation_ms
    fields["processing_times_ms"] = processing_times
    return fields
