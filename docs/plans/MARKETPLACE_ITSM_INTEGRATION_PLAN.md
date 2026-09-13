# Marketplace ↔ ITSM Integration — Architecture & Solution Plan

Status: **planning only — no implementation, no migration, no code changes yet**
Scope: connect **19 confirmed target marketplaces** (Group A + B, §1a — 2026-09-09) to
**itsm-service** (this repo), with both automatic (event-driven) and manual
(agent-triggered) create/update of ITSM records. AliExpress/Alibaba.com (Group C) and
Taobao/Tmall/Meta Commerce/Target Plus/Wayfair/Noon (Group D) are explicitly out of
scope for this build — see §1a for why.
Not to be confused with the existing Amazon/Shopify OAuth integrations in the *chatbot*
repo (`Project-IQ-V2/action_server/actions/actions_amazon.py`,
`docs/plans/AMAZON_INTEGRATION_PLAN.md`) — those exist for customer-support order
lookup/refund/cancel via Rasa. This is a separate concern: ops/inventory/listing events
becoming ITSM tickets and CMDB-style records, for internal ops teams, not shoppers.

---

## 0. The short version

This repo already has ~80% of the infrastructure this needs, built for other purposes:

| What's needed | What already exists | Reuse as-is? |
|---|---|---|
| Inbound signed-webhook receiver, idempotent, Celery-retriable | `app/api/v1/webhooks.py` (`iam_webhook`) — HMAC verify → parse → route by `event` → 200/401/500 | **Yes, clone the pattern exactly** |
| Background async processing | Celery (`app/workers/celery_app.py`, `tasks_webhooks.py`) | **Yes** |
| Outbound event notification to Teams/Zapier/generic webhook | `WebhookEndpoint`/`WebhookDelivery` models + `integrations.py` catalog (`EVENT_TYPES` incl. `ticket.created`, `asset.created`) | **Yes — nothing to build**, marketplace-triggered tickets/assets fire these automatically once created, for free |
| Rule-based auto vs. manual behavior once a ticket exists | `AutomationRule`/`AutomationLog` (`automation.py`) — triggers on `ticket_created`, `asset_status_changed`, etc. | **Yes downstream**, but its trigger vocabulary doesn't include "marketplace event received" — the auto/manual decision for *whether to create the ticket at all* has to happen in the new ingestion layer, not this engine (see §5) |
| A place to represent a marketplace listing/product | `Asset` (`asset.py`) | **No** — this is a physical/CMDB schema (`serial_number`, `warranty_expiry_date`, `model_number`) and forcing marketplace listings into it would be a schema misuse. New entity needed (§4.3) |
| What's missing entirely | Per-marketplace/per-middleware credential storage, the inbound marketplace event router, the event→ticket/listing mapping logic, the admin UI | **Net-new build**, scoped below |

---

## 1. Scoped v1 requirement: exactly three capabilities

The full event taxonomy in §3 (below) is the eventual ceiling. **v1 scope is fixed to
three things, confirmed 2026-09-09:**

1. **Sync orders**
2. **Sync return/replacement**
3. **Sync messaging against order/return/replacement**

Researched each against real vendor/platform docs — #1 and #2 are solid. **#3 has a
hard platform-level wall on Amazon specifically, not an engineering gap:**

