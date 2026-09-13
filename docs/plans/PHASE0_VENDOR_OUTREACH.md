# Phase 0a + 0b — Vendor Confirmation: Findings & Outreach Package

Companion to `MARKETPLACE_ITSM_INTEGRATION_PLAN.md` §9 (Phase 0a/0b). Both phases are
vendor conversations that need a human to actually send/sign/call — this document is
everything an AI research pass could resolve from public sources, plus the exact
questionnaires to send for what's left. Status: **research complete, outreach not yet
sent** (needs an owner — plan §10 decision 5).

**Confirmed target scope (2026-09-09): Group A + Group B = 19 marketplaces.**
Group C (AliExpress, Alibaba.com — high friction/geo-restricted) and Group D
(Taobao/Tmall, Meta Commerce order API, Target Plus/Wayfair/Noon — blocked by
legal/access gates independent of any vendor) are explicitly **out of scope** for
this outreach round:

- **Group A (11):** Amazon, Shopify, Walmart, eBay, Etsy, Allegro, Bol.com, Otto, Coupang, Mercado Libre, Wildberries
- **Group B (8):** Temu, Cdiscount, Lazada, Shopee, Flipkart, Trendyol, TikTok Shop, Zalando

---

## Phase 0a — ChannelEngine

### What public docs already confirm (no sales call needed for this part)

| Item | Finding | Source |
|---|---|---|
| Correct API surface | **Merchant API** — not the *Channel API*, which is for building your own marketplace (wrong product for our use case; both exist under the ChannelEngine umbrella and are easy to conflate) | `channelengine.com/developer-hub` |
| Authentication | Header-based API key: `x-ce-key: <your-api-key>` — not OAuth, not HMAC | ChannelEngine Help Center |
| Webhook registration | `POST /v2/webhooks` with `Name`, `Url`, `IsActive`, `Events` (e.g. `"ORDERS_CREATE"`) | ChannelEngine Help Center |
| Webhook security | **Shared auth key/value pair** (delivered as a header or query-string parameter you define) — **not cryptographic signing**. This corrects the main plan doc's §5, which assumed an HMAC-SHA256 scheme mirroring `iam_webhook` — ChannelEngine's model is simpler (a static shared secret compared, not a computed digest over the body) | ChannelEngine Help Center |
| Available webhook events | `ORDERS_CREATE`, `ORDERS_CHANGE`, `RETURNS_CHANGE`, `SHIPMENTS_CHANGE`, `PRODUCTS_CHANGE` — confirms returns are a first-class webhook-able event, validating the plan's return/replacement sync design | ChannelEngine Help Center |
| Sandbox access | **Not self-serve** — a "test/development account" must be requested (a dedicated Help Center article exists for this, content itself was blocked to automated fetch — a human needs to open it directly) | `support.channelengine.com` (article exists, 403'd to automated fetch) |
| Free trial | **None** — confirmed no free-tier/trial exists; a test account is provisioned on request, presumably tied to an active or pending commercial relationship | Third-party review aggregators |

### What still needs a real conversation (can't be resolved from public docs)

1. Exact JSON payload shape for each webhook event (`ORDERS_CREATE`, `RETURNS_CHANGE`, etc.) — public docs describe the event names and registration mechanism but not field-level payload schemas.
2. Written, per-marketplace confirmation against this org's confirmed 19-marketplace
   target list above (not the "1300+ channels" marketing figure) — every one of
   Group A + B, individually, not "the platform generally supports this region."
3. Pricing — GMV-based, no public rate card found.
4. Whether the shared-key webhook auth can be rotated/scoped per-tenant (itsm-service is multi-tenant; need one ChannelEngine account+webhook-key per tenant, not one global key).

### Ready-to-send request (Step 1 — request the test account)

Submit via ChannelEngine's support/sales contact form (the Help Center article
"ChannelEngine: how to get a test/development account" documents the process but
requires being read directly — start there):

> Subject: Merchant API test/development account request — [org name]
>
> We're evaluating ChannelEngine's Merchant API to sync orders and returns from the
> following 19 marketplaces: Amazon, Shopify, Walmart, eBay, Etsy, Allegro, Bol.com,
> Otto, Coupang, Mercado Libre, Wildberries, Temu, Cdiscount, Lazada, Shopee,
> Flipkart, Trendyol, TikTok Shop, Zalando — into our internal ITSM platform.
> Requesting:
> 1. A Merchant API test/development account with sandbox order + return webhook
>    events enabled (`ORDERS_CREATE`, `ORDERS_CHANGE`, `RETURNS_CHANGE`).
> 2. Full JSON payload schema/examples for those three webhook events.
> 3. Written confirmation of current integration support for **each of the 19
>    marketplaces listed above individually**, specifically for order + return sync
>    (not just listing sync) — please flag any of the 19 not currently supported.
> 4. Current Merchant API pricing structure for our approximate order volume
>    [fill in monthly order volume estimate].

