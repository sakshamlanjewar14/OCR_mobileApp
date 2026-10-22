from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    storage_path: Mapped[str] = mapped_column(String(500), unique=True)
    processing_status: Mapped[str] = mapped_column(String(30), default="uploaded")
    extracted_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    leads: Mapped[list["Lead"]] = relationship(back_populates="document")
    source_links: Mapped[list["LeadSourceImage"]] = relationship(back_populates="document")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    lead_number: Mapped[str] = mapped_column(
        String(40), unique=True, index=True, default=lambda: f"LD-{uuid4().hex.upper()}"
    )
    company_name: Mapped[str] = mapped_column(String(255), index=True)
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    designation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    company_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    facebook_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    instagram_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    whatsapp_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(20), default="verified", nullable=False)
    source_card_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    document: Mapped[Document | None] = relationship(back_populates="leads")
    contacts: Mapped[list["LeadContact"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="LeadContact.position"
    )
    source_images: Mapped[list["LeadSourceImage"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="LeadSourceImage.position"
    )

    @property
    def source_document_ids(self) -> list[str]:
        ids = [source.document_id for source in self.source_images]
        if self.document_id and self.document_id not in ids:
            ids.insert(0, self.document_id)
        return list(dict.fromkeys(ids))

    @property
    def contact_number(self) -> str | None:
        return self.contact_numbers[0] if self.contact_numbers else None

    @contact_number.setter
    def contact_number(self, value: str | None) -> None:
        self.contact_numbers = [value] if value else []

    @property
    def email(self) -> str | None:
        return self.email_addresses[0] if self.email_addresses else None

    @email.setter
    def email(self, value: str | None) -> None:
        self.email_addresses = [value] if value else []

    @property
    def contact_numbers(self) -> list[str]:
        return [contact.value for contact in self.contacts if contact.contact_type == "phone"]

    @contact_numbers.setter
    def contact_numbers(self, values: list[str] | None) -> None:
        self._replace_contact_values("phone", values)

    @property
    def email_addresses(self) -> list[str]:
        return [contact.value for contact in self.contacts if contact.contact_type == "email"]

    @email_addresses.setter
    def email_addresses(self, values: list[str] | None) -> None:
        self._replace_contact_values("email", values)

    def _replace_contact_values(self, contact_type: str, values: list[str] | None) -> None:
        current_contacts = [contact for contact in self.contacts if contact.contact_type == contact_type]
        remaining = [contact for contact in self.contacts if contact.contact_type != contact_type]
        by_value = {contact.value: contact for contact in current_contacts}
        values_in_order = list(dict.fromkeys(value.strip() for value in (values or []) if value and value.strip()))
        replacement = []
        for index, value in enumerate(values_in_order):
            contact = by_value.pop(value, None)
            if contact is None:
                contact = LeadContact(contact_type=contact_type, value=value, position=index)
            contact.position = index
            replacement.append(contact)
        self.contacts = remaining + replacement


class LeadContact(Base):
    __tablename__ = "lead_contacts"
    __table_args__ = (UniqueConstraint("lead_id", "contact_type", "value", name="uq_lead_contact_value"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    contact_type: Mapped[str] = mapped_column(String(10))
    value: Mapped[str] = mapped_column(String(320))
    position: Mapped[int] = mapped_column(Integer, default=0)
    lead: Mapped[Lead] = relationship(back_populates="contacts")


class LeadSourceImage(Base):
    __tablename__ = "lead_source_images"
    __table_args__ = (UniqueConstraint("lead_id", "document_id", name="uq_lead_source_image"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), index=True)
    side: Mapped[str] = mapped_column(String(10), default="front")
    position: Mapped[int] = mapped_column(Integer, default=0)
    lead: Mapped[Lead] = relationship(back_populates="source_images")
    document: Mapped[Document] = relationship(back_populates="source_links")
