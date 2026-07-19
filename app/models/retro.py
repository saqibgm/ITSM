"""Post-incident review / RCA governance — retrospectives, action items, AI PIR
drafts (Phase 8 / S8.5), evolved in-place into governed RCA objects (specs/08,
Phase 1+2).

`IncidentRetrospective` is the single row type behind both flows:
- Legacy: `is_rca_governed=False`, free-form draft/in_review/published status,
  used today only by the "Generate PIR draft" button. Untouched by the new
  columns below — they're all nullable/defaulted so old rows keep working.
- Governed "RCA": `is_rca_governed=True`, status driven by the state machine
  in `app.services.rca_service`, with policy/evidence/action-item/history
  tracking layered on via the new tables in this file (all FK to
  `incident_retrospectives.id` as `retro_id` — there is no separate
  `rca_records` table by design).
"""

import enum
from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from uuid_extensions import uuid7

from app.models.base import Base, TimestampMixin


class RetroStatus(str, enum.Enum):
    """Legacy (non-governed) lifecycle — kept for tickets/incidents that never
    opt into RCA governance."""

    draft = "draft"
    in_review = "in_review"
    published = "published"


class RcaStatus(str, enum.Enum):
    """Governed RCA lifecycle (is_rca_governed=True). See app.services.rca_service
    for the transition graph and enforcement rules."""

    not_required = "not_required"
    required = "required"
    draft = "draft"
    under_review = "under_review"
    approved = "approved"
    rejected = "rejected"
    actions_in_progress = "actions_in_progress"
    completed = "completed"
    waived = "waived"
    overdue = "overdue"


class ActionItemStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    done = "done"
    blocked = "blocked"
    cancelled = "cancelled"
    accepted_risk = "accepted_risk"
    overdue = "overdue"


class PIRDraftStatus(str, enum.Enum):
    pending_review = "pending_review"
    accepted = "accepted"
    rejected = "rejected"


class IncidentRetrospective(Base, TimestampMixin):
    """One retrospective/RCA per incident (or, for governed RCAs, per
    ticket/problem/change via source_ticket_id when incident_id is null)."""

    __tablename__ = "incident_retrospectives"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    incident_id: Mapped[Optional[UUID]] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=True, unique=True,
    )
    summary: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    contributing_factors: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    impact: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'draft'"))
    published_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)

    # --- governance (specs/08) ---
    is_rca_governed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    source_type: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'incident'"))
    source_ticket_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True)
    rca_number: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(20), nullable=True)
    severity: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(10), nullable=True)
    owner_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approver_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    team_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    service_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("oncall_services.id", ondelete="SET NULL"), nullable=True)
    product_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    customer_org_id: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    trigger_policy_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("rca_policies.id", ondelete="SET NULL"), nullable=True)
    previous_status: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(20), nullable=True)
    due_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    waived_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    waived_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    waiver_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    executive_summary: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    customer_impact: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    business_impact: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    technical_summary: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    root_cause_statement: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    root_cause_category: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(30), nullable=True)
    contributing_factor_tags: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    detection_gap: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    response_gap: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    prevention_plan: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    lessons_learned: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    customer_facing_summary: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    ai_draft_status: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'not_requested'"))
    ai_confidence: Mapped[Optional[float]] = mapped_column(sa.Float, nullable=True)
    ai_model_version: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(40), nullable=True)
    created_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class RetroActionItem(Base):
    __tablename__ = "retro_action_items"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    retro_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=False, index=True)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    owner_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ticket_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(sa.VARCHAR(15), nullable=False, server_default=sa.text("'open'"))

    # --- governance (specs/08) ---
    title: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    action_type: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(30), nullable=True)
    priority: Mapped[str] = mapped_column(sa.VARCHAR(10), nullable=False, server_default=sa.text("'medium'"))
    team_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    due_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    verification_method: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    verified_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    linked_change_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="SET NULL"), nullable=True)
    accepted_risk_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    accepted_risk_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())


