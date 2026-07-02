from fastapi import APIRouter

from app.api.v1.admin import router as admin_router
from app.api.v1.automation import router as automation_router
from app.api.v1.ai_tickets import ai_router as ai_router, router as ai_tickets_router
from app.api.v1.assets import (
    asset_categories_router,
    asset_types_router,
    router as assets_router,
    vendors_router,
)
from app.api.v1.gdpr import router as gdpr_router
from app.api.v1.health import router as health_router
from app.api.v1.kb import router as kb_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.platform import router as platform_router
from app.api.v1.reports import router as reports_router
from app.api.v1.tickets import router as tickets_router
from app.api.v1.virtual_agent import router as virtual_agent_router
from app.api.v1.webhooks import router as webhooks_router
from app.api.v1.webhooks_outbound import router as webhooks_outbound_router
from app.api.v1.integrations import router as integrations_router
from app.api.v1.sla import router as sla_router
from app.api.v1.sla_tickets import router as sla_tickets_router
from app.api.v1.oncall import router as oncall_router, services_router
from app.api.v1.alerting import (
    router as alerting_router, alerts_router, routing_router,
)
from app.api.v1.incidents import router as incidents_router
from app.api.v1.ops import (
    ops_router, public_router as status_public_router, maint_router, workflows_router,
)

router = APIRouter()

router.include_router(health_router)
router.include_router(webhooks_router, tags=["webhooks"])
router.include_router(tickets_router)
router.include_router(notifications_router)
router.include_router(admin_router)

# AI enrichment endpoints:
#   /api/v1/tickets/{id}/ai-classification         (GET, POST accept, POST reject)
#   /api/v1/tickets/{id}/ai-duplicates             (GET, POST dismiss)
# ai_tickets_router already carries prefix="/tickets"; mounting without an
# additional prefix keeps all routes at /api/v1/tickets/... as designed.
router.include_router(ai_tickets_router)

# /api/v1/ai/classification-dataset  (admin HITL export)
# ai_router carries prefix="/ai" — mounted at /api/v1/ai/...
router.include_router(ai_router)

# Asset Management (S2.1)
router.include_router(assets_router)            # /api/v1/assets
router.include_router(asset_types_router)       # /api/v1/asset-types
router.include_router(asset_categories_router)  # /api/v1/asset-categories
router.include_router(vendors_router)           # /api/v1/vendors

# Knowledge Base (S3.1) — /api/v1/kb
router.include_router(kb_router)

# Platform API (S4.1) — /api/v1/platform/...
router.include_router(platform_router, prefix="/platform")

# Virtual Agent RAG engine (S4B.1) — /api/v1/virtual-agent/...
router.include_router(virtual_agent_router)

# GDPR Data Export & Right-to-Erasure (S6D) — /api/v1/gdpr/...
router.include_router(gdpr_router)

# Reporting & Analytics (S6C) — /api/v1/reports/...
router.include_router(reports_router)

# Automation Rules Engine (S6A) — /api/v1/automation/...
router.include_router(automation_router)

# Outbound Webhooks (S6B) — /api/v1/webhooks-config/...
router.include_router(webhooks_outbound_router)

# Integration marketplace catalog (4b) — /api/v1/integrations/catalog
router.include_router(integrations_router)

# SLM — SLA/OLA/UC agreements, targets, rules, coverage windows (Phase 7 / S7.1)
# /api/v1/sla/...
router.include_router(sla_router)

# Per-ticket SLA runtime (Phase 7 / S7.2) — /api/v1/tickets/{id}/sla[...]
router.include_router(sla_tickets_router)

# On-call & services (Phase 8 / S8.1) — /api/v1/services, /api/v1/on-call/*
router.include_router(services_router)
router.include_router(oncall_router)

# Alerting, escalation & paging (Phase 8 / S8.2)
router.include_router(alerting_router)   # /api/v1/on-call/escalation-policies, contact-methods, heartbeats
router.include_router(alerts_router)     # /api/v1/alerts
router.include_router(routing_router)    # /api/v1/routing/rules

# Incidents (Phase 8 / S8.3) — /api/v1/incidents/*
router.include_router(incidents_router)

# Status page, maintenance windows & workflows (Phase 8 / S8.4)
router.include_router(ops_router)             # /api/v1/status-page
router.include_router(status_public_router)   # /api/v1/status/{slug}  (UNAUTH)
router.include_router(maint_router)           # /api/v1/maintenance-windows
router.include_router(workflows_router)       # /api/v1/workflows