| Capability | Amazon | eBay | Shopify | Walmart | Others (via ChannelEngine) |
|---|---|---|---|---|---|
| **Orders** | ✅ via ChannelEngine (`POST/GET /v2/orders`) | ✅ | ✅ | ✅ | ✅ — confirmed core ChannelEngine feature |
| **Returns/Replacement** | ✅ via ChannelEngine (`GET /v2/returns/channel`) | ✅ eBay Post-Order API has dedicated case/return search + `CaseDetails` | ✅ | ✅ | ✅ — confirmed core ChannelEngine feature |
| **Messaging** | ⚠️ **SP-API's Messaging API is send-only.** Amazon's own docs/dev-forum confirm: *"developers currently cannot retrieve buyer messages and respond to them — the Messaging API only provides the feature to let you send a message with the specified template."* There is **no API to read what a buyer sent** — a seller has to check Seller Central by hand. This is Amazon platform policy, not a vendor or engineering limitation, and no amount of build effort routes around it. | eBay Post-Order API handles order **inquiries** as part of the case object — more complete than Amazon, but needs a sandbox validation pass before assuming full two-way thread retrieval | No native order-tied buyer-messaging concept — Shopify's "messaging" is a separate customer-service surface (Shopify Inbox), not an order/case field | Not confirmed in this research pass — needs direct doc check before committing | **Not a ChannelEngine feature at all** — it's an orders/inventory/listing OMS, messaging is outside its scope entirely |

**What this means for the architecture:** no single vendor covers all three under
evaluation. Orders + Returns route through ChannelEngine (§1a). Messaging needs a
**second, purpose-built source** — dedicated marketplace-helpdesk bridges exist
(e.g. ChannelReply) that aggregate buyer messages *and* returns across
Amazon/eBay/Walmart/Etsy/Shopify into a helpdesk. Two things still need confirming
directly with whichever vendor is chosen before committing engineering time:

- Their prebuilt connectors generally target **named commercial helpdesks** (Zendesk,
  Freshdesk, Gorgias, Help Scout, Re:amaze, Zoho Desk, Kustomer) — **not itsm-service**,
  a custom system. Needs a direct vendor conversation to confirm whether they expose a
  generic webhook/API a custom system can consume the same way, or whether itsm-service
  would need to be formally onboarded as a supported target.
- Ask directly **how** a candidate vendor sources Amazon buyer messages, given SP-API's
  documented read restriction (§1's table) — legitimate mechanisms exist (e.g. Amazon's
  own buyer-message email-forwarding feature, separate from the API), but this should
  be confirmed per vendor rather than assumed.

**Working v1 stance on messaging, pending that vendor conversation:** scope Amazon as
**outbound-only** (an agent can compose and send from ITSM via SP-API's Messaging API;
inbound buyer messages still require a human checking Seller Central unless a chosen
vendor confirms a legitimate read path) while eBay and any marketplace a chosen vendor
actually confirms gets full two-way sync. Don't block on Amazon inbound messaging —
ship it as a known, documented gap unless/until a vendor conversation resolves it.

---

## 1a. Marketplace coverage decision (from prior research)

19 marketplaces were researched. Feasibility splits into four groups that drive the
ingestion-layer decision in §2:

| Group | Marketplaces | Native API friction |
|---|---|---|
| **A — Easy native** | Amazon, Shopify, Walmart, eBay, Etsy, Allegro, Bol.com, Otto, Coupang, Mercado Libre, Wildberries | Standard OAuth2 REST, low friction |
| **B — Medium friction** | Temu, Cdiscount, Lazada, Shopee, Flipkart, Trendyol, TikTok Shop, Zalando | Approval-gated but workable |
| **C — High friction** | AliExpress, Alibaba.com | App approval + geographic restriction + no OpenAPI spec |
| **D — Blocked/gated by non-engineering factors** | Taobao/Tmall (needs a Mainland China business entity — legal, not technical), Meta Commerce order API (invite-only beta), Target Plus/Wayfair/Noon (no public self-serve API found) | Park until a business/legal decision is made independently of this build |

**Confirmed target scope (2026-09-09): Group A + Group B = 19 marketplaces.**
Group C (AliExpress, Alibaba.com) and Group D are explicitly out of scope for this
build — Group C's approval-gate friction and Group D's legal/access blockers (§1a's
own table) aren't worth carrying into vendor outreach or engineering scope until/unless
a separate decision reopens them.

