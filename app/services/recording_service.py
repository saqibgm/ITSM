"""Support Session Recording service (specs/08, Phase 1).

Recordings default to external_reference storage (metadata + link only) per
the PRD's own decision (§16) — manual_upload is the only source_type that
touches app.services.storage_service, exactly like TicketAttachment does.

AI summarization always runs off the request path (Celery-invoked only) and
never raises into its caller — a budget/timeout failure degrades to
ai_summary_status="failed", never blocks the ticket page.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ValidationError
from app.models.recording import (
    RecordingAccessLog,
    SupportRecording,
    TenantRecordingPolicy,
    TicketRecordingLink,
)
from app.models.notification import NotificationType
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)

_SESSION_SUMMARY_PROMPT = (
    "You are summarizing an internal customer-support screen-sharing session "
    "transcript for an ITSM ticket. Produce a structured, blameless summary. "
    "Respond as strict JSON with keys: customer_issue, environment_context, "
    "actions_taken, next_steps, open_questions, action_items (list of short strings). "
    "Never fabricate details not present in the transcript."
)

_SENSITIVE_NO_AI = {"contains_pii", "contains_credentials", "legal_hold"}


def _classify_source_type(url: str) -> str:
    host = url.lower()
    if "teams.microsoft.com" in host or "teams.live.com" in host:
        return "teams"
    if "sharepoint.com" in host:
        return "sharepoint"
    if "onedrive.live.com" in host or "1drv.ms" in host:
        return "onedrive"
    raise ValidationError(
        "Recording URL must be a Teams, SharePoint, or OneDrive link. "
        "Use manual upload for other sources."
    )


async def _get_policy(db: AsyncSession, tenant_id: UUID) -> TenantRecordingPolicy:
    policy = (await db.execute(
        select(TenantRecordingPolicy).where(TenantRecordingPolicy.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if policy is None:
        # No row yet — return in-memory defaults matching the model's server_defaults
        # rather than round-tripping a write on every read.
        policy = TenantRecordingPolicy(tenant_id=tenant_id)
    return policy


async def log_access(
    db: AsyncSession,
    tenant_id: UUID,
    recording: SupportRecording,
    actor_id: Optional[UUID],
    actor_email: str,
    action: str,
    source_ip: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    """Insert-only access log row. Called from every read/open/download/summary/
    permission-change endpoint — never optional (NFR §10.3)."""
    db.add(RecordingAccessLog(
        tenant_id=tenant_id, recording_id=recording.id, actor_id=actor_id,
        actor_email=actor_email, action=action, source_ip=source_ip, request_id=request_id,
    ))


async def link_from_url(
    db: AsyncSession,
    tenant_id: UUID,
    actor_id: Optional[UUID],
    actor_email: str,
    ticket_id: UUID,
    url: str,
    *,
    title: str,
    link_type: str = "support_call",
    evidence_weight: str = "none",
    notes: Optional[str] = None,
    consent_status: str = "not_required",
) -> tuple[SupportRecording, TicketRecordingLink]:
    """Classify + create a SupportRecording from a pasted URL and link it to a
    ticket in one step (the "one click"/"paste a link" UX from specs/08 §3.4.2)."""
    source_type = _classify_source_type(url)

    policy = await _get_policy(db, tenant_id)
    if policy.block_link_if_missing_consent and consent_status == "missing":
        raise ValidationError(
            "This tenant requires recording consent before linking — "
            "set consent_status to a non-missing value or capture consent first."
        )

    recording = SupportRecording(
        tenant_id=tenant_id, source_type=source_type, recording_url=url,
        title=title, consent_status=consent_status, status="linked",
        storage_mode="external_reference", created_by=actor_id,
    )
    db.add(recording)
    await db.flush()

    link = TicketRecordingLink(
        ticket_id=ticket_id, recording_id=recording.id, link_type=link_type,
        linked_by=actor_id, evidence_weight=evidence_weight, notes=notes,
    )
    db.add(link)

    await log_access(db, tenant_id, recording, actor_id, actor_email, "viewed_metadata")

    from app.models.ticket import Ticket
    ticket = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
    if ticket is not None and ticket.assignee_id is not None:
        await NotificationService().send(
            db, tenant_id=tenant_id, user_id=ticket.assignee_id,
            type=NotificationType.recording_linked,
            title=f"Recording linked to ticket {getattr(ticket, 'ticket_number', ticket_id)}",
            body=title, entity_type="support_recording", entity_id=recording.id,
        )

    return recording, link


async def generate_recording_summary(db: AsyncSession, tenant_id: UUID, recording: SupportRecording, redis) -> None:
    """Celery-only — never called inline from a request handler (NFR §10.1).
    Always degrades gracefully: any AI/budget failure sets ai_summary_status
    to "failed" rather than propagating."""
    policy = await _get_policy(db, tenant_id)

    if recording.sensitivity in _SENSITIVE_NO_AI or not policy.allow_ai_summary:
        recording.ai_summary_status = "skipped_no_consent"
        return

    if not recording.transcript_url:
        recording.ai_summary_status = "skipped_no_consent"
        return

    recording.ai_summary_status = "pending"
    try:
        from app.services.ai.ai_service import ai_service
        from app.services.pii_masker import pii_masker
        import json

        # Phase 1/2: transcript_url is metadata pasted alongside the recording,
        # not fetched live (that's Phase 3's Graph integration) — the caller
        # is expected to have supplied transcript text out of band if any.
        raw_context = recording.description or recording.title
        masked = pii_masker.mask(raw_context)
        raw = await ai_service.generate(
            tenant_id=str(tenant_id), redis=redis,
            messages=[{"role": "user", "content": masked}],
            system=_SESSION_SUMMARY_PROMPT, feature="recording_summary",
        )
        parsed = json.loads(raw)
        recording.ai_summary = parsed.get("customer_issue") or raw
        recording.ai_action_items = parsed.get("action_items") or []
        recording.ai_summary_status = "completed"
    except Exception as exc:
        logger.warning(
            "recording_summary_generation_failed",
            extra={"tenant_id": str(tenant_id), "recording_id": str(recording.id), "error": str(exc)},
        )
        recording.ai_summary_status = "failed"


async def missing_required_recordings(db: AsyncSession, tenant_id: UUID) -> dict:
    """RCA cases whose recording_linked_if_exists evidence item is still
    'missing' — i.e. required but not provided."""
    from app.models.retro import RcaEvidenceChecklist, IncidentRetrospective
    total = (await db.execute(
        select(func.count()).select_from(RcaEvidenceChecklist)
        .join(IncidentRetrospective, IncidentRetrospective.id == RcaEvidenceChecklist.retro_id)
        .where(
            IncidentRetrospective.tenant_id == tenant_id,
            RcaEvidenceChecklist.evidence_type == "recording_linked_if_exists",
            RcaEvidenceChecklist.status == "missing",
            RcaEvidenceChecklist.required.is_(True),
        )
    )).scalar_one()
    return {"missing_required_recordings": int(total)}


async def inaccessible_recordings(db: AsyncSession, tenant_id: UUID) -> dict:
    total = (await db.execute(
        select(func.count()).select_from(SupportRecording)
        .where(SupportRecording.tenant_id == tenant_id, SupportRecording.status == "inaccessible")
    )).scalar_one()
    return {"inaccessible_recordings": int(total)}


async def consent_issues(db: AsyncSession, tenant_id: UUID) -> dict:
    rows = (await db.execute(
        select(SupportRecording.consent_status, func.count())
        .where(SupportRecording.tenant_id == tenant_id, SupportRecording.consent_status.in_(["missing", "disputed"]))
        .group_by(SupportRecording.consent_status)
    )).all()
    return {"consent_issues": {status: int(n) for status, n in rows}}


async def summary(db: AsyncSession, tenant_id: UUID, since: Optional[datetime] = None) -> dict:
    since = since or (datetime.now(timezone.utc) - timedelta(days=30))
    total = (await db.execute(
        select(func.count()).select_from(SupportRecording)
        .where(SupportRecording.tenant_id == tenant_id, SupportRecording.created_at >= since)
    )).scalar_one()
    linked_to_rca = (await db.execute(
        select(func.count()).select_from(TicketRecordingLink)
        .join(SupportRecording, SupportRecording.id == TicketRecordingLink.recording_id)
        .where(SupportRecording.tenant_id == tenant_id, TicketRecordingLink.evidence_weight == "required_for_rca")
    )).scalar_one()
    return {"since": since.isoformat(), "recordings": int(total), "linked_to_rca": int(linked_to_rca)}
