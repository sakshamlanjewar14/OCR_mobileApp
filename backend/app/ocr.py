import re
import os
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import pytesseract
import pymupdf
from PIL import Image, ImageChops, ImageEnhance, ImageOps
from .translation import translate_document_text, latin_name


if os.getenv("TESSERACT_CMD"):
    pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]


class OCRUnavailableError(RuntimeError):
    """Raised when neither the configured OCR engine nor Windows OCR can run."""


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
        if len(re.sub(r"[^A-Za-z0-9]", "", best_text)) < 80:
            for angle in (90, 180, 270):
                rotated_image = image.rotate(angle, expand=True)
                rotated_text = recognize(rotated_image)
                if len(re.sub(r"[^A-Za-z0-9]", "", rotated_text)) > len(re.sub(r"[^A-Za-z0-9]", "", best_text)):
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
    # only when no email or website was found, then the richer result wins.
    if not re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|(?:https?://|www\.)\S+", text, re.I):
        enhanced = ImageEnhance.Contrast(ImageOps.grayscale(image)).enhance(1.8).convert("RGB")
        previous = image
        image = enhanced
        alternate, alternate_heights = read("--psm 12")
        image = previous
        alternate_score = len(re.findall(r"[\u0900-\u097F]", alternate)) + 3 * len(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\+?\d[\d ()-]{7,}", alternate, re.I))
        text_score = len(re.findall(r"[\u0900-\u097F]", text)) + 3 * len(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\+?\d[\d ()-]{7,}", text, re.I))
        if alternate_score > text_score:
            text, heights = alternate, alternate_heights
    if not re.search(r"[\u0900-\u097F]", text):
        indic_text, indic_heights = read("--psm 12")
        if len(re.findall(r"[\u0900-\u097F]", indic_text)) >= 3:
            text, heights = indic_text, indic_heights
    return (text or _read_image(image)), heights


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
    source_lines = [re.sub(r"\s+", " ", line.replace("\ufffd", "").replace("•", "")).strip(" ,;|\t") for line in text.splitlines() if line.strip()]
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


def _explicit_fields(text: str) -> dict[str, list[str]]:
    """Read labelled values before heuristic classification can mix fields."""
    labels = {
        "company name": "company_name", "business name": "company_name", "firm name": "company_name",
        "organization": "company_name", "organisation": "company_name", "company": "company_name",
        "owner name": "owner_name", "owner": "owner_name", "contact person": "owner_name",
        "person name": "owner_name", "director name": "owner_name", "founder name": "owner_name",
        "proprietor name": "owner_name",
        "designation": "designation", "job title": "designation", "position": "designation", "title": "designation",
        "address": "company_address", "company address": "company_address", "office address": "company_address",
        "registered office": "company_address", "location": "company_address",
        "website": "website", "web site": "website", "url": "website",
        "email": "email_addresses", "email id": "email_addresses", "e-mail": "email_addresses", "gmail": "email_addresses",
        "phone": "contact_numbers", "mobile": "contact_numbers", "contact no": "contact_numbers", "contact number": "contact_numbers",
        "telephone": "telephone_numbers", "tel": "telephone_numbers", "telephone number": "telephone_numbers",
        "fax": "fax_numbers", "fax number": "fax_numbers",
        "whatsapp": "whatsapp_link", "whatsapp link": "whatsapp_link", "facebook": "facebook_link",
        "facebook link": "facebook_link", "instagram": "instagram_link", "instagram link": "instagram_link",
        "city": "city", "state": "state", "pincode": "pincode", "pin code": "pincode",
        "gst": "gst_number", "gst number": "gst_number", "registration number": "registration_number",
    }
    source = [re.sub(r"\s+", " ", line.replace("\ufffd", "")).strip() for line in text.splitlines()]
    found: dict[str, list[str]] = {}
    for index, line in enumerate(source):
        match = re.match(r"^\s*([A-Za-z][A-Za-z ]{1,35}?)(?:\s*\d+)?\s*[:#-]\s*(.*)$", line)
        if not match:
            match = re.match(r"^\s*([A-Za-z][A-Za-z ]{1,35}?)(?:\s*\d+)?\s*$", line)
            value = source[index + 1].strip() if match and index + 1 < len(source) else ""
        else:
            value = match.group(2).strip()
        if not match:
            continue
        key = re.sub(r"\s+", " ", match.group(1).strip().lower())
        target = labels.get(key)
        if target and value:
            found.setdefault(target, []).append(value)
    return found


def _parse_fields(text: str, line_heights: dict[str, int] | None = None) -> dict[str, object]:
    text = "".join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in text)
    explicit = _explicit_fields(text)
    lines = _clean_lines(text)
    full_text = "\n".join(lines)

    email_matches = re.finditer(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", full_text, re.I)
    email_addresses = list(dict.fromkeys(match.group(0).rstrip(".,;") for match in email_matches))
    website_match = re.search(
        r"(?<![@\w.-])(?:https?://)?(?:www\.)?[A-Z0-9-]+(?:\.[A-Z0-9-]+)*\.[A-Z]{2,}(?:/[^\s]*)?(?![\w@.-])",
        full_text,
        re.I,
    )
    contact_numbers: list[str] = []
    phone_pattern = re.compile(r"(?<!\d)\+?\d(?:[ \t().-]*\d){6,14}(?!\d)")
    for line in lines:
        for match in phone_pattern.finditer(line):
            number = re.sub(r"\s+", " ", match.group(0)).strip()
            digits = re.sub(r"\D", "", number)
            if len(digits) >= 7 and number not in contact_numbers:
                contact_numbers.append(number)

    email = email_addresses[0] if email_addresses else None
    website = website_match.group(0).rstrip(".,;") if website_match else None

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
        r"association|societ(?:y|ies|y)|scociety|institutes?|universit(?:y|ies)|bank|hospitals?|clinic|schools?|"
        r"colleges?|academ(?:y|ies)|estate|estates|real\s+estate|realstates?|pharma(?:ceutical)?s?|pharamasetical|"
        r"business|innovations?|inovation|advertising|enterprises?|spaces?|hotels?|lodges?|laudge|systems?|"
        r"restaurants?|restorents|finance|loans?|developers?|associates?|education|consultan(?:t|cy|ts|cies)|"
        r"movers?|packers?|packagers?|automation|ngo|classes|pvt\.?)\b",
        re.I,
    )
    designation_markers = re.compile(
        r"\b(director|manager|owner|founder|ceo|cto|cfo|president|sales|consultant|engineer|officer|executive|"
        r"specialist|analyst|designer|architect|administrator|coordinator|developer|accountant|partner|head)\b",
        re.I,
    )
    honorific_owner = next((
        line for line in candidates
        if re.match(r"^(?:mr|mrs|ms|miss|dr|prof)\.?\s+[\w\u00c0-\u024f]", line, re.I)
    ), None)
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
    if company is None:
        company_candidates = [
            line for line in candidates
            if line != honorific_owner and not designation_markers.search(line)
            and not re.match(r"^(?:mr|mrs|ms|miss|dr|prof)\.?\s+", line, re.I)
        ]
        if company_candidates:
            heights = line_heights or {}
            company = max(company_candidates, key=lambda line: heights.get(line.casefold(), 0)) if heights else company_candidates[0]

    # OCR commonly places legal suffixes on their own line (for example,
    # ``Example Industries Pvt.`` followed by ``Ltd.``). Keep those tokens in
    # the company name instead of losing the complete registered name.
    legal_suffix_only = re.compile(
        r"^(?:(?:private|pvt\.?|limited|ltd\.?|llp|incorporated|inc\.?|"
        r"corporation|corp\.?|company|co\.?)\s*)+$",
        re.I,
    )
    if company:
        company_index = candidates.index(company)
        if company_index > 0 and legal_suffix_only.fullmatch(company):
            company = f"{candidates[company_index - 1]} {company}"
        elif company_index + 1 < len(candidates) and legal_suffix_only.fullmatch(candidates[company_index + 1]):
            company = f"{company} {candidates[company_index + 1]}"
    promotional_text = re.compile(
        r"\b(certified|authori[sz]ed|official|channel|solutions?|provider|dealer|distributor|welcome|quality|trusted|leading|serving)\b",
        re.I,
    )
    designation = next(
        (
            line for line in lines
            if (designation_markers.search(line) or re.fullmatch(r"(?:managing\s+)?partner", line, re.I))
            and not promotional_text.search(line) and line != company
        ),
        None,
    )
    # When a card has no explicit company label or legal suffix, a branded
    # website is safer evidence than treating a person's prominent name as a
    # company. Use the registered domain as a review candidate.
    if company and not company_markers.search(company) and website:
        host = re.sub(r"^https?://|^www\.", "", website, flags=re.I).split(".")[0]
        if host and host.casefold() not in {"gmail", "yahoo", "google", "facebook", "instagram"}:
            company = re.sub(r"[-_]+", " ", host).title()
    before_designation = candidates[:candidates.index(designation)] if designation in candidates else candidates
    owner = honorific_owner or next(
        (
            line for line in candidates
            if line not in {company, designation}
            and not promotional_text.search(line)
            and not re.search(r"\b(partner|director|manager|owner|founder|ceo|cto|cfo|president|sales|consultant|engineer|officer|executive)\b", line, re.I)
            and 2 <= len(line.split()) <= 4
            and re.fullmatch(r"[A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){1,3}", line)
        ),
        None,
    )
    if not honorific_owner:
        person_candidates = [line for line in before_designation if line not in {company, designation}
                      and not promotional_text.search(line)
                      and 2 <= len(line.split()) <= 4
                      and re.fullmatch(r"[A-Za-z][A-Za-z'.-]*(?:\s+[A-Za-z][A-Za-z'.-]*){1,3}", line)]
        owner = next((line for line in person_candidates if not line.isupper()), person_candidates[0] if person_candidates else owner)

    address_label = re.compile(r"^\s*(?:(?:office|registered|company)\s+)*address\s*[:#-]*\s*", re.I)
    explicit_address_indexes = [index for index, line in enumerate(lines) if address_label.search(line)]
    address_marker = re.compile(
        r"\b(road|rd\.?|street|st\.?|avenue|ave\.?|lane|ln\.?|suite|floor|block|sector|"
        r"near|complex|building|tower|plot|area|landmark|district|state|province|region|city|town|"
        r"\d{3}\s?\d{3}|\d{5,6})\b",
        re.I,
    )
    excluded_address_lines = {value.strip().casefold() for value in (company, owner, designation) if value}
    address_indexes = explicit_address_indexes or [
        index for index, line in enumerate(lines)
        if line.strip().casefold() not in excluded_address_lines and address_marker.search(line)
    ]
    address_parts: list[str] = []
    if address_indexes:
        start = address_indexes[0]
        boundary = re.compile(r"@|https?://|www\.|\b(tel\.?|telephone|mobile|phone|email|e-mail|web|website|fax|director|manager|ceo|owner|founder|mto|iso)\b", re.I)
        for index in range(start, len(lines)):
            line = address_label.sub("", lines[index], count=1).strip(" ,;:-") if index == start else lines[index]
            if not line or line.strip().casefold() in excluded_address_lines or boundary.search(line):
                break
            phone_match = phone_pattern.search(line)
            if phone_match:
                line = line[:phone_match.start()].strip(" ,;:-")
                if not line:
                    break
            address_parts.append(line)
    # Explicit labels always win over heuristic guesses. This is the key guard
    # against putting a labelled person/address/contact into the company field.
    labeled_contacts = explicit.get("contact_numbers", [])
    labeled_emails = explicit.get("email_addresses", [])
    # When a card labels mobile/contact numbers, do not pull digits from its
    # telephone, fax, WhatsApp URL, pincode, or registration number into it.
    contacts = list(dict.fromkeys(labeled_contacts if labeled_contacts else contact_numbers))
    emails = list(dict.fromkeys(labeled_emails + email_addresses))
    explicit_address = ", ".join(explicit.get("company_address", [])).strip()
    return {
        "company_name": (explicit.get("company_name") or [company])[0],
        "owner_name": (explicit.get("owner_name") or [owner])[0],
        "designation": (explicit.get("designation") or [designation])[0],
        "company_address": explicit_address or ", ".join(address_parts) or None,
        "contact_number": contacts[0] if contacts else None,
        "contact_numbers": contacts,
        "email": emails[0] if emails else None,
        "email_addresses": emails,
        "website": (explicit.get("website") or [website])[0],
        "telephone_numbers": explicit.get("telephone_numbers", []),
        "fax_numbers": explicit.get("fax_numbers", []),
        "facebook_link": (explicit.get("facebook_link") or [None])[0],
        "instagram_link": (explicit.get("instagram_link") or [None])[0],
        "whatsapp_link": (explicit.get("whatsapp_link") or [None])[0],
        "city": (explicit.get("city") or [None])[0],
        "state": (explicit.get("state") or [None])[0],
        "pincode": (explicit.get("pincode") or [None])[0],
        "gst_number": (explicit.get("gst_number") or [None])[0],
        "registration_number": (explicit.get("registration_number") or [None])[0],
        "raw_text": _format_labeled_text(text),
    }


def extract_fields(path: Path, content_type: str) -> dict[str, object]:
    if content_type == "application/pdf":
        return _fields_from_ocr_text(_extract_text(path, content_type))

    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        regions = _split_card_image(image)
        if regions and len(regions) > 1:
            cards: list[dict[str, object]] = []
            for region in regions:
                text, line_heights = _read_image_with_layout(region)
                cards.append(_fields_from_ocr_text(text, line_heights))
            cards = [card for card in cards if card.get("company_name") and (card.get("contact_numbers") or card.get("email_addresses") or card.get("website") or card.get("company_address"))]
            if len(cards) > 1:
                primary = cards[0].copy()
                primary["cards"] = cards
                return primary
        # Card photos are frequently portrait-oriented because the phone was
        # held upright while the physical landscape card was sideways. OCR on
        # the unrotated pixels causes names and labels to swap fields.
        ocr_image = image.rotate(90, expand=True) if image.height > image.width * 1.25 else image
        text, line_heights = _read_image_with_layout(ocr_image)
        return _fields_from_ocr_text(text, line_heights)


def _fields_from_ocr_text(original_text: str, line_heights: dict[str, int] | None = None) -> dict[str, object]:
    fields = _parse_fields(original_text, line_heights)
    translated, status = translate_document_text(original_text)
    if translated and status == "translated":
        heights = {after.casefold(): (line_heights or {}).get(before.casefold(), 0)
                   for before, after in zip(original_text.splitlines(), translated.splitlines())}
        english = _parse_fields(translated, heights if any(heights.values()) else None)
        # Do not let a translation model invent or modify a contact identifier.
        for key in ("contact_numbers", "contact_number", "email_addresses", "email", "website"):
            english[key] = fields.get(key)
        source_lines, english_lines = _clean_lines(original_text), _clean_lines(translated)
        if len(source_lines) == len(english_lines):
            for key in ("company_name", "owner_name"):
                value = english.get(key)
                if value in english_lines:
                    source = source_lines[english_lines.index(value)]
                    if re.search(r"[\u0900-\u097f]", source):
                        rendered = latin_name(source)
                        english[key] = rendered
                        english["raw_text"] = str(english["raw_text"]).replace(str(value), str(rendered))
        fields = english
        fields["translation_message"] = "Converted to English locally. Names use Latin-letter transliteration; check spelling and every field against the original before saving."
    fields["original_ocr_text"] = original_text
    fields["translation_status"] = status
    if status == "unavailable":
        fields["translation_message"] = "English translation is unavailable or incomplete. Retry after configuring the local translation model, or enter English values manually before saving. The original OCR text is preserved."
    return fields