- **Group A (11):** Amazon, Shopify, Walmart, eBay, Etsy, Allegro, Bol.com, Otto, Coupang, Mercado Libre, Wildberries
- **Group B (8):** Temu, Cdiscount, Lazada, Shopee, Flipkart, Trendyol, TikTok Shop, Zalando

At 19 marketplaces, hand-building native connectors inside itsm-service is still not
the right call — same conclusion reached in the chatbot research, and stronger here
since itsm-service starts from zero native marketplace code (unlike the chatbot repo,
which already has Amazon/Shopify OAuth built for a different purpose). Recommendation
unchanged: **route all 19 through a middleware aggregator (ChannelEngine, pending
Phase 0a's per-marketplace written confirmation — see `PHASE0_VENDOR_OUTREACH.md`) as
the single upstream source for orders/returns**, one inbound webhook shape consumed
regardless of how many of the 19 sit behind it. Native itsm-service connectors stay an
option later for any single marketplace where a tenant's volume justifies the
dedicated engineering (Amazon is the most likely candidate, given the chatbot repo's
OAuth code could plausibly be extracted into a shared library rather than rebuilt).

---

## 2. Target architecture (two-source, per §1)

```
                    ┌─ Orders + Returns/Replacement ─────────────────────────┐
                    │  Marketplace(s) ──▶ ChannelEngine ──▶ ONE webhook shape │
                    └──────────────────────────┬───────────────────────────┘
                                                │
                    ┌─ Messaging ───────────────┼──────────────────────────┐
                    │  Marketplace(s) ──▶ messaging vendor (tbc, §1) ──▶ a  │
                    │  SECOND webhook shape, once vendor confirms a custom  │
                    │  helpdesk target is possible — else per-marketplace   │
                    │  native (eBay Post-Order API inquiries; Amazon        │
                    │  outbound-send-only via SP-API Messaging API directly)│
                    └──────────────────────────┬──────────────────────────┘
                                                ▼
                          POST /api/v1/webhooks/marketplace/{provider}/{kind}
                          kind = orders | returns | messages — same HMAC-verify →
                          idempotency-check-via-MarketplaceEvent → enqueue-Celery
                          pattern regardless of source (mirrors iam_webhook), just a
                          different secret/payload-shape per (provider, kind) pair
                                                │
                                                ▼
                          tasks_marketplace_sync.process_marketplace_event
                          (Celery, mirrors tasks_webhooks.py)
                                                │
                    ┌───────────────────────────┼────────────────────────────┐
                    ▼                            ▼                            ▼
         _map_order()                 _map_return_to_ticket()      _map_message_to_comment()
         creates/updates a            creates/updates a Ticket     appends a TicketComment to
         MarketplaceOrder row          (type=service_request,      the Ticket already linked to
         (new entity, §4)              linked via                  that order/return via
                                        marketplace_order_ticket_   marketplace_order_ticket_links
                                        links — reuses full ticket  — inbound message = comment
                                        status/SLA/assignment       from "customer" actor type;
                                        machinery for the return    outbound agent reply in that
                                        workflow state machine)     same ticket triggers a send
                                                                     back to the marketplace (§6a)
                    │                            │                            │
                    ▼                            ▼                            ▼
       Existing ticket_created/updated triggers fire automatically for the return-ticket
       and any comment-triggered notification: SLA policy assignment, AutomationRule
       engine, outbound WebhookEndpoint delivery — NO NEW CODE needed for any of this,
       it's already wired (§0). Order sync itself doesn't create a ticket by default
       (§3's conservative-default table) — it's a silent MarketplaceOrder upsert unless
       a tenant's config says otherwise.

Manual path (agent-triggered, same mapping code, different entry point):
  POST /api/v1/integrations/marketplace/{connection_id}/sync?kind=orders|returns|messages
      → pulls current state from the relevant vendor's REST API (not a webhook)
      → runs the SAME _map_order()/_map_return_to_ticket()/_map_message_to_comment()
        functions → "auto" and "manual" are two invocations of one mapping layer each,
        not separate implementations to maintain
```

### 6a. Outbound messaging (agent replies from ITSM back to the marketplace)

This is new — not covered by the existing outbound `WebhookEndpoint` framework, which
notifies *other systems* (Teams/Zapier), not a marketplace's own buyer-facing channel.
An agent typing a reply into a return-ticket's comment thread needs that reply
**sent back to the buyer through the marketplace**, not just logged. Concretely:
a `TicketComment` with `actor_type='agent'` on a ticket that has a
`marketplace_order_ticket_links` row triggers a Celery task calling either (a) the
chosen messaging vendor's send-message endpoint, once confirmed as the messaging
source (§1), or (b) SP-API's Messaging API directly for Amazon (send-only, but that's
exactly what's needed for this direction — see §1's outbound-only stance).

