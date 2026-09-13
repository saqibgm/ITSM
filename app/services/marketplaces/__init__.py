"""
Native marketplace-integration module (V3-Marketplaces).

Separate module/folder per docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §0a —
keeps this large, multi-year surface area cleanly separable from core ITSM code.

- ``connectors/`` — one file per marketplace, all implementing the shared
  ``CommerceConnector`` interface (connectors/base.py). §5's pilot batch:
  Amazon, Shopify, Walmart, eBay, Etsy.
- ``ingestion.py`` — the connector-agnostic mapping layer: turns a
  NormalizedOrder/NormalizedReturn/NormalizedMessage into a MarketplaceOrder
  upsert, a Ticket + MarketplaceOrderTicketLink, or a TicketComment.
- ``registry.py`` — resolves a MarketplaceConnection.provider to its
  CommerceConnector implementation (added once the first connector lands).
"""
