import csv
import io
import json
import os
import re
import shutil
import textwrap
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine, get_db
from .models import Document, Lead, LeadContact, LeadSourceImage
from .ocr import OCRUnavailableError, _parse_fields, extract_fields
from .qr import parse_qr_payload
from .geocoding import reverse_geocode
from .schemas import DocumentRead, DocumentStatusRead, DocumentStatusRequest, LeadCreate, LeadFields, LeadRead, LeadUpdate, QRParseRequest, ReverseGeocodeRequest


UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads")).resolve()
ALLOWED_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif", "application/pdf",
}
TYPE_SUFFIXES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/tiff": ".tiff", "image/gif": ".gif", "application/pdf": ".pdf",
}
DOCUMENT_SUFFIXES = set(TYPE_SUFFIXES) | {".zip"}

app = FastAPI(title="Lead Gennie API", version="0.1.0")
DEFAULT_CORS_ORIGINS = ",".join(
    f"http://{host}:{port}"
    for host in ("localhost", "127.0.0.1")
    for port in range(5173, 5177)
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGINS).split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
def initialize_database() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "postgresql":
        columns = {column["name"] for column in inspect(engine).get_columns("leads")}
        document_unique_constraints = [
            item["name"] for item in inspect(engine).get_unique_constraints("leads")
            if item.get("column_names") == ["document_id"] and item.get("name")
        ]
        additions = {
            "designation": "VARCHAR(120)",
            "raw_text": "TEXT",
            "verification_status": "VARCHAR(20) NOT NULL DEFAULT 'verified'",
            "source_card_index": "INTEGER",
            "facebook_link": "VARCHAR(500)",
            "instagram_link": "VARCHAR(500)",
            "whatsapp_link": "VARCHAR(500)",
            "original_ocr_text": "TEXT",
        }
        with engine.begin() as connection:
            for constraint_name in document_unique_constraints:
                quoted_name = constraint_name.replace('"', '""')
                connection.execute(text(f'ALTER TABLE leads DROP CONSTRAINT "{quoted_name}"'))
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE leads ADD COLUMN {name} {definition}"))
            # Move old JSON arrays and scalar primary contacts into one relational
            # row per value before dropping all legacy contact columns.
            legacy_contact_columns = [name for name in ("contact_numbers", "email_addresses") if name in columns]
            selected_columns = ["id"]
            selected_columns.extend(name for name in ("contact_number", "email") if name in columns)
            selected_columns.extend(name for name in legacy_contact_columns if name not in selected_columns)
            rows = connection.execute(text(
                "SELECT " + ", ".join(selected_columns) + " FROM leads"
            )).mappings().all()
            for row in rows:
                for field, kind, fallback in (
                    ("contact_numbers", "phone", row.get("contact_number")),
                    ("email_addresses", "email", row.get("email")),
                ):
                    raw_values = row.get(field) if field in row else None
                    if isinstance(raw_values, str):
                        try:
                            raw_values = json.loads(raw_values)
                        except json.JSONDecodeError:
                            raw_values = [raw_values]
                    values = raw_values if isinstance(raw_values, list) else []
                    if fallback and fallback not in values:
                        values = [fallback, *values]
                    for position, value in enumerate(dict.fromkeys(str(value).strip() for value in values if value and str(value).strip())):
                        connection.execute(text(
                            "INSERT INTO lead_contacts (id, lead_id, contact_type, value, position) "
                            "VALUES (:id, :lead_id, :contact_type, :value, :position) "
                            "ON CONFLICT (lead_id, contact_type, value) DO NOTHING"
                        ), {"id": str(uuid.uuid4()), "lead_id": row["id"], "contact_type": kind,
                            "value": value, "position": position})
            for name in [column for column in ("contact_numbers", "email_addresses", "contact_number", "email") if column in columns]:
                connection.execute(text(f"ALTER TABLE leads DROP COLUMN {name}"))
            legacy_locations = [name for name in ("city", "district", "state") if name in columns]
            if legacy_locations:
                address_parts = ["NULLIF(BTRIM(company_address), '')"]
                for name in legacy_locations:
                    address_parts.append(
                        f"CASE WHEN NULLIF(BTRIM({name}), '') IS NOT NULL "
                        f"AND POSITION(LOWER(BTRIM({name})) IN LOWER(COALESCE(company_address, ''))) = 0 "
                        f"THEN BTRIM({name}) END"
                    )
                connection.execute(text(
                    "UPDATE leads SET company_address = NULLIF(CONCAT_WS(', ', "
                    + ", ".join(address_parts)
                    + "), '')"
                ))
                for name in legacy_locations:
                    connection.execute(text(f"ALTER TABLE leads DROP COLUMN {name}"))

    # Background tasks live in this local API process. If it was restarted in
    # the middle of OCR, keep the uploaded file and make the interrupted job
    # visible and retryable instead of leaving it stuck as "processing".
    with SessionLocal() as db:
        interrupted = db.scalars(select(Document).where(
            Document.processing_status.in_(["uploaded", "processing"])
        )).all()
        for document in interrupted:
            document.processing_status = "failed"
            document.error_message = "Scanning was interrupted. Retry the scan; the uploaded source file is still available."
        if interrupted:
            db.commit()