---

## 3. Which marketplace events become what (scoped to the three v1 capabilities, §1)

Not every event within the three should create a ticket — a naive "webhook → ticket"
mapping on every order event would spam the queue with routine noise. Recommend a
**per-tenant, per-event-type mapping config**, defaulting conservatively:

| Marketplace event class | Default action | Rationale |
|---|---|---|
| Order created / acknowledged / shipped / delivered | Silent — upsert `marketplace_orders` row only, no ticket | Routine, high-volume, not an ops concern |
| Return requested | **Auto-create `service_request` ticket**, linked via `marketplace_order_ticket_links` (link_type='return') | Needs human ops attention, has a real workflow (approve → receive → refund) that maps onto `TicketStatus` |
| Replacement requested | **Auto-create `service_request` ticket**, link_type='replacement' | Same as above, distinct workflow |
| Return/replacement status changed upstream (e.g. carrier picked up, refund issued by the marketplace) | Update the linked ticket's status/comment — no new ticket | Keeps the existing ticket as the single source of truth rather than creating duplicates |
| Buyer message received (order or return/replacement context) | **Append `TicketComment`** to the linked ticket if one exists; if the message arrives before any return/replacement ticket does (a pre-return inquiry), auto-create a `service_request` ticket to hold the thread | A message with nowhere to land is the one failure mode worth designing out explicitly |
| Agent reply sent from ITSM | Push to the marketplace via §6a | The manual/outbound half of requirement #3 |

This mapping config is intentionally **not** built on top of `AutomationRule` (§0) —
that engine's trigger vocabulary is ticket/asset-lifecycle-based and evaluates
conditions on already-created ITSM entities. The marketplace layer's decision ("should
this external event become a ticket at all") happens one step upstream of that, in the
new ingestion code. Once a ticket exists, though, `AutomationRule` runs against it
exactly like any other ticket — free reuse, not a gap.

---

## 4. Data model (new tables)

### 4.1 `marketplace_connections`
Mirrors the chatbot repo's planned `commerce_connections` shape (§4.1 of
`AMAZON_INTEGRATION_PLAN.md`), itsm-scoped:

```
tenant_id            UUID FK tenants
provider             VARCHAR   -- 'channelengine' | 'amazon' | 'shopify' | ... (native, if ever added)
external_id          VARCHAR   -- ChannelEngine account id, or seller/shop id for native
credentials          JSONB     -- encrypted (Fernet, same pattern as existing secret storage)
status                VARCHAR  -- connected | disconnected | error
sync_config           JSONB    -- per-event-type mapping (§3), overrides the tenant default
last_synced_at        TIMESTAMPTZ
created_at/updated_at TIMESTAMPTZ
```

### 4.2 `marketplace_events`
Mirrors `WebhookDelivery`'s shape but for **inbound** events — gives idempotency,
audit trail, and replay:

