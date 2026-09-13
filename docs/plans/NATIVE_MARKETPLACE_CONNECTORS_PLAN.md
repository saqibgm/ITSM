# Native Marketplace Connectors — Build Plan (pay-engineers path)

Status: **planning only — no implementation, no migration, no code changes yet**
Decision confirmed 2026-09-09: build native connectors for all 19 target marketplaces
instead of routing through a paid middleware (ChannelEngine) or messaging vendor
(ChannelReply/eDesk). `PHASE0_VENDOR_OUTREACH.md` is now **on hold, not cancelled** —
if native-build effort or messaging-API feasibility comes back worse than expected,
that outreach package is ready to resume without re-research. See that doc's status
line for the pointer.

---

## 0. The real scale, upfront

This org has two directly-measured data points for what a single marketplace
integration costs, from this exact codebase's own history:

| Marketplace | Scope built | Traditional effort | AI-assisted effort |
|---|---|---|---|
| Shopify (chatbot repo) | Order status, refund, cancel, shipping-address update — **no messaging** | 46-63 person-days | 26-33 person-days |
| Amazon (chatbot repo, planned) | Same 4 capabilities, **no messaging** | Similar order of magnitude, plus an external approval process with unknown lead time | Similar, same external-approval caveat |

Neither of those included messaging sync at all — the hardest of our three
requirements. **19 marketplaces, with orders + returns + messaging each, is not a
linear scale-up of those numbers — it's a materially larger and longer-running
commitment than a single "let's build it" project.** This section exists so that's
visible before work starts, not discovered six months in.

Rough floor, using the AI-assisted Shopify rate (26-33 days) as a per-marketplace
baseline for **orders+returns only** (messaging effort is currently unknowable — see
§2): **19 × ~30 days ≈ 570 person-days ≈ 2.5 person-years**, before any messaging work,
before the shared ingestion layer (a fixed cost, not per-marketplace, see §1), and
before the ongoing maintenance burden that starts the day each connector ships and
never stops (every marketplace API this org has touched so far — Shopify, Amazon —
has required mid-build fixes for undocumented runtime behavior; that discovery
pattern doesn't go away after launch, it recurs every time a marketplace ships an API
version change).

This isn't a reason not to do it — it's the actual shape of the "pay engineers"
decision, stated plainly so the roadmap in §5 can be resourced honestly.

---

## 0a. Module boundary + configuration (locked in 2026-09-09)

- **Separate module/folder** — all marketplace-integration code lives under its own
  namespace (e.g. `app/marketplaces/` — models, services, connectors, workers, API
  routes), not scattered across the existing `models/`/`services/`/`api/v1/` folders
  by convention-of-proximity. Keeps this large, multi-year surface area cleanly
  separable from core ITSM code, and makes it obvious in review/blame what's
  marketplace-specific.
- **Configurable, tenant-level only** — one settings row per tenant, no system-level
  default/override layer (simplified from an earlier two-level design during
  planning — tenant-level only is the confirmed scope):

  ```
  marketplace_integration_settings
      id           UUID (uuid7)
      tenant_id    UUID NOT NULL   -- one row per tenant
      settings     JSONB            -- {"enabled_marketplaces": [...], "event_mapping": {...}, ...}
      updated_at, updated_by

      UNIQUE (tenant_id)
  ```