def _read_document(document: Document) -> DocumentRead:
    linked_leads = {lead.id: lead for lead in document.leads}
    linked_leads.update({source.lead.id: source.lead for source in document.source_links})
    linked_lead = next(iter(linked_leads.values()), None)
    return DocumentRead(
        id=document.id,
        original_filename=document.original_filename,
        content_type=document.content_type,
        processing_status=document.processing_status,
        extracted_fields=document.extracted_fields or {},
        error_message=document.error_message,
        created_at=document.created_at,
        lead_id=linked_lead.id if linked_lead else None,
        lead_count=len(linked_leads),
        resolved_card_indexes=sorted({lead.source_card_index for lead in document.leads if lead.source_card_index is not None}),
    )


def _process_document(document_id: str) -> None:
    db = SessionLocal()
    try:
        document = db.get(Document, document_id)
        if not document:
            return
        document.processing_status = "processing"
        document.error_message = None
        db.commit()
        try:
            fields = extract_fields(Path(document.storage_path), document.content_type)
            document.extracted_fields = fields
            # OCR results are only candidates. A user must review and save them
            # explicitly so an OCR mismatch cannot silently become a lead.
            document.processing_status = "needs_review"
            candidate = fields.get("cards")
            first_card = candidate[0] if isinstance(candidate, list) and candidate else fields
            has_company = isinstance(first_card, dict) and bool(str(first_card.get("company_name") or "").strip())
            has_other_data = isinstance(first_card, dict) and any(
                first_card.get(key) for key in ("owner_name", "designation", "company_address",
                                                 "contact_numbers", "email_addresses", "website",
                                                 "facebook_link", "instagram_link", "whatsapp_link")
            )
            if has_company:
                document.error_message = None
            elif has_other_data:
                document.error_message = (
                    "No company name was found. This card may contain only QR contact, email, phone, "
                    "or address data. Review the extracted fields and enter the company name manually before saving; "
                    "you can enter the details manually."
                )
            else:
                document.error_message = (
                    "No usable company or contact data was found. Make sure the card text and QR code are clear, "
                    "then retry the scan or enter the details manually."
                )
            db.commit()
        except Exception as exc:  # Preserve the upload and allow manual entry or retry.
            db.rollback()
            document = db.get(Document, document_id)
            if not document:
                return
            document.processing_status = "failed"
            if isinstance(exc, OCRUnavailableError):
                document.error_message = str(exc)
            elif exc.__class__.__name__ == "TesseractNotFoundError":
                document.error_message = "OCR engine is unavailable. Install Tesseract or enter the lead details manually."
            elif isinstance(exc, ValueError):
                document.error_message = str(exc)
            else:
                document.error_message = "Could not read or save this document. Check the file and try again, or enter details manually."
            db.commit()
    finally:
        db.close()


def _content_type_for_header(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "image/tiff"
    return None


def _safe_filename(filename: str) -> str:
    return PurePosixPath(filename.replace("\\", "/")).name or "upload"


def _create_document_from_stream(db: Session, filename: str, stream: BinaryIO) -> Document:
    filename = _safe_filename(filename)
    header = stream.read(16)
    content_type = _content_type_for_header(header)
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail=f"{filename} is not a supported image or PDF file.")
    document_id = str(uuid.uuid4())
    path = UPLOAD_DIR / f"{document_id}{TYPE_SUFFIXES[content_type]}"
    try:
        with path.open("wb") as output:
            output.write(header)
            shutil.copyfileobj(stream, output)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    document = Document(
        id=document_id,
        original_filename=filename[:255],
        content_type=content_type,
        storage_path=str(path),
        processing_status="uploaded",
        extracted_fields={},
    )
    db.add(document)
    return document