```
id                    UUID (uuid7)
tenant_id             UUID
connection_id         UUID FK marketplace_connections
provider              VARCHAR
external_event_id     VARCHAR   -- idempotency key
event_type            VARCHAR
payload               JSONB
status                VARCHAR   -- received | processed | ticket_created | order_updated | comment_added | failed | ignored
resulting_ticket_id   UUID NULL FK tickets
resulting_order_id    UUID NULL FK marketplace_orders
error_message         TEXT NULL
received_at/processed_at  TIMESTAMPTZ

UNIQUE (provider, external_event_id)   -- dedup replayed webhooks, exactly like
                                            the IAM webhook's tenant-uniqueness pattern
```

### 4.3 `marketplace_orders` (new entity — v1 priority, replaces the earlier
`marketplace_listings` idea, which is deferred out of v1 scope entirely; product/
listing sync was never in the confirmed three-capability list §1)

```
id                     UUID (uuid7)
tenant_id              UUID
connection_id          UUID FK marketplace_connections
provider               VARCHAR
external_order_id      VARCHAR   -- unique per (tenant, provider)
status                 VARCHAR   -- new | acknowledged | shipped | delivered | cancelled
buyer_email            VARCHAR NULL   -- PII — encrypt at rest, same pattern flagged
                                        in SHOPIFY_INTEGRATION_PLAN.md §4.6
order_lines            JSONB
total_amount           NUMERIC(12,2)
currency               VARCHAR(3)
placed_at              TIMESTAMPTZ
raw_metadata           JSONB     -- full provider payload, for fields not worth
                                    modeling individually
created_at/updated_at  TIMESTAMPTZ

UNIQUE (tenant_id, provider, external_order_id)
```

### 4.4 `marketplace_order_ticket_links` (junction, mirrors `AssetTicketLink`)

Links a `marketplace_orders` row to the `Ticket` created for its return/replacement
case (§3, §5). One order can have zero tickets (routine order, never returned) or more
than one (multiple partial return events on the same order):

```
order_id     UUID FK marketplace_orders
ticket_id    UUID FK tickets
link_type    VARCHAR   -- 'return' | 'replacement'
linked_at    TIMESTAMPTZ
```

### 4.5 Messaging — no new entity, reuses `TicketComment`

`app/models/ticket.py`'s existing `TicketComment` (line 525) already models a threaded
comment on a ticket. Inbound buyer messages become `TicketComment` rows with a new
`source` discriminator (`'marketplace_buyer'` vs. the existing agent/system actor
types — confirm `TicketComment`'s current actor-type column supports extension before
assuming this is additive-only) tagged with `marketplace_message_external_id` for
idempotency. No new messaging-specific table — deliberately, to keep the return ticket
as the single place an agent reads/replies, rather than splitting the conversation
across two UIs.

### 4.6 Ticket creation
No schema change needed beyond §4.4's junction table — marketplace-triggered tickets
are ordinary `Ticket` rows (`type=service_request` for return/replacement, per §3),
with `custom_fields` (already JSONB on the ticket model, mirroring `Asset.custom_fields`)
carrying `{"marketplace_provider": "amazon", "marketplace_event_id": "...",
"marketplace_order_id": "..."}` for traceability back to source.

---

## 5. Inbound receiver (new: `app/api/v1/webhooks.py` gets a sibling route, or a new
`webhooks_marketplace.py` module — cleaner given the IAM receiver's HMAC scheme is
provider-specific and shouldn't be diluted with marketplace-specific parsing)

