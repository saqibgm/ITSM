"""Resolves a MarketplaceConnection.provider string to its CommerceConnector
implementation — the piece flagged as a TODO in this package's __init__.py
since the first connector landed. Used by the Celery task and the manual
sync endpoint, both of which only have a provider string (from the DB row),
not a direct import of a specific connector module.
"""

from app.services.marketplaces.connectors.allegro import allegro_connector
from app.services.marketplaces.connectors.amazon import amazon_connector
from app.services.marketplaces.connectors.base import CommerceConnector
from app.services.marketplaces.connectors.bolcom import bolcom_connector
from app.services.marketplaces.connectors.cdiscount import cdiscount_connector
from app.services.marketplaces.connectors.ebay import ebay_connector
from app.services.marketplaces.connectors.etsy import etsy_connector
from app.services.marketplaces.connectors.flipkart import flipkart_connector
from app.services.marketplaces.connectors.lazada import lazada_connector
from app.services.marketplaces.connectors.mercadolibre import mercadolibre_connector
from app.services.marketplaces.connectors.shopify import shopify_connector
from app.services.marketplaces.connectors.walmart import walmart_connector
from app.services.marketplaces.connectors.wildberries import wildberries_connector
from app.services.marketplaces.connectors.zalando import zalando_connector

CONNECTORS: dict[str, CommerceConnector] = {
    "shopify": shopify_connector,
    "amazon": amazon_connector,
    "walmart": walmart_connector,
    "ebay": ebay_connector,
    "etsy": etsy_connector,
    # Messaging-only marketplaces (2026-09-15) — see each connector
    # module's docstring for scope/verification caveats.
    "mercadolibre": mercadolibre_connector,
    "allegro": allegro_connector,
    "cdiscount": cdiscount_connector,
    "lazada": lazada_connector,
    "wildberries": wildberries_connector,
    # Thin, email-fallback-only marketplaces (2026-09-15) — no native
    # messaging API exists for any of these (confirmed), so
    # messaging_capability == NONE and the connector only exists to
    # capture buyer_email for itsm-service's email fallback.
    "bolcom": bolcom_connector,
    "zalando": zalando_connector,
    "flipkart": flipkart_connector,
}


def get_connector(provider: str) -> CommerceConnector:
    connector = CONNECTORS.get(provider)
    if connector is None:
        raise ValueError(f"no connector registered for provider '{provider}'")
    return connector


__all__ = ["CONNECTORS", "get_connector"]
