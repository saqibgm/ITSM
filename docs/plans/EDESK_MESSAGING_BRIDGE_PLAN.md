# eDesk Messaging Bridge — Plan

Status: **planning only — no implementation, no migration, no code changes yet**
Scope: use eDesk (a commercial marketplace helpdesk vendor) to solve **only the
messaging gap** the native connector build (V3-Marketplaces) hit hard, structural
walls on — not to replace the native order/return sync that already works.

Relates to `MARKETPLACE_ITSM_INTEGRATION_PLAN.md` §1, which flagged this exact
category of vendor (naming ChannelReply — eDesk acquired ChannelReply in 2021,
same underlying product family) as the answer to Amazon's documented
"Messaging API is send-only, no read path" restriction. That plan was written
before the native build happened; this one assumes the native build (Shopify,
Amazon, Walmart, eBay, Etsy connectors — all shipped, live-tested) stays as-is
for orders/returns, and evaluates eDesk as a narrowly-scoped addition.

---

## 1. Why this, why now

Messaging was built against each marketplace's own native API this session, and
hit real, confirmed, structural walls — not engineering gaps:

| Marketplace | What we found |
|---|---|
| Amazon | SP-API's Messaging API is send-only by platform policy — no endpoint exists to read what a buyer sent, confirmed via docs. On top of that, our app's `getMessagingActionsForOrder` call returns a live 403 (Messaging role not granted in Seller Central). |
| eBay | The only real messaging resource (Post-Order API's Inquiry/INR resource) — both `search` and `send_message` — either 404s or is documented as unsupported in Sandbox entirely. Nothing to build or verify against in this environment. |
| Shopify, Etsy, Walmart | No buyer-messaging API exists at all (confirmed via direct doc research) — not a gap, a platform limitation. |

Every one of these is either a permanent platform limitation or an
environment/access wall unrelated to code quality. eDesk (and vendors like it)
exist specifically because they've already gone through each marketplace's
commercial-partner approval process — access we don't have and can't get by
writing better code.

## 2. Confirmed API facts (2026-09-14 research pass — not assumed)

- Base URL: `https://api.edesk.com/v1/`, Bearer token auth (token generated in
  the eDesk dashboard).
- **API access requires the eDesk Enterprise plan** — custom pricing, needs a
  sales conversation. Standard tiers (Essential $39, Growth $89, Professional
  $119 per agent/month, annual billing) do NOT include API access.
- **No webhooks** — confirmed no dedicated webhook documentation exists across
  their full endpoint index. Integration must be poll-based.
- `GET /tickets` — supports `filter_last_updated_at_gte`/`filter_created_at_gte`
  for incremental polling, `filter_channel_id_equals` to scope to one
  marketplace connection, `filter_sales_order_id_equals`/
  `filter_seller_order_id_equals` to correlate to a specific order. Response
  includes nested sales-order detail but only a `messages_ids` array per
  ticket, not full message bodies.
- `GET /messages/{id}` — needed per message to get the actual body (N+1 pattern
  — a ticket with 5 messages needs 1 + 5 calls to fully sync it).
- `POST /messages` (`type: "Message"`) — outbound send, genuinely relays to the
  real marketplace via eDesk's own configured channel connection when
  `send=true` and the channel has a default-recipient configured. This is the
  actual value: eDesk already has the Amazon Messaging role / eBay production
  access we don't.
- Rate limits: **not found** in the public docs — needs direct confirmation
  from eDesk (sales/support conversation) before designing poll frequency,
  since polling too aggressively without a known limit risks throttling.

## 3. Scope decision: messaging-only bridge, not a replacement

Two architectures were considered:

- **(A) eDesk as messaging-only bridge** — keep the native connectors
  (Shopify/Amazon/Walmart/eBay/Etsy) exactly as they are for orders and
  returns (already built, already live-tested, already working for
  Shopify/Amazon). Add eDesk ONLY for the messaging capability that's
  genuinely blocked everywhere else.
- **(B) eDesk as the full source of truth** — retire the native connectors
  entirely, route orders/returns/messages all through eDesk.

**(A) is the right call.** The native build already works for the two
capabilities (orders, returns) that had no vendor-access blocker — throwing
that away to route everything through a new paid subscription would be pure
regression for zero benefit. eDesk only earns its cost for the one capability
it uniquely unlocks: real marketplace-message send/receive.

## 4. Proposed shape (assuming (A))

1. **New connection type**: `EdeskConnection` (or extend `MarketplaceConnection`
   with `provider="edesk"`) — one API token, likely tenant-level like every
   other connection in this build, unless the business runs one shared eDesk
   account across all tenants (needs a business decision, not an engineering
   one — see §6).
2. **Order/ticket correlation**: eDesk tickets carry `sales_order_id` and
   `filter_seller_order_id_equals` — match against `MarketplaceOrder.
   external_order_id` (already the join key everywhere else in this build) to
   attach eDesk messages to the right order.
3. **Inbound poll (Celery beat task)**: on an interval (frequency TBD pending
   the rate-limit answer in §2), `GET /tickets?filter_last_updated_at_gte=
   <last poll>`, then `GET /messages/{id}` per new/updated message, map each
   into the existing `marketplace_messages` table (migration 0041, already
   shipped) with `direction='inbound'`. Same table the native connectors'
   outbound sends already write to — one Messaging page, one
   `MarketplaceMessage` model, regardless of source.
4. **Outbound send**: `SendBuyerMessageModal`'s existing "Message buyer" flow
   — instead of (or in addition to, per-provider) calling the native
   connector's `send_message()`, call eDesk's `POST /messages` for orders on
   channels eDesk actually covers. Existing `POST /marketplaces/orders/
   {order_id}/send-message` route gets a second connector-like path, not a
   rewrite — same response contract either way.
5. **Admin UI**: one more `ProviderCard`-style connection entry in
   `MarketplacesSection.jsx`, API-token form (not OAuth — simpler than every
   connector built so far).

## 5. What this does NOT change

- `marketplace_orders`, `marketplace_order_ticket_links` — untouched, native
  connectors keep owning orders/returns.
- The Messaging page, `SendBuyerMessageModal`, `marketplace_messages` — all
  already built this session, reused as-is. eDesk becomes a second *source*
  of rows in that table, not a new page/table.
- Amazon/eBay's native `send_message()` stubs stay in the codebase (correct,
  ready for if/when Amazon grants the Messaging role or eBay moves to
  production) — eDesk is additive, not a replacement for that code.

