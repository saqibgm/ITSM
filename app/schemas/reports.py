"""
Pydantic v2 response schemas for the Reporting & Analytics API (S6C).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Ticket Summary
# ---------------------------------------------------------------------------


class TicketSummaryResponse(BaseModel):
    total: int
    start_date: str
    end_date: str
    by_status: dict[str, int] = Field(default_factory=dict)
    by_priority: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# SLA Compliance
# ---------------------------------------------------------------------------


class SLAByPriorityItem(BaseModel):
    total: int
    within_sla: int
    breached: int


class SLAComplianceResponse(BaseModel):
    total_tickets: int
    within_sla: int
    breached: int
    compliance_pct: float
    avg_resolution_hours: float
    by_priority: dict[str, SLAByPriorityItem] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Volume Trend
# ---------------------------------------------------------------------------


class VolumeTrendPoint(BaseModel):
    date: str | None
    count: int


class VolumeTrendResponse(BaseModel):
    granularity: str
    start_date: str
    end_date: str
    points: list[VolumeTrendPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Asset Summary
# ---------------------------------------------------------------------------


class AssetSummaryResponse(BaseModel):
    total: int
    by_status: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# AI Usage Summary
# ---------------------------------------------------------------------------


class AIUsageByModel(BaseModel):
    input_tokens: int
    output_tokens: int
    call_count: int


class AIUsageByFeature(BaseModel):
    total_input_tokens: int
    total_output_tokens: int
    total_calls: int
    by_model: dict[str, AIUsageByModel] = Field(default_factory=dict)


class AIUsageSummaryResponse(BaseModel):
    total_tokens: int
    total_calls: int
    by_feature: dict[str, AIUsageByFeature] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# KB Top Articles
# ---------------------------------------------------------------------------


class KBTopArticleItem(BaseModel):
    id: str
    title: str
    view_count: int
    helpful_count: int
    not_helpful_count: int
    helpful_pct: float


class KBTopArticlesResponse(BaseModel):
    total: int
    articles: list[KBTopArticleItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Team Workload
# ---------------------------------------------------------------------------


class TeamWorkloadItem(BaseModel):
    team_id: str
    team_name: str
    open_tickets: int
    avg_age_hours: float


class TeamWorkloadResponse(BaseModel):
    start_date: str
    end_date: str
    teams: list[TeamWorkloadItem] = Field(default_factory=list)
