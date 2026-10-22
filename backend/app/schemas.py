from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LeadFields(BaseModel):
    company_name: str = Field(min_length=1, max_length=255)
    owner_name: str | None = Field(default=None, max_length=255)
    designation: str | None = Field(default=None, max_length=120)
    company_address: str | None = None
    contact_number: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=320)
    contact_numbers: list[str] = Field(default_factory=list)
    email_addresses: list[str] = Field(default_factory=list)
    website: str | None = Field(default=None, max_length=500)
    facebook_link: str | None = Field(default=None, max_length=500)
    instagram_link: str | None = Field(default=None, max_length=500)
    whatsapp_link: str | None = Field(default=None, max_length=500)
    raw_text: str | None = None
    original_ocr_text: str | None = None


class LeadCreate(LeadFields):
    document_id: str | None = None
    source_document_ids: list[str] = Field(default_factory=list)
    source_sides: list[str] = Field(default_factory=list)
    document_card_index: int | None = Field(default=None, ge=0)
    source_review_confirmed: bool = False


class LeadUpdate(BaseModel):
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    owner_name: str | None = Field(default=None, max_length=255)
    designation: str | None = Field(default=None, max_length=120)
    company_address: str | None = None
    contact_number: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=320)
    contact_numbers: list[str] | None = None
    email_addresses: list[str] | None = None
    website: str | None = Field(default=None, max_length=500)
    facebook_link: str | None = Field(default=None, max_length=500)
    instagram_link: str | None = Field(default=None, max_length=500)
    whatsapp_link: str | None = Field(default=None, max_length=500)
    raw_text: str | None = None
    original_ocr_text: str | None = None
    source_review_confirmed: bool = False


class LeadRead(LeadFields):
    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_number: str
    verification_status: str = "verified"
    source_card_index: int | None = None
    source_document_ids: list[str] = Field(default_factory=list)
    document_id: str | None
    created_at: datetime
    updated_at: datetime


class DocumentRead(BaseModel):
    id: str
    original_filename: str
    content_type: str
    processing_status: str
    extracted_fields: dict[str, object]
    error_message: str | None
    created_at: datetime
    lead_id: str | None = None
    lead_count: int = 0
    resolved_card_indexes: list[int] = Field(default_factory=list)


class DocumentStatusRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


class DocumentStatusRead(BaseModel):
    id: str
    processing_status: str
    error_message: str | None
    lead_count: int


class QRParseRequest(BaseModel):
    payload: str = Field(max_length=10000)


class ReverseGeocodeRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