class AIPIRDraft(Base):
    """AI/heuristic-generated post-incident review or RCA draft (HITL: accept/reject)."""

    __tablename__ = "ai_pir_drafts"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(
        sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    incident_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incidents.id", ondelete="CASCADE"), nullable=True)
    retro_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=True, index=True)
    summary: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    contributing_factors: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    impact: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    action_items: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(sa.VARCHAR(20), nullable=False, server_default=sa.text("'pending_review'"))
    model_version: Mapped[str] = mapped_column(sa.VARCHAR(40), nullable=False, server_default=sa.text("'template-v1'"))
    created_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())


class RcaPolicy(Base):
    """Tenant-defined rule that auto-requires an RCA under matching conditions.

    `conditions` is a list of `{field, op, value}` dicts evaluated by
    `app.services.rca_policy_engine` (a safe dispatch table — never `eval()`).
    `outputs` carries `{owner_role, approver_role, due_days, required_evidence_types, ...}`.
    """

    __tablename__ = "rca_policies"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(sa.VARCHAR(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(sa.VARCHAR(15), nullable=False, server_default=sa.text("'draft'"))
    conditions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'"))
    outputs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("100"))
    created_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
    updated_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())


class RcaLinkedEntity(Base):
    """Generic evidence/source link from an RCA to any other entity.

    `entity_type` spans an open-ended set (ticket/incident/problem/change/
    asset/service/alert/recording/kb_article/comment/attachment/log_link/
    dashboard_link) so this uses the entity_type+entity_id convention already
    established by Notification/AutomationLog rather than a fixed FK.
    """

    __tablename__ = "rca_linked_entities"
    __table_args__ = (
        sa.CheckConstraint("entity_id IS NOT NULL OR entity_ref IS NOT NULL", name="ck_rca_linked_entities_ref"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    retro_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(sa.VARCHAR(30), nullable=False)
    entity_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), nullable=True)
    entity_ref: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(255), nullable=True)
    link_role: Mapped[str] = mapped_column(sa.VARCHAR(30), nullable=False, server_default=sa.text("'related'"))
    required: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    linked_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())


# Evidence types seeded onto every governed RCA at creation time (specs/08 §4.4.3).
RCA_EVIDENCE_TYPES = [
    "primary_ticket_linked",
    "customer_impact_documented",
    "timeline_completed",
    "recording_linked_if_exists",
    "recording_summary_reviewed",
    "logs_alerts_linked",
    "service_asset_impact_linked",
    "sla_breach_evidence_linked",
    "customer_communication_captured",
    "corrective_actions_created",
    "approval_comments_present",
]


class RcaEvidenceChecklist(Base):
    __tablename__ = "rca_evidence_checklist"
    __table_args__ = (
        sa.UniqueConstraint("retro_id", "evidence_type", name="uq_rca_evidence_checklist_retro_type"),
    )

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    retro_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=False, index=True)
    evidence_type: Mapped[str] = mapped_column(sa.VARCHAR(40), nullable=False)
    required: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    status: Mapped[str] = mapped_column(sa.VARCHAR(15), nullable=False, server_default=sa.text("'missing'"))
    provided_entity_type: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(30), nullable=True)
    provided_entity_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), nullable=True)
    waived_by: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    waiver_reason: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())


class RcaHistory(Base):
    """Insert-only audit trail of every RCA field/status change. No UPDATE/DELETE
    anywhere in the service layer — enforced by convention, matching how
    incident_timeline/sla_events are already treated."""

    __tablename__ = "rca_history"

    id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid7)
    retro_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("incident_retrospectives.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id: Mapped[UUID] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    actor_id: Mapped[Optional[UUID]] = mapped_column(sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(sa.VARCHAR(30), nullable=False)
    field_changed: Mapped[Optional[str]] = mapped_column(sa.VARCHAR(60), nullable=True)
    old_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