---

## Phase 0b — Messaging vendor

### ChannelReply — findings (negative signal, not disqualifying, but real)

| Item | Finding |
|---|---|
| Supported helpdesks | Exactly 8, all named commercial products: Zendesk, Freshdesk, Help Scout, Gorgias, Re:amaze, Zoho Desk, Kustomer, Onsite Support — **itsm-service is not one of them, and no generic/custom webhook target is advertised anywhere on their site** |
| Marketplace coverage | 10 channels: Amazon, eBay, Shopify, Walmart, Back Market, Newegg, Etsy, WooCommerce, +Mirakl/Octopia (beta) — **against the confirmed 19-marketplace target, that's 4 of 19 covered (Amazon, eBay, Shopify, Walmart)**; Etsy overlaps too (5 of 19), but Allegro, Bol.com, Otto, Coupang, Mercado Libre, Wildberries, Temu, Cdiscount, Lazada, Shopee, Flipkart, Trendyol, TikTok Shop, Zalando are all absent |
| API/auth/Amazon-sourcing details | Not publicly documented at all — their own API profile listing (apitracker.io) shows the fields tracked but every value is blank | 
| **Read on this**: | Against the *original* placeholder 8-marketplace list this looked like a partial fit; against the confirmed 19-marketplace target it's under a third covered — worth weighting ChannelReply lower in the shortlist accordingly, though still worth sending the questionnaire since messaging coverage doesn't have to come from one vendor for all 19 the way orders/returns should. |

### Candidate shortlist for Phase 0b outreach

Not a recommendation — a list to run the same questionnaire against, since the plan's
messaging-vendor decision (§10 item 2) is still explicitly open:

1. **ChannelReply** — narrower marketplace coverage, no public custom-target story (per above)
2. **eDesk** (formerly xSellco) — broader coverage (250+ channels claimed), has a real
   public developer API (`developers.edesk.com/reference`, OpenAPI/Swagger, webhooks
   via "message rules") — listed here as a candidate to evaluate, not as an endorsed
   choice; the main plan doc was deliberately reset to vendor-neutral on this
   decision. **Correction (2026-09-09):** eDesk's own support docs
   (`support.edesk.com/4-ways-use-edesk-api`) confirm their public API's 4 documented
   use cases are all *eDesk-internal* (order tracking updates, ticket/message
   creation, QA data pulls, custom-field sync) — **not a pass-through to trigger
   marketplace-side actions**. Their own docs state refunds are processed by an agent
   working manually inside eDesk's UI, which then talks to Amazon/eBay through
   eDesk's own internal (non-public) connection — not exposed to API callers. This
   makes questionnaire question 2 (send-replies-via-API) the load-bearing one for
   eDesk specifically — current public documentation suggests the answer may be no,
   which would rule it out for requirement #3's outbound half unless their team
   confirms otherwise directly.
3. Worth a quick look, not yet researched in depth: **Replydesk** (appeared in earlier
   search results as another Amazon-helpdesk bridge) — unverified, add to the
   questionnaire batch rather than skip

### Ready-to-send questionnaire (send the same one to each candidate)

> Subject: Marketplace messaging integration evaluation — [org name]
>
> We're evaluating a vendor to sync buyer messages (order and return/replacement
> context) from the following 19 marketplaces: Amazon, Shopify, Walmart, eBay, Etsy,
> Allegro, Bol.com, Otto, Coupang, Mercado Libre, Wildberries, Temu, Cdiscount,
> Lazada, Shopee, Flipkart, Trendyol, TikTok Shop, Zalando — into our internal,
> custom-built ITSM platform, not one of the commercial helpdesks you list as
> supported integrations. Specifically need to know:
> 1. Can your platform deliver message data to a **custom webhook endpoint** we
>    control, rather than only to named helpdesk integrations? If yes, what's the
>    payload shape and auth/signing scheme?
> 2. Can we **send** replies back out through your platform via API (agent reply →
>    buyer), not just receive?
> 3. For Amazon specifically: how do you source buyer messages, given Amazon's
>    Selling Partner API has no endpoint to read buyer-sent messages? (Legitimate
>    mechanisms exist — e.g. Amazon's buyer-message email-forwarding feature — asking
>    to confirm which mechanism you use and its reliability/limitations.)
> 4. Confirm current support for **each of the 19 marketplaces above individually** —
>    please flag any not currently supported.
> 5. Pricing structure for [agent count / message volume estimate].

---

## Next step

Both questionnaires are ready to send — this needs a human owner (plan §10 decision
5) to actually dispatch them; nothing further can be resolved from public research
alone. Once responses come back, they slot directly into `MARKETPLACE_ITSM_INTEGRATION_PLAN.md`
§9's Phase 0a/0b rows to unblock Phase 1.
