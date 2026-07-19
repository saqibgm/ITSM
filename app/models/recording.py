"""Support Session Recording module (specs/08, Phase 1).

`SupportRecording` stores metadata + an external link by default (Teams/
SharePoint/OneDrive) — the binary video is only copied into ITSM storage for
the `manual_upload` source type, reusing `app.services.storage_service`
exactly like `TicketAttachment` does. `RecordingAccessLog` is insert-only,
written on every metadata view/open/download/summary/permission-change.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin


class SupportRecording(Base):
    __tablename__ = "support_recordings"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False)
    source_recording_id: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    source_meeting_id: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    source_drive_id: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    source_item_id: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    recording_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    transcript_url: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    title: Mapped[str] = mapped_column(sa.VARCHAR(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(sa.Integer, nullable=True)
    organizer_user_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    organizer_email: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(320), nullable=True)
    customer_contact_name: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    customer_contact_email: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(320), nullable=True)
    participants: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    consent_status: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'not_required'"))
    sensitivity: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'normal'"))
    access_policy: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'ticket_team_only'"))
    retention_expires_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    storage_mode: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'external_reference'"))
    storage_key: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(512), nullable=True)
    status: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'linked'"))
    ai_summary_status: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'not_requested'"))
    ai_summary: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    ai_action_items: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())


class TicketRecordingLink(Base):
    """No own tenant_id — FK-isolated via ticket_id, same posture as
    ticket_tag_assignments/ticket_watchers."""

    __tablename__ = "ticket_recording_links"

    ticket_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True)
    recording_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("support_recordings.id", ondelete="CASCADE"), primary_key=True)
    link_type: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'support_call'"))
    linked_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
    is_primary: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    visible_to_customer: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    notes: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    evidence_weight: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'none'"))


class RecordingAccessLog(Base):
    """Insert-only. Every recording open/download/view is logged here — never
    UPDATE/DELETE (NFR §10.3/§10.4, specs/08)."""

    __tablename__ = "recording_access_log"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    recording_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("support_recordings.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_email: Mapped[str] = mapped_column(sa.VARCHAR(320), nullable=False)
    action: Mapped[str] = mapped_column(sa.VARCHAR(30), nullable=False)
    source_ip: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(45), nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())


class TenantRecordingPolicy(Base):
    """Singleton per tenant (PK = tenant_id) — no RLS needed, every query is a
    direct PK lookup."""

    __tablename__ = "tenant_recording_policies"

    tenant_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    recording_consent_required: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    consent_source: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'not_required'"))
    block_link_if_missing_consent: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    allow_customer_visibility: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    redact_transcript_before_ai: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    allow_ai_summary: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    recording_retention_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("365"))
    allow_manual_upload: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    updated_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