```python
POST /api/v1/webhooks/marketplace/{provider}/{kind}
# kind = 'orders' | 'returns' | 'messages' — {provider} is 'channelengine' for
# orders/returns and, pending §1's vendor conversation, whichever messaging vendor
# is chosen (or a per-native-marketplace value, e.g. 'ebay', 'amazon') for messages

1. Read raw body (needed if the source signs the payload; not all do — see below)
2. Verify the source's own auth scheme — **confirmed via public docs (Phase 0a,
   `PHASE0_VENDOR_OUTREACH.md`) that ChannelEngine does NOT use HMAC signing for
   orders/returns**, unlike `iam_webhook`'s model this plan originally assumed by
   analogy. It's a shared auth key/value pair (header or query string), compared with
   `secrets.compare_digest` for timing safety same as today, just no digest to
   recompute — simpler than `_verify_hmac()`, not a straight clone of it. The
   messaging vendor's scheme (Phase 0b) may differ again and needs its own check once
   confirmed — don't assume every source signs the same way
3. Idempotency check: SELECT ... WHERE provider=? AND external_event_id=? — if it
   exists, return 200 immediately (replayed delivery), don't reprocess
4. INSERT marketplace_events row, status='received'
5. Enqueue Celery task (tasks_marketplace_sync.process_marketplace_event), return 200
   fast — mirrors the "don't do slow work inline" principle already used for webhook
   delivery (WebhookDelivery rows are written 'pending' then processed async)
6. On any unexpected error before step 5: return 500 so the source retries, exactly
   like iam_webhook's except-block behavior
```

## 6. Manual sync endpoint

```python
POST /api/v1/integrations/marketplace/{connection_id}/sync?kind=orders|returns|messages
  - role-gated (admin, tenant_admin, agent — same _READ_ROLES-style gate as integrations.py)
  - Pulls current state from the relevant vendor's REST API for that connection and kind
  - Feeds each item through the SAME mapping functions the webhook path uses
  - Useful for: initial backfill when a connection is first set up, and an explicit
    "sync now" button for an agent who doesn't want to wait for the next webhook
```

---

## 7. Admin UI

Extend the existing **Integrations** page (already driven by `integrations.py`'s
`CATALOG` + `EVENT_TYPES` pattern — the frontend already renders installable
integrations data-driven, not hardcoded) with a second catalog *kind*:
`inbound_marketplace_sync`, alongside the existing `outbound_webhook` kind. Same
`ConfigModal`/connect-flow UX users already see for Teams/generic webhook, plus:

- Connect flow: enter ChannelEngine account credentials (or per-marketplace OAuth,
  if a native connector is ever added)
- Per-event-type mapping table (§3) — toggle auto-create ticket / silent-update-only /
  ignore, per marketplace event type, with tenant-level defaults and per-connection
  overrides
- "Sync now" button → calls §6
- Recent `marketplace_events` log view (mirrors the existing `WebhookDelivery` log UI
  pattern, if one exists — otherwise a simple table)

---

## 8. What needs zero new code (already covered)

- **Outbound notifications** when a marketplace-triggered ticket changes — the
  existing `WebhookEndpoint`/`ticket.created`/`ticket.updated` machinery fires exactly
  as it does for any other ticket. No changes to `webhooks_outbound.py`/`tasks_webhooks.py`.
- **SLA policy assignment, automation rules, ticket transitions** on the
  marketplace-created ticket — all existing `ticket_service.py`/`automation.py`/
  `sla_service.py` logic runs unmodified, since the new ticket is just a `Ticket` row.
- **RBAC** — reuse `app/auth/dependencies.py`'s existing `require_role` pattern for
  every new endpoint, same as `integrations.py`.

---

## 9. Phased roadmap

