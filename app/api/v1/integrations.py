"""Integration marketplace catalog (4b).

A read-only manifest of the *installable* integrations the platform backs via
the outbound-webhook system. The admin portal renders the marketplace from this
(so available integrations + their config fields are data-driven, not hardcoded
in the frontend) and creates the corresponding webhook endpoint via
/webhooks-config/endpoints with the catalog entry's `format` + `integration_key`.

Managed services (IAM/storage/email/LLM) and KB data-source connectors are
composed by the portal itself and intentionally not listed here.
"""

from fastapi import APIRouter, Depends

from app.auth.dependencies import CurrentUser, require_role

router = APIRouter(prefix="/integrations", tags=["integrations"])

_READ_ROLES = ("admin", "tenant_admin", "agent", "team_lead", "manager")

# Domain events the platform currently fires on the webhook bus.
EVENT_TYPES = [
    "ticket.created",
    "ticket.updated",
    "asset.created",
    "asset.updated",
    "kb_article.published",
]

# Installable integrations. Each maps onto a webhook endpoint (`format` +
# `integration_key`); `config_schema` drives the configure form in the UI.
CATALOG = [
    {
        "key": "microsoft_teams",
        "name": "Microsoft Teams",
        "category": "Notifications",
        "kind": "outbound_webhook",
        "format": "teams",
        "icon": "microsoft-teams",
        "description": "Post ticket, asset and knowledge-base events to a Teams "
                       "channel via an incoming-webhook URL.",
        "config_schema": [
            {
                "name": "url",
                "label": "Teams incoming webhook URL",
                "type": "url",
                "required": True,
                "placeholder": "https://<org>.webhook.office.com/webhookb2/...",
            },
        ],
        "default_events": ["ticket.created", "ticket.updated"],
    },
    {
        "key": "generic_webhook",
        "name": "Webhook",
        "category": "Notifications",
        "kind": "outbound_webhook",
        "format": "generic",
        "icon": "webhook",
        "description": "Send HMAC-signed JSON for each event to any HTTPS "
                       "endpoint (also works as a Zapier 'Catch Hook').",
        "config_schema": [
            {"name": "url", "label": "Endpoint URL", "type": "url", "required": True},
            {
                "name": "secret",
                "label": "Signing secret",
                "type": "password",
                "required": True,
                "min_length": 16,
            },
        ],
        "default_events": [],
    },
]


@router.get("/catalog")
async def get_catalog(
    current_user: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> dict:
    return {"integrations": CATALOG, "event_types": EVENT_TYPES}