def _documents_from_zip(db: Session, filename: str, stream: BinaryIO) -> list[Document]:
    documents: list[Document] = []
    stream.seek(0)
    try:
        with zipfile.ZipFile(stream) as archive:
            with db.begin_nested():
                for entry in archive.infolist():
                    member_name = entry.filename.replace("\\", "/")
                    safe_name = _safe_filename(member_name)
                    if entry.is_dir() or safe_name.startswith(".") or Path(safe_name).suffix.lower() not in TYPE_SUFFIXES.values():
                        continue
                    with archive.open(entry) as member:
                        try:
                            documents.append(_create_document_from_stream(db, safe_name, member))
                        except HTTPException as exc:
                            if exc.status_code != 415:
                                raise
    except Exception as exc:
        for document in documents:
            Path(document.storage_path).unlink(missing_ok=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=400, detail=f"{filename} could not be read as a valid ZIP archive.") from exc
    if not documents:
        raise HTTPException(status_code=400, detail="No supported images or PDFs were found in the ZIP archive.")
    return documents


def _process_documents(document_ids: list[str]) -> None:
    for document_id in document_ids:
        _process_document(document_id)


def _normalize_india_phone(value: str) -> str:
    original = value.strip()
    digits = re.sub(r"\D", "", original)
    if original.startswith("+"):
        return original
    if len(digits) == 10:
        return f"+91 {digits}"
    if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        return f"+91 {digits[1:]}"
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return f"+{digits[:2]} {digits[2:]}"
    if digits.startswith("0091") and len(digits) == 14 and digits[4] in "6789":
        return f"+91 {digits[4:]}"
    return original


def _lead_read(lead: Lead) -> LeadRead:
    result = LeadRead.model_validate(lead)
    result.contact_numbers = [_normalize_india_phone(value) for value in result.contact_numbers]
    result.contact_number = result.contact_numbers[0] if result.contact_numbers else None
    return result


def _filter_lead_statement(
    statement,
    *,
    company_name: str | None = None,
    owner_name: str | None = None,
    city: str | None = None,
    state: str | None = None,
    search: str | None = None,
):
    for column, value in (
        (Lead.company_name, company_name),
        (Lead.owner_name, owner_name),
        (Lead.company_address, city),
        (Lead.company_address, state),
    ):
        if value and value.strip():
            statement = statement.where(column.ilike(f"%{value.strip()}%"))
    if search and search.strip():
        term = f"%{search.strip()}%"
        statement = statement.where(or_(
            Lead.lead_number.ilike(term), Lead.company_name.ilike(term), Lead.owner_name.ilike(term),
            Lead.designation.ilike(term),
            Lead.id.in_(select(LeadContact.lead_id).where(LeadContact.value.ilike(term))),
            Lead.website.ilike(term), Lead.facebook_link.ilike(term), Lead.instagram_link.ilike(term),
            Lead.whatsapp_link.ilike(term), Lead.company_address.ilike(term), Lead.raw_text.ilike(term),
        ))
    return statement


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/qr/parse")
def parse_qr(payload: QRParseRequest):
    """Classify scanned QR data locally; this endpoint never opens its URLs."""
    return parse_qr_payload(payload.payload)


@app.post("/api/geocoding/reverse")
def resolve_coordinates(payload: ReverseGeocodeRequest):
    """Explicit caller-triggered lookup; only coordinates go to configured Nominatim."""
    try:
        return reverse_geocode(payload.latitude, payload.longitude)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        # Do not return upstream response content, URLs, or request metadata.
        raise HTTPException(status_code=502, detail="Address lookup is temporarily unavailable. Keep or edit the address manually.") from exc


@app.post("/api/documents", response_model=DocumentRead, status_code=202)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    filename = _safe_filename(file.filename or "upload")
    document = _create_document_from_stream(db, filename, file.file)
    db.commit()
    db.refresh(document)
    background_tasks.add_task(_process_document, document.id)
    return _read_document(document)