| Phase | Scope | Depends on |
|---|---|---|
| **0a — ChannelEngine coverage confirmation** | **Public-doc research done** (`PHASE0_VENDOR_OUTREACH.md`): correct API is Merchant API (not Channel API), auth is a shared key/value pair not HMAC, webhook registration mechanism confirmed. **Still open**: exact payload schemas, per-marketplace written confirmation, pricing, per-tenant key scoping — outreach email drafted and ready to send, needs an owner (§10 decision 5) | Nothing further from research — needs a human to send the request |
| **0b — Messaging vendor selection + conversation** | **Public-doc research done**: ChannelReply's coverage confirmed narrow (10 channels, no custom-webhook-target story) — a real negative signal, not disqualifying. Candidate shortlist (ChannelReply, eDesk, Replydesk-unverified) and a ready-to-send questionnaire are in `PHASE0_VENDOR_OUTREACH.md`. **Still open**: everything questionnaire-gated — needs an owner | Nothing further from research — needs a human to send the questionnaire |
| **1 — Data model + inbound receiver skeleton** | `marketplace_connections`, `marketplace_events`, `marketplace_orders`, `marketplace_order_ticket_links` migrations; `POST /api/v1/webhooks/marketplace/{provider}/{kind}` with signature verify + idempotency + Celery enqueue, no mapping logic yet (just logs the event) | Phase 0a |
| **2 — Order sync (capability #1)** | `tasks_marketplace_sync.py`'s `_map_order()`, silent `marketplace_orders` upsert, manual sync for orders | Phase 1 |
| **3 — Return/replacement sync (capability #2)** | `_map_return_to_ticket()`, ticket creation + `marketplace_order_ticket_links`, return/replacement status-update handling | Phase 1, can start parallel with 2 |
| **4 — Messaging sync, inbound + outbound (capability #3)** | `_map_message_to_comment()`, §6a's outbound-reply push, scoped per Phase 0b's findings — likely ships with eBay/other confirmed marketplaces first and Amazon flagged as outbound-only per §1 unless 0b resolves it | Phase 0b + Phase 3 (needs a return/replacement ticket to attach messages to) |
| **5 — Manual sync + Admin UI** | §6 + §7, covering all three kinds | Phases 2-4 |
| **6 — First marketplace live, end-to-end** | Amazon for orders+returns (outbound-only messaging per §1 unless Phase 0b changes that) as the pilot, given existing organizational familiarity — verify against real webhook delivery (same "can't test webhooks against localhost" caveat the Shopify build hit — needs a public HTTPS endpoint or tunnel, see `SHOPIFY_INTEGRATION_PLAN.md` §6c finding 4) | Phases 1-5 |
| **7 — Roll out remaining marketplaces** | Incremental, config-only once ChannelEngine/the messaging vendor have them configured — no new itsm-service code per marketplace, since they're all routed through the same webhook shape | Phase 6 proven |

---

## 10. Decisions needed from you

1. **Confirm ChannelEngine (or name a preferred alternative) for orders/returns** —
   this plan assumes it based on the confirmed-coverage research; a different vendor
   choice changes §0a/§5's webhook-shape specifics but not the overall architecture.
2. **Select and confirm the messaging vendor** — pending Phase 0b's conversation, or
   accept native-per-marketplace (eBay via Post-Order API, Amazon outbound-only via
   SP-API Messaging API directly) as the fallback if no vendor supports a custom
   helpdesk target.
3. **Accept Amazon messaging as outbound-only for v1** (§1) — i.e., an agent can send
   from ITSM, but inbound buyer messages still require someone checking Seller Central
   by hand, unless Phase 0b's vendor conversation finds a legitimate read path. This
   needs an explicit yes, since it's a real gap in requirement #3 for the marketplace
   this org knows best.
4. **Which return/replacement/message event types default to auto-create-ticket vs.
   silent-update vs. manual-review-queue** (§3's table is a starting proposal, not a
   final answer) — business call about ops team workload, not engineering.
5. **Pilot marketplace for Phase 6** — Amazon is the natural default, but confirm
   before committing the first end-to-end build slice to it, especially given #3's gap.
6. **Budget/appetite for ChannelEngine's GMV-based pricing** *and* the messaging
   vendor's separate pricing — worth getting concrete quotes for both against the
   tenant's actual sales volume before Phase 1 starts, since running two vendors'
   costs simultaneously changes the ROI case relative to native connectors for at
   least the highest-volume marketplace.

No migrations, models, or endpoints have been created — this is the plan only, per the
project's standing convention (see `SLA_ONCALL_INITIATIVE` precedent: plan-only until
explicitly approved).
