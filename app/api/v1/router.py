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
from app.api.v1.kb_chunk_search import router as kb_chunk_search_router
from app.api.v1.kb_curation import router as kb_curation_router
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
from app.api.v1.sre import router as sre_router
from app.api.v1.slo import router as slo_router, services_slo_router
from app.api.v1.recordings import router as recordings_router
from app.api.v1.rca import router as rca_router
from app.api.v1.rca_admin import router as rca_admin_router
from app.api.v1.rca_dashboards import router as rca_dashboards_router
from app.api.v1.marketplace_shopify import router as marketplace_shopify_router, webhook_router as marketplace_shopify_webhook_router
from app.api.v1.marketplace_amazon import router as marketplace_amazon_router
from app.api.v1.marketplace_walmart import router as marketplace_walmart_router
from app.api.v1.marketplace_ebay import router as marketplace_ebay_router
from app.api.v1.marketplace_etsy import router as marketplace_etsy_router
from app.api.v1.marketplace_mercadolibre import router as marketplace_mercadolibre_router
from app.api.v1.marketplace_allegro import router as marketplace_allegro_router
from app.api.v1.marketplace_cdiscount import router as marketplace_cdiscount_router
from app.api.v1.marketplace_lazada import router as marketplace_lazada_router
from app.api.v1.marketplace_wildberries import router as marketplace_wildberries_router
from app.api.v1.marketplace_bolcom import router as marketplace_bolcom_router
from app.api.v1.marketplace_zalando import router as marketplace_zalando_router
from app.api.v1.marketplace_flipkart import router as marketplace_flipkart_router
from app.api.v1.marketplace_sync import router as marketplace_sync_router
from app.api.v1.marketplace_settings import router as marketplace_settings_router

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
# KB wiki curation (KB_WIKI_CURATION_RAG_PLAN Phase 1) — /api/v1/kb/curation
router.include_router(kb_curation_router)
# KB chunk search (KB_WIKI_CURATION_RAG_PLAN Phase 3) — /api/v1/kb/chunks/search
router.include_router(kb_chunk_search_router)

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

# SRE analytics + post-incident review (Phase 8 / S8.5)
router.include_router(sre_router)             # /api/v1/incidents/.../retrospective, /incidents/reports/*, etc.

# SLI / SLO / error-budget reliability (Phase 9) — /api/v1/slo/*, /api/v1/services/{id}/slo
router.include_router(slo_router)
router.include_router(services_slo_router)

# Support Session Recording + RCA Governance (specs/08, Phase 1+2)
router.include_router(recordings_router)               # /api/v1/support-recordings/*, /tickets/{id}/recordings/*, /dashboards/recordings/*
router.include_router(rca_router, prefix="/rca")        # /api/v1/rca/*
router.include_router(rca_admin_router)                 # /api/v1/tenant/rca-policies/*, /tenant/recording-policies
router.include_router(rca_dashboards_router)            # /api/v1/dashboards/rca/*

# Native marketplace integration (V3-Marketplaces) — /api/v1/marketplaces/{provider}/*
router.include_router(marketplace_shopify_router)
router.include_router(marketplace_shopify_webhook_router)  # /api/v1/webhooks/marketplace/shopify (UNAUTH — HMAC-verified)
router.include_router(marketplace_amazon_router)  # no webhook route — Amazon has none, see connectors/amazon.py
router.include_router(marketplace_walmart_router)  # no /callback (no OAuth redirect) or webhook route — see connectors/walmart.py
router.include_router(marketplace_ebay_router)     # no webhook route — signing scheme unconfirmed, see connectors/ebay.py
router.include_router(marketplace_etsy_router)     # no webhook route — signing scheme unconfirmed, see connectors/etsy.py
# §5 pilot batch complete: Amazon, Shopify, Walmart, eBay, Etsy — all 5 have
# connect/status/disconnect; only Shopify has a wired inbound webhook route
# today (the others' signing schemes need confirming, or don't exist at all
# for Amazon/Walmart's client-credentials model). fetch_orders()/
# fetch_returns() (manual/backfill path) work for all 5 pending sandbox
# validation — none of this has been tested against a live account.

router.include_router(marketplace_mercadolibre_router)  # messaging-only (no returns sync) — see connectors/mercadolibre.py; UNVERIFIED, no live credentials for this batch
router.include_router(marketplace_allegro_router)        # messaging-only — see connectors/allegro.py; UNVERIFIED, no live credentials for this batch
router.include_router(marketplace_cdiscount_router)      # messaging-only — see connectors/cdiscount.py; UNVERIFIED, no live credentials for this batch
router.include_router(marketplace_lazada_router)          # messaging-only — see connectors/lazada.py; UNVERIFIED, no live credentials for this batch
router.include_router(marketplace_wildberries_router)     # messaging-only — see connectors/wildberries.py; UNVERIFIED, no live credentials for this batch
router.include_router(marketplace_bolcom_router)          # thin, email-fallback-only — see connectors/bolcom.py; UNVERIFIED, no live credentials for this batch
router.include_router(marketplace_zalando_router)         # thin, email-fallback-only — see connectors/zalando.py; UNVERIFIED, no live credentials for this batch
router.include_router(marketplace_flipkart_router)        # thin, email-fallback-only — see connectors/flipkart.py; UNVERIFIED, no live credentials for this batch

# Static-path settings route registered before the parametric {provider}/sync
# route, same defensive ordering convention as tickets.py — no actual
# collision today (no bare /marketplaces/{provider} route exists), kept
# consistent anyway.
router.include_router(marketplace_settings_router)  # /api/v1/marketplaces/settings (GET/PUT) — tenant-level config, §0a
router.include_router(marketplace_sync_router)       # /api/v1/marketplaces/{provider}/sync — manual path, mirrors the Celery task's mapping calls