- **One admin config page**, tenant-scoped, gated to the `tenant_admin` role (matches
  the existing `_READ_ROLES` pattern already used in `app/api/v1/integrations.py`) —
  where a tenant turns individual marketplaces on/off and sets per-event-type
  auto/manual mapping (§3's table).

---

## 1. Shared architecture (unchanged from the middleware plan — this is the part that doesn't change)

The entire ingestion/mapping layer designed in `MARKETPLACE_ITSM_INTEGRATION_PLAN.md`
§2-§8 stays exactly as-is: `marketplace_connections`, `marketplace_events`,
`marketplace_orders`, `marketplace_order_ticket_links`, the Celery task, the
`_map_order()`/`_map_return_to_ticket()`/`_map_message_to_comment()` functions, reuse
of `TicketComment`/`AutomationRule`/`WebhookEndpoint`. **None of that was middleware-specific** — it consumed a normalized event regardless of whether the event came from
one ChannelEngine webhook or from 19 different native connectors. What changes is
only the **upstream half** — instead of one `{provider: 'channelengine'}` source,
there are now 19 native producers, each translating its own marketplace's
webhooks/API shape into the same internal normalized event before it reaches the
existing Celery task.

### 1.1 `CommerceConnector` interface

Every native connector implements the same abstract shape (mirrors the
`CommerceProvider` abstraction already proposed in the chatbot repo's
`AMAZON_INTEGRATION_PLAN.md` §4.2, applied here at the itsm-service ingestion layer
instead):

```python
class CommerceConnector(ABC):
    provider: str  # 'amazon' | 'shopify' | 'walmart' | ...

    def connect(tenant_id, credentials) -> ConnectionResult
    def fetch_orders(connection, since=None) -> list[NormalizedOrder]       # manual/backfill path
    def fetch_returns(connection, since=None) -> list[NormalizedReturn]    # manual/backfill path
    def register_webhooks(connection) -> None                              # auto path, where supported
    def parse_webhook(raw_payload, headers) -> NormalizedEvent | None      # auto path
    def send_message(connection, order_id, message) -> SendResult         # outbound half of #3, where supported
    messaging_capability: Literal['none', 'outbound_only', 'full']         # set per-connector, per §2's findings
```

`messaging_capability` is deliberately a first-class field, not an afterthought — §2
shows it varies per marketplace, and the ingestion layer (§6a's outbound-reply logic)
needs to know per-connection whether "agent replies from ITSM" is even possible
before offering that action in the UI.

---

## 2. Per-marketplace assessment — Phase 0 complete (2026-09-09)

All 19 now researched to messaging-API depth. **The headline finding reverses the
pessimism §2 opened with: Amazon and Etsy turn out to be the outliers, not the
pattern.** 9 of the other 17 have confirmed, real, bidirectional (read + reply)
messaging APIs — several better-documented than Amazon's own.

| Marketplace | Auth model | Orders API | Returns/case API | Messaging API | Messaging verdict |
|---|---|---|---|---|---|
| Amazon | LWA (OAuth-like), no SigV4 needed | SP-API Orders v0 (read-mostly) | Feeds API (async, write) | **Send-only** — confirmed, Amazon's own docs: no endpoint to read buyer-sent messages at all | **outbound_only** |
| Etsy | OAuth 2.0 | Open API v3 | Not directly researched | **None at all** — confirmed via Etsy's own GitHub discussion: "incoming messages via API" is an open feature request, doesn't exist. Workaround sellers use is email outside Etsy entirely (`order.paid` webhook → buyer email → external ESP), not real messaging sync | **none** |
| Otto | OAuth2 (Service Partner Program) | OTTO Market API | Not directly researched | "Customer Communication" is email-based, handled **inside OTTO's own Partner Connect portal** — no REST messaging endpoint surfaced; looks structurally like eDesk's UI-only refund limitation (portal action, not an API call) | **none (portal-only, pending deeper check)** |
| eBay | OAuth 2.0 | Sell APIs (REST) | Post-Order API — dedicated case/return search, `CaseDetails` | Inquiries handled inside the case object | **likely full, not sandbox-validated** |
| Shopify | OAuth 2.0, expiring tokens | Admin GraphQL API | `orderCancel`/`refundCreate` mutations | No native order-tied messaging concept exists on the platform at all | **none (no concept, not an API gap)** |
| Coupang | API key (Wing) | OPEN API | Not directly researched | **Confirmed full**: `Query of Coupang Contact Center Inquiries` (GET) + `Answer to Inquiries via Coupang Contact Center` (POST) — genuine two-way CS API | **full** |
| Mercado Libre | OAuth 2.0 | REST API (per-country subdomain) | Not directly researched | **Confirmed strong**: Questions & Answers API (pre-sale) + Post-Sale Messages API (`/marketplace/messages/packs/$PACK_ID`, full conversation history w/ attachments) | **full (or very close)** |
| Trendyol | Partner Program enrollment | Partner API | Not directly researched | **Confirmed full**: `GET .../questions/filter` + `POST .../questions/{id}/answers` — well-documented, real constraints (10-2000 char, forbidden-word filter) | **full** |
| TikTok Shop | Seller-gated OAuth | Partner API | Not directly researched | **Confirmed full, best-documented of all 19**: Create Conversation, Get Conversations, Get Conversation Messages, Upload Buyer Message Image, Read Message, **Send Message** — a proper conversation model, gated behind `seller.customer_service` scope | **full** |
| Shopee | OAuth | Open Platform | Not directly researched | **Confirmed full**: Chat API — get conversations, read message history, **send messages**, receive webhook events for new messages in real time. One access gate: not every partner account gets Chat API scope automatically, needs requesting | **full, access-gated** |
| Allegro | OAuth 2.0 (PKCE) | REST API | `/sale/issues/{issueId}/messages` — GET + **POST** (issue/complaint messaging) | `GET /messaging/threads`, `GET /messaging/threads/{id}/messages` confirmed (read); issue/complaint thread has confirmed POST too | **full for issues/returns; general messaging read-confirmed, write likely** |
| Wildberries | Token-based (180-day expiry) | REST API | Buyer returns handled as a separate method (chat events used to include refund objects, now deprecated from chat in favor of a dedicated returns method) | Dedicated `user-communication` doc section exists: chat list + chat events (messages) confirmed; send-endpoint not explicitly confirmed in this pass | **likely full, send-endpoint unconfirmed** |
| Lazada | OAuth | Open Platform | Not directly researched | A dedicated **"Lazada IM Open API"** exists as its own named API surface — strong signal of real bidirectional capability, endpoint-level detail not extracted this pass | **likely full, not endpoint-confirmed** |
| Cdiscount | Manual credential request (email) | Marketplace API (Octopia) | Not directly researched | "Retrieve all discussions with customers through the API" confirmed as part of CRM scope; write/reply side implied ("increase your reactivity") but not explicitly endpoint-confirmed | **likely full or strong read, write unconfirmed** |
| Walmart | OAuth 2.0, 15-min tokens | Marketplace API — 3 separate endpoint groups | Not directly researched | No messaging/case endpoint surfaced despite a thorough pass (Seller Performance, WFS, Multichannel Solutions APIs all checked) | **unclear — needs a direct docs deep-dive, not ruled out** |
| Bol.com | OAuth2 | Retailer Partner API | Confirmed orders/returns/commission all covered | Not surfaced in this pass | **unclear — needs direct docs deep-dive** |
| Temu | ISV-style authorization | Seller/Open API | Not directly researched | Not surfaced — search returned no Temu-specific messaging documentation | **unclear — needs direct docs deep-dive** |
| Flipkart | OAuth | Seller API v3 | Confirmed: listings, orders, shipments, returns, reports all covered | Not surfaced in this pass | **unclear — needs direct docs deep-dive** |
| Zalando | OAuth 2.0 client-credentials | zDirect Platform APIs | Orders/shipments/returns/articles confirmed | Not surfaced — docs cover product/order/fulfillment lifecycle but no messaging endpoint mentioned | **unclear — needs direct docs deep-dive** |

### Summary

- **Full or likely-full bidirectional messaging: 9 of 19** — Coupang, Mercado Libre,
  Trendyol, TikTok Shop, Shopee, Allegro, Wildberries, Lazada, Cdiscount (last 3
  need endpoint-level write confirmation, but the read side and the API's existence
  are solid).
- **Outbound-only or none: 3 of 19** — Amazon (outbound-only), Etsy (none at all —
  worse than Amazon), Otto (portal-only, not clean API).
- **No native concept: 1 of 19** — Shopify (not a gap, the platform doesn't model
  order-tied messaging at all).
- **Still unclear, needs a direct docs deep-dive before Phase 2/3 build order locks
  in: 5 of 19** — Walmart, Bol.com, Temu, Flipkart, Zalando. Notably this includes
  Walmart, a Group A "easy" marketplace expected to be a near-term build target —
  worth resolving before Phase 2 rather than discovering it mid-build the way
  Amazon's restriction was discovered mid-plan.

**This changes the effort/risk posture from §0 meaningfully for the better on
messaging specifically** — the working assumption going into Phase 0 was that
Amazon's restriction might be a preview of a systemic problem; instead it looks
like an Amazon-specific (and Etsy-specific) quirk, and most of the 19 have a real,
buildable messaging API once their order/return connector work is underway anyway.

---

## 3. Reuse opportunities (reduces the §0 floor, doesn't eliminate it)

- **Amazon + Shopify**: the chatbot repo (`Project-IQ-V2/action_server/actions/actions_amazon.py`,
  `actions_shopify.py`, `common/db/_amazon.py`, `_shopify.py`) already has working
  OAuth, token storage/refresh, and API-calling code for both — built for a different
  purpose (customer support order lookup), but the auth/connection layer is directly
  extractable into a shared library both repos import, rather than rebuilt from
  scratch in itsm-service. This is the single biggest effort-reduction opportunity
  available and should be Phase 2's starting point.
- **Per-marketplace open-source SDKs**: e.g. `saleweaver/python-amazon-sp-api` for
  Amazon — real, maintained, handles auth/Orders/Feeds/Reports boilerplate. Worth
  checking for each of the 19 individually during Phase 0/2 rather than hand-rolling
  raw REST clients — cuts boilerplate, not the harder data-model/discovery work (the
  Shopify refund saga in `SHOPIFY_INTEGRATION_PLAN.md` §6b took 4 live-test-fix
  cycles *with* a working GraphQL client already in hand — an SDK doesn't remove that
  category of cost).
- **The shared `CommerceConnector` interface (§1.1) and ingestion layer are a fixed
  cost, built once** — not multiplied by 19. Getting this right in Phase 1 is what
  makes marketplace #4 onward cheaper than marketplace #1-3.

---

## 4. Effort estimate (honest ranges, not false precision)

| Component | Effort | Notes |
|---|---|---|
| Phase 0 — messaging API research, 16 marketplaces | 8-16 days | ~0.5-1 day per marketplace to confirm messaging capability from docs, matching the depth already done for Amazon/Shopify/eBay |
| Phase 1 — shared `CommerceConnector` interface + ingestion layer | 15-20 days | Reuses `MARKETPLACE_ITSM_INTEGRATION_PLAN.md` §2-§8's design almost entirely; the itsm-service-side webhook receiver, Celery task, and data model don't change from the middleware plan |
| Phase 2 — Amazon + Shopify connectors (extracted from chatbot repo) | 15-25 days combined | Lower than building fresh, given §3's reuse; still needs itsm-specific work (orders/returns mapping wasn't part of the chatbot build, which only did support-agent order lookup) |
| Phase 3 — remaining Group A (Walmart, eBay, Etsy, Allegro, Bol.com, Otto, Coupang, Mercado Libre, Wildberries — 9 marketplaces) | 9 × 20-30 days ≈ **180-270 days** | Each is a fresh build; "easy" (Group A) only describes auth friction, not full-scope effort — same discovery-cost pattern as Shopify/Amazon should be expected per marketplace |
| Phase 4 — Group B (Temu, Cdiscount, Lazada, Shopee, Flipkart, Trendyol, TikTok Shop, Zalando — 8 marketplaces) | 8 × 25-35 days ≈ **200-280 days** | Higher per-marketplace estimate — "medium friction" tier means approval-gate delays add elapsed (not effort) time, same category of risk flagged for Amazon's Public Developer review |
| **Total, orders+returns only, all 19** | **~430-630 person-days (~1.7-2.5 person-years)** | Excludes messaging entirely — Phase 0 determines whether that's addable per-marketplace or needs separate scoping |
| Ongoing maintenance, all 19 live | Not a phase — a standing cost | Every marketplace API in this org's direct experience (Shopify, Amazon) required mid-build fixes for undocumented behavior; expect recurring maintenance work as each of 19 APIs versions/deprecates independently, indefinitely |

This roughly matches or exceeds §0's floor estimate once the shared-layer and
reuse credits are netted against the per-marketplace discovery-cost reality — the
19-marketplace scope is a multi-year program, not a project with an end date.

---

## 5. Phased roadmap

| Phase | Scope | Depends on |
|---|---|---|
| **0 — Messaging API research, 16 marketplaces** ✅ **Done 2026-09-09** | Closed §2's gap — 9/19 confirmed full bidirectional messaging (better than expected), 3/19 outbound-only-or-none (Amazon, Etsy, Otto), 1/19 no concept (Shopify), 5/19 still need a direct docs deep-dive (Walmart, Bol.com, Temu, Flipkart, Zalando) before their build order locks in | Nothing — pure research, done |
| **1 — Shared connector interface + ingestion layer** | `CommerceConnector` ABC (§1.1), `marketplace_connections`/`marketplace_events`/`marketplace_orders`/`marketplace_order_ticket_links` migrations, webhook receiver skeleton, Celery task — this is `MARKETPLACE_ITSM_INTEGRATION_PLAN.md` §2-§8 built as designed, just fed by native connectors instead of ChannelEngine | Nothing — can run parallel to Phase 0 |
| **2 — Amazon + Shopify connectors (pilot)** | Extract/adapt chatbot repo's existing OAuth code into a shared library; build orders+returns mapping (new work, not in the chatbot build); messaging per Phase 0's findings (Amazon known outbound-only already) | Phase 1, Phase 0 for messaging scope |
| **3 — Remaining Group A (9 marketplaces)** | Walmart, eBay, Etsy, Allegro, Bol.com, Otto, Coupang, Mercado Libre, Wildberries — one connector at a time, same pattern as Phase 2 | Phase 2 proven, Phase 0's per-marketplace messaging findings |
| **4 — Group B (8 marketplaces)** | Temu, Cdiscount, Lazada, Shopee, Flipkart, Trendyol, TikTok Shop, Zalando — expect real elapsed-time delays from approval gates (Temu's ISV authorization, Trendyol/Zalando/Otto's Partner Program enrollment), not just build effort | Phase 3 pattern proven |
| **Ongoing — maintenance** | Standing team capacity for API version churn across 19 live connectors, indefinitely | Starts the moment Phase 2 ships, never ends |

---

## 6. Team & resourcing

Given §4's scale, this needs sustained backend engineering capacity, not a short
sprint — realistically 1-2 backend engineers dedicated for the better part of 2
years to reach all 19, longer if messaging turns out to need custom per-marketplace
work (Phase 0 will clarify). Compare this resourcing commitment against what a
vendor subscription would have cost over the same period (ChannelEngine's GMV-based
fee, or even Rithum's $1,500-6,000+/mo) before finalizing — not to re-open the
buy-vs-build decision, but because the actual multi-year cost of "pay engineers"
should be a known number going in, the same way §0 made the effort floor visible
upfront.

---

## 7. Risks

- **16 of 19 marketplaces have unresearched messaging capability** — Amazon's
  surprise restriction means this can't be assumed away; Phase 0 could reveal
  several more marketplaces are outbound-only or worse, changing requirement #3's
  real scope significantly.
- **Approval-gate delays are external and elapsed-time, not effort** — Amazon's
  Public Developer review, Temu's ISV authorization, and several Group B
  marketplaces' Partner Program enrollment are outside engineering's control,
  same risk category already flagged in `AMAZON_INTEGRATION_PLAN.md` §7.
- **Discovery cost repeats per marketplace, not just per API-family** — the Shopify
  refund saga (4 live-test-fix cycles) and Amazon's SP-API structural surprises both
  happened *within* well-documented, "standard-looking" REST/OAuth2 APIs — there's
  no reason to expect the other 17 to be cleaner, and every planning number in §4
  assumes some of this cost, not zero.
- **Maintenance is a permanent headcount commitment, not a project cost** — 19 live
  integrations against 19 independently-evolving marketplace APIs is a standing
  operational burden this org doesn't currently carry at any scale.
- **No fallback if a marketplace's API turns out not to support something needed**
  (e.g. Amazon's messaging) — unlike a vendor relationship, there's no one to
  escalate to; the org owns the gap permanently once committed.

---

## 8. Decisions needed from you

1. **Confirm starting Phase 0 (messaging research) immediately** — it's pure
   research, blocks nothing else, and directly changes what Phase 2-4 actually cost.
2. **Confirm the build order** — this plan assumes Amazon+Shopify pilot first (§5
   Phase 2), reusing the chatbot repo's existing OAuth code; alternative: start with
   whichever marketplace has the org's highest current order volume, if that's not
   Amazon/Shopify.
3. **Resourcing** — who is actually assigned to this, starting when, and is it 1
   engineer part-time or a dedicated 1-2 person team (§6's estimate assumes the
   latter for the ~2-year timeline to hold).
4. **Is `PHASE0_VENDOR_OUTREACH.md` fully shelved, or kept as a fallback** — this
   plan treats it as on-hold, not cancelled, given the scale in §0/§4; worth an
   explicit decision rather than letting it go stale silently.
5. **Messaging scope tolerance** — if Phase 0 reveals more marketplaces are
   messaging-restricted like Amazon, is outbound-only (or no messaging at all) an
   acceptable permanent state for those, or does that reopen the vendor conversation
   for messaging specifically even if orders/returns stay native?