@app.post("/api/documents/batch", status_code=202)
async def upload_document_batch(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    documents: list[Document] = []
    skipped_files: list[dict[str, str]] = []
    for upload in files:
        filename = _safe_filename(upload.filename or "upload")
        try:
            if Path(filename).suffix.lower() == ".zip":
                documents.extend(_documents_from_zip(db, filename, upload.file))
            else:
                documents.append(_create_document_from_stream(db, filename, upload.file))
        except HTTPException as exc:
            skipped_files.append({"filename": filename, "reason": str(exc.detail)})
    if not documents:
        raise HTTPException(status_code=415, detail="No supported image or PDF documents were found.")
    try:
        db.commit()
        for document in documents:
            db.refresh(document)
    except Exception:
        db.rollback()
        for document in documents:
            Path(document.storage_path).unlink(missing_ok=True)
        raise
    background_tasks.add_task(_process_documents, [document.id for document in documents])
    return {"documents": [_read_document(document) for document in documents], "skipped_files": skipped_files}


RAW_FIELD_HEADERS = {
    "company_name": {"company", "company name", "company full name", "full company name", "legal company name", "registered company name", "business", "business name", "firm", "firm name", "organization", "organization name", "organisation", "organisation name"},
    "owner_name": {"owner", "owner name", "owner full name", "contact person", "contact person name", "primary contact name", "contact name", "person name", "full name", "proprietor", "proprietor name", "director name"},
    "designation": {"designation", "job title", "title", "position", "role", "work title"},
    "company_address": {"address", "company address", "office address", "business address", "registered address", "registered office", "office location", "location"},
    "website": {"website", "web site", "company website", "website url", "web address", "company url", "url", "domain"},
    "facebook_link": {"facebook", "facebook link", "facebook url", "fb link"},
    "instagram_link": {"instagram", "instagram link", "instagram url", "ig link"},
    "whatsapp_link": {"whatsapp link", "whatsapp url", "whatsapp chat link"},
}
RAW_TEXT_HEADERS = {
    "other document text", "other text from document", "other text", "document text", "source text", "raw text",
    "company work data", "company description", "business description", "company profile", "business overview",
}
RAW_PHONE_HEADERS = {
    "phone", "phones", "phone no", "phone nos", "phone number", "phone numbers", "primary phone", "secondary phone", "business phone", "office phone",
    "mobile", "mobiles", "mobile no", "mobile nos", "mobile number", "mobile numbers", "primary mobile", "secondary mobile", "telephone",
    "telephone no", "telephone number", "contact no", "contact nos", "contact number", "contact numbers", "contact phone", "contact mobile",
    "cell", "cell no", "cell number", "whatsapp", "whatsapp no", "whatsapp number",
}
RAW_EMAIL_HEADERS = {
    "email", "emails", "e mail", "email id", "email ids", "email address", "email addresses", "primary email", "secondary email", "business email",
    "work email", "official email", "personal email", "contact email", "office email", "mail id", "mail address",
}


def _normalized_header(header: str) -> str:
    key = re.sub(r"[^a-z0-9]+", " ", header.casefold()).strip()
    return re.sub(r"\s+\d+$", "", key).strip()


def _raw_cell_text(value: object) -> str:
    """Avoid importing an integer phone cell from Excel with a trailing .0."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _raw_row_fields(headers: list[str], values: list[object]) -> dict[str, object]:
    pairs = [(str(header or "").strip(), values[index] if index < len(values) else None)
             for index, header in enumerate(headers)]
    mapped: dict[str, object] = {"contact_numbers": [], "email_addresses": []}
    recognized_pairs: list[tuple[str, object]] = []
    has_company_header = any(_normalized_header(header) in RAW_FIELD_HEADERS["company_name"] for header in headers)
    for header, value in pairs:
        if value in (None, ""):
            continue
        key = _normalized_header(header)
        field = next((field for field, aliases in RAW_FIELD_HEADERS.items() if key in aliases), None)
        if field:
            mapped[field] = _raw_cell_text(value)
            recognized_pairs.append((header, _raw_cell_text(value)))
        elif key in RAW_PHONE_HEADERS:
            mapped["contact_numbers"].extend(part.strip() for part in re.split(r"[\r\n;,/]+", _raw_cell_text(value)) if part.strip())
            recognized_pairs.append((header, _raw_cell_text(value)))
        elif key in RAW_EMAIL_HEADERS:
            mapped["email_addresses"].extend(part.strip() for part in re.split(r"[\r\n;,]+", _raw_cell_text(value)) if part.strip())
            recognized_pairs.append((header, _raw_cell_text(value)))
        elif key in RAW_TEXT_HEADERS:
            recognized_pairs.append((header, _raw_cell_text(value)))
    raw_text = "\n".join(f"{header}: {value}" for header, value in recognized_pairs)
    if not has_company_header:
        parsed = _parse_fields(raw_text)
        for field in ("company_name", "owner_name", "designation", "company_address", "website"):
            if parsed.get(field):
                mapped[field] = parsed[field]
        mapped["contact_numbers"].extend(parsed.get("contact_numbers", []))
        mapped["email_addresses"].extend(parsed.get("email_addresses", []))
    mapped["contact_numbers"] = list(dict.fromkeys(mapped["contact_numbers"]))
    mapped["email_addresses"] = list(dict.fromkeys(mapped["email_addresses"]))
    mapped["raw_text"] = raw_text or None
    return mapped


def _find_raw_header_row(rows: list[list[object]], scan_limit: int = 25) -> int | None:
    """Find recognized tabular headers even when a workbook has blank/title rows above them."""
    aliases = [*RAW_FIELD_HEADERS.values(), RAW_TEXT_HEADERS, RAW_PHONE_HEADERS, RAW_EMAIL_HEADERS]
    best_index: int | None = None
    best_score = 0
    for index, row in enumerate(rows[:scan_limit]):
        headers = [str(value or "").strip() for value in row]
        score = sum(
            1 for header in headers
            if any(_normalized_header(header) in field_aliases for field_aliases in aliases)
        )
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def _raw_candidates_from_rows(rows: list[list[object]]) -> list[dict[str, object]]:
    header_index = _find_raw_header_row(rows)
    has_known_headers = header_index is not None
    headers = [str(value or "").strip() for value in rows[header_index]] if has_known_headers else []
    data_start = header_index + 1 if header_index is not None else 0
    candidates = []
    for row_number, row in enumerate(rows[data_start:], start=data_start + 1):
        if not any(value not in (None, "") for value in row):
            continue
        row_headers = headers if has_known_headers else [f"Column {index + 1}" for index in range(len(row))]
        candidates.append({"row": row_number, "fields": _raw_row_fields(row_headers, list(row))})
    return candidates


@app.post("/api/imports/leads/preview")
async def preview_raw_leads(file: UploadFile = File(...)):
    filename = _safe_filename(file.filename or "raw-data")
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".txt":
            source = (await file.read()).decode("utf-8-sig")
            fields = _parse_fields(source)
            return {"filename": filename, "candidates": [{"row": 1, "fields": fields}]}
        if suffix == ".csv":
            source = (await file.read()).decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(source)))
        elif suffix == ".xlsx":
            workbook = load_workbook(file.file, read_only=True, data_only=True)
            try:
                rows = [list(row) for row in workbook.active.iter_rows(values_only=True)]
            finally:
                workbook.close()
        else:
            raise HTTPException(status_code=415, detail="Choose a UTF-8 TXT, CSV, or XLSX raw-data file.")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Text and CSV files must use UTF-8 encoding.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Could not read this raw-data file.") from exc

    if not rows:
        raise HTTPException(status_code=400, detail="The raw-data file is empty.")
    candidates = _raw_candidates_from_rows(rows)
    if not candidates:
        raise HTTPException(status_code=400, detail="No non-empty lead rows were found.")
    return {"filename": filename, "candidates": candidates}


@app.get("/api/documents/pending", response_model=list[DocumentRead])
def list_pending_documents(db: Session = Depends(get_db)):
    statement = select(Document).where(
        Document.processing_status.in_(["uploaded", "processing", "needs_review", "failed"])
    ).order_by(Document.created_at.desc())
    return db.scalars(statement).all()


@app.post("/api/documents/status", response_model=list[DocumentStatusRead])
def get_document_statuses(payload: DocumentStatusRequest, db: Session = Depends(get_db)):
    # Return compact OCR status during polling without loading extracted text.
    document_ids = list(dict.fromkeys(payload.ids))
    statuses: list[dict[str, object]] = []
    for start in range(0, len(document_ids), 1000):
        chunk = document_ids[start:start + 1000]
        documents = db.scalars(select(Document).where(Document.id.in_(chunk))).all()
        statuses.extend({
            "id": document.id,
            "processing_status": document.processing_status,
            "error_message": document.error_message,
            "lead_count": len({lead.id for lead in document.leads} | {source.lead.id for source in document.source_links}),
        } for document in documents)
    return statuses


@app.get("/api/documents/{document_id}", response_model=DocumentRead)
def get_document(document_id: str, db: Session = Depends(get_db)):
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")
    return _read_document(document)


@app.get("/api/documents/{document_id}/file")
def get_document_file(document_id: str, db: Session = Depends(get_db)):
    document = db.get(Document, document_id)
    if not document or not Path(document.storage_path).is_file():
        raise HTTPException(status_code=404, detail="Document file not found.")
    return FileResponse(
        document.storage_path,
        media_type=document.content_type,
        filename=document.original_filename,
        content_disposition_type="inline",
    )


@app.post("/api/documents/{document_id}/retry", response_model=DocumentRead, status_code=202)
def retry_document(document_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")
    document.processing_status = "uploaded"
    document.error_message = None
    db.commit()
    db.refresh(document)
    background_tasks.add_task(_process_document, document_id)
    return _read_document(document)


def _require_english_ocr(values: dict) -> None:
    # `raw_text` is the source/document transcript and may intentionally keep
    # the original Hindi or Marathi text. Only structured lead fields must be
    # Latin-letter values; `original_ocr_text` preserves the untouched source.
    fields = ("company_name", "owner_name", "designation", "company_address")
    remaining = [key for key in fields if re.search(r"[\u0900-\u097f]", str(values.get(key) or ""))]
    if remaining:
        raise HTTPException(status_code=422, detail="Enter English/Latin-letter values before saving: " + ", ".join(remaining) + ". Re-scan and translate, or correct the preview manually. Original OCR text is kept separately.")


@app.post("/api/leads", response_model=LeadRead, status_code=201)
def create_lead(payload: LeadCreate, db: Session = Depends(get_db)):
    if (payload.raw_text or payload.document_id) and not payload.source_review_confirmed:
        raise HTTPException(status_code=422, detail="Compare the lead details with the available source before saving.")
    document = None
    if payload.document_id:
        document = db.get(Document, payload.document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Source document not found.")
    values = payload.model_dump(exclude={"document_id", "source_document_ids", "source_sides", "document_card_index", "source_review_confirmed"})
    if not values["contact_numbers"] and values["contact_number"]:
        values["contact_numbers"] = [values["contact_number"]]
    if not values["email_addresses"] and values["email"]:
        values["email_addresses"] = [values["email"]]
    values["contact_numbers"] = [_normalize_india_phone(value) for value in values["contact_numbers"]]
    values["contact_number"] = (values["contact_numbers"] or [None])[0]
    values["email"] = (values["email_addresses"] or [None])[0]
    if document and not values["raw_text"]:
        values["raw_text"] = (document.extracted_fields or {}).get("raw_text")
    if document or values.get("original_ocr_text") or payload.source_document_ids:
        _require_english_ocr(values)
    if document and not values.get("original_ocr_text"):
        source = document.extracted_fields or {}
        if payload.document_card_index is not None and source.get("cards"):
            if payload.document_card_index >= len(source["cards"]):
                raise HTTPException(status_code=422, detail="Source card index is invalid.")
            source = source["cards"][payload.document_card_index]
        values["original_ocr_text"] = source.get("original_ocr_text")
    lead = Lead(
        **values,
        document=document,
        source_card_index=payload.document_card_index,
        verification_status="verified" if payload.source_review_confirmed or not (payload.raw_text or payload.document_id) else "needs_review",
    )
    db.add(lead)
    source_ids = list(dict.fromkeys(payload.source_document_ids))
    if payload.document_id and payload.document_id not in source_ids:
        source_ids.insert(0, payload.document_id)
    source_documents = {}
    if source_ids:
        source_documents = {item.id: item for item in db.scalars(select(Document).where(Document.id.in_(source_ids))).all()}
        if len(source_documents) != len(source_ids):
            raise HTTPException(status_code=404, detail="One or more source images could not be found.")
    if document:
        db.flush()
        card_values = (document.extracted_fields or {}).get("cards")
        expected_cards = len(card_values) if isinstance(card_values, list) and card_values else 1
        resolved_indexes = db.scalars(select(Lead.source_card_index).where(
            Lead.document_id == document.id, Lead.source_card_index.is_not(None)
        )).all()
        document.processing_status = (
            "saved" if payload.document_card_index is None or len(set(resolved_indexes)) >= expected_cards else "needs_review"
        )
    if source_ids:
        db.flush()
        for position, source_id in enumerate(source_ids):
            source = source_documents[source_id]
            side = payload.source_sides[position] if position < len(payload.source_sides) and payload.source_sides[position] in {"front", "back"} else ("front" if position == 0 else "back")
            lead.source_images.append(LeadSourceImage(document_id=source_id, side=side, position=position))
            source.processing_status = "saved"
    db.commit()
    db.refresh(lead)
    return lead


@app.get("/api/leads")
def list_leads(
    search: str | None = None,
    company_name: str | None = None,
    owner_name: str | None = None,
    city: str | None = None,
    state: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    statement = select(Lead).order_by(Lead.created_at.desc(), Lead.id.desc())
    statement = _filter_lead_statement(
        statement, search=search, company_name=company_name, owner_name=owner_name, city=city, state=state,
    )
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    leads = db.scalars(
        statement.offset((page - 1) * page_size).limit(page_size)
    ).all()
    # Serialize through LeadRead so computed contact arrays (stored in the
    # lead_contacts table) are included just like they are on the detail API.
    return {
        "leads": [_lead_read(lead) for lead in leads],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get("/api/leads/{lead_id}", response_model=LeadRead)
def get_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")
    return _lead_read(lead)


@app.patch("/api/leads/{lead_id}", response_model=LeadRead)
def update_lead(lead_id: str, payload: LeadUpdate, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")
    updates = payload.model_dump(exclude_unset=True)
    source_review_confirmed = updates.pop("source_review_confirmed", False)
    if (updates.get("raw_text", lead.raw_text) or lead.document_id) and not source_review_confirmed:
        raise HTTPException(status_code=422, detail="Compare the lead details with the available source before saving.")
    if "company_name" in updates and updates["company_name"] is None:
        raise HTTPException(status_code=422, detail="Company name is required.")
    if lead.document_id or updates.get("original_ocr_text", lead.original_ocr_text):
        _require_english_ocr({key: updates.get(key, getattr(lead, key)) for key in ("company_name", "owner_name", "designation", "company_address", "raw_text")})
    contact_fields = {"contact_number", "contact_numbers", "email", "email_addresses"}
    for key, value in updates.items():
        if key not in contact_fields:
            setattr(lead, key, value)
    if "contact_numbers" in updates:
        lead.contact_numbers = [_normalize_india_phone(value) for value in (updates["contact_numbers"] or [])]
    elif "contact_number" in updates:
        lead.contact_numbers = [_normalize_india_phone(updates["contact_number"])] if updates["contact_number"] else []
    if "email_addresses" in updates:
        lead.email_addresses = updates["email_addresses"] or []
    elif "email" in updates:
        lead.email_addresses = [updates["email"]] if updates["email"] else []
    if source_review_confirmed:
        lead.verification_status = "verified"
    db.commit()
    db.refresh(lead)
    return _lead_read(lead)


@app.post("/api/leads/batch", status_code=201)
def create_leads_batch(payload: list[LeadFields], db: Session = Depends(get_db)):
    """Explicit one-click import; leave imported rows flagged for user review."""
    if not payload:
        raise HTTPException(status_code=400, detail="No valid lead rows were provided.")
    created: list[Lead] = []
    try:
        for item in payload:
            values = item.model_dump()
            if not values["contact_numbers"] and values["contact_number"]:
                values["contact_numbers"] = [values["contact_number"]]
            if not values["email_addresses"] and values["email"]:
                values["email_addresses"] = [values["email"]]
            values["contact_numbers"] = [_normalize_india_phone(value) for value in values["contact_numbers"]]
            values["contact_number"] = (values["contact_numbers"] or [None])[0]
            values["email"] = (values["email_addresses"] or [None])[0]
            lead = Lead(**values, verification_status="needs_review")
            db.add(lead)
            created.append(lead)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"created": len(created)}


@app.delete("/api/leads/{lead_id}", status_code=204)
def delete_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found.")
    if lead.document and len(lead.document.leads) <= 1:
        lead.document.processing_status = "needs_review"
    db.delete(lead)
    db.commit()


EXPORT_LEAD_HEADERS = [
    "S.No.", "Autogenerated ID", "Company name", "Owner name", "Designation", "Company address", "Data check",
]
EXPORT_END_HEADERS = ["Website", "Facebook link", "Instagram link", "WhatsApp link", "Other document text"]


@app.get("/api/exports/leads")
def export_leads(
    search: str | None = None,
    ids: list[str] = Query(default=[]),
    company_name: str | None = None,
    owner_name: str | None = None,
    city: str | None = None,
    state: str | None = None,
    export_count: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
):
    statement = select(Lead).order_by(Lead.created_at.desc(), Lead.id.desc())
    if ids:
        statement = statement.where(Lead.id.in_(ids))
    else:
        statement = _filter_lead_statement(
            statement, search=search, company_name=company_name, owner_name=owner_name, city=city, state=state,
        )
    selected_id_query = statement.with_only_columns(Lead.id, maintain_column_froms=True).order_by(
        Lead.created_at.desc(), Lead.id.desc()
    )
    if export_count is not None:
        selected_id_query = selected_id_query.limit(export_count)
    selected_ids = selected_id_query.subquery()
    contact_counts = (
        select(LeadContact.lead_id, LeadContact.contact_type, func.count().label("value_count"))
        .where(LeadContact.lead_id.in_(select(selected_ids.c.id)))
        .group_by(LeadContact.lead_id, LeadContact.contact_type)
        .subquery()
    )
    maximums = db.execute(
        select(contact_counts.c.contact_type, func.max(contact_counts.c.value_count))
        .group_by(contact_counts.c.contact_type)
    ).all()
    maximum_by_type = {contact_type: count for contact_type, count in maximums}
    phone_column_count = max(1, maximum_by_type.get("phone", 0))
    email_column_count = max(1, maximum_by_type.get("email", 0))
    headers = [
        *EXPORT_LEAD_HEADERS,
        *(f"Contact Number {index}" for index in range(1, phone_column_count + 1)),
        *(f"Email ID {index}" for index in range(1, email_column_count + 1)),
        *EXPORT_END_HEADERS,
    ]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Leads"
    sheet.freeze_panes = "A2"
    header_fill = PatternFill(fill_type="solid", fgColor="226B4B")
    header_font = Font(bold=True, color="FFFFFF")
    column_widths = [len(header) for header in headers]
    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=column, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", horizontal="left", wrap_text=True)
    sheet.row_dimensions[1].height = 30
    export_query = statement.with_only_columns(
        Lead.id, Lead.lead_number, Lead.company_name, Lead.owner_name, Lead.designation,
        Lead.company_address, Lead.verification_status, Lead.website, Lead.facebook_link,
        Lead.instagram_link, Lead.whatsapp_link, Lead.raw_text,
        maintain_column_froms=True,
    ).where(Lead.id.in_(select(selected_ids.c.id))).order_by(Lead.created_at.desc(), Lead.id.desc())
    row_count = 0
    page_size = 500
    while True:
        rows = db.execute(export_query.limit(page_size).offset(row_count)).all()
        if not rows:
            break
        lead_ids = [row.id for row in rows]
        contact_rows = db.execute(
            select(LeadContact.lead_id, LeadContact.contact_type, LeadContact.value)
            .where(LeadContact.lead_id.in_(lead_ids))
            .order_by(LeadContact.lead_id, LeadContact.position, LeadContact.id)
        ).all()
        contacts_by_lead: dict[str, tuple[list[str], list[str]]] = {
            lead_id: ([], []) for lead_id in lead_ids
        }
        for lead_id, contact_type, value in contact_rows:
            contacts = contacts_by_lead[lead_id]
            contacts[0 if contact_type == "phone" else 1].append(value)
        for lead_id, (phone_values, email_values) in contacts_by_lead.items():
            contacts_by_lead[lead_id] = ([_normalize_india_phone(value) for value in phone_values], email_values)
        for row_offset, row in enumerate(rows):
            contact_numbers, email_addresses = contacts_by_lead[row.id]
            values = [
                row_count + row_offset + 1, row.lead_number, row.company_name, row.owner_name, row.designation, row.company_address, row.verification_status,
                *contact_numbers, *([None] * (phone_column_count - len(contact_numbers))),
                *email_addresses, *([None] * (email_column_count - len(email_addresses))),
                row.website, row.facebook_link, row.instagram_link, row.whatsapp_link, row.raw_text,
            ]
            safe_values = [values[0], *(str(value) if value is not None else "" for value in values[1:])]
            sheet.append(safe_values)
            for index, raw_value in enumerate(safe_values):
                value = str(raw_value) if raw_value is not None else ""
                cell = sheet.cell(row=sheet.max_row, column=index + 1)
                # Explicit string cells keep phone numbers (including +91) exact
                # while preventing spreadsheet formula execution.
                if value.lstrip().startswith(("=", "+", "-", "@")):
                    cell.data_type = "s"
                if value:
                    longest_line = max((len(line) for line in value.splitlines()), default=0)
                    column_widths[index] = max(column_widths[index], longest_line)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        row_count += len(rows)
    for index, longest in enumerate(column_widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = min(60, max(14, longest + 2))
    for row_index in range(2, sheet.max_row + 1):
        visible_lines = 1
        for column_index, width in enumerate(column_widths, start=1):
            value = sheet.cell(row=row_index, column=column_index).value
            if value:
                usable_width = max(1, min(60, max(14, width + 2)) - 2)
                visible_lines = max(visible_lines, sum(
                    max(1, len(textwrap.wrap(line, width=usable_width, break_long_words=True, break_on_hyphens=True)))
                    for line in str(value).splitlines()
                ))
        sheet.row_dimensions[row_index].height = min(390, max(20, visible_lines * 15))
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{row_count + 1}"
    from io import BytesIO

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="leads.xlsx"'},
    )