## 6. Open questions before committing engineering time

Same discipline as every connector in this build: confirm live, don't assume.

1. **Business decision, not engineering**: does this org want to pay for an
   eDesk Enterprise subscription? API access is gated behind it — nothing
   here is buildable without that commitment first. Worth a sales call to get
   real Enterprise pricing before deciding.
2. **Rate limits** — not published; need a direct answer from eDesk before
   designing poll frequency (too aggressive = throttled, confirmed risk given
   the N+1 message-body pattern in §2).
3. **Trial/sandbox account** — same "live-verify everything" discipline this
   entire build followed (Shopify, Amazon, eBay all had real, live-tested
   surprises vs. their docs). Implementation should wait for a real eDesk
   trial/sandbox account to test polling + send against, not be written blind
   against docs alone.
4. **Channel connection scope** — does eDesk need to be connected to the SAME
   Amazon/Shopify/eBay seller accounts our native connectors are already
   connected to (dual-connecting one seller account to two different apps —
   confirm marketplaces allow this), or does eDesk become the sole
   messaging-capable connection per marketplace once adopted?

## 7. Estimated build size (once §6 is resolved)

Comparable to one more native connector (Etsy/Walmart were each roughly a
session each) — new connection type + poll task + inbound mapping + outbound
send wiring + admin UI. Smaller than a full connector since there's no
OAuth flow (API token only) and no orders/returns logic to write (messaging
only). Real estimate needs the trial account from §6.3 to firm up once
correlation/rate-limit behavior is confirmed live rather than assumed from
docs.
