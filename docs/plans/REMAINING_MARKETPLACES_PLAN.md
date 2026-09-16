# Remaining Marketplaces — Plan

Status: **planning only — no implementation, no migration, no code changes yet**
Scope: the 12 marketplaces still outside the 13 already built (Shopify, Amazon,
Walmart, eBay, Etsy, Mercado Libre, Allegro, Cdiscount, Lazada, Wildberries,
Bol.com, Zalando, Flipkart) — 6 from `MARKETPLACE_ITSM_INTEGRATION_PLAN.md`'s
original 19-marketplace scope that were never built, plus 6 major global
marketplaces that were never in scope at all until asked about directly
(2026-09-16).

---

## 1. Priority table (all 12)

| Marketplace | Real messaging? | Access friction | Recommendation |
|---|---|---|---|
| **Best Buy Marketplace** | ✅ Confirmed — Mirakl Inbox Threads API (`OR41`), corroborated by third-party tools already using it live | Standard Mirakl seller-portal API key | **Build first** — best candidate in this whole batch |
| **Shopee** | Likely real (Chat API) — but primary docs blocked this research pass (JS-rendered SPA) | OAuth 2.0, chat is a separately-gated scope | **Redo research with a different fetch approach**, then build if confirmed |
| **Newegg** | Real messaging feature exists, but **UI-only — not exposed via API at all** | Partner registration (API key + secret) | **Email-fallback connector only** (masked `CustomerEmailAddress` works) — no messaging connector possible |
| **Rakuten** | ❌ False positive caught — `MessageModelList` is API status codes, not chat. No real channel found | Not self-serve — consultative onboarding + contract, Japanese-only docs/UI | **Skip** — no confirmed messaging, high access friction for what's left (email fallback, and even that's masked) |
| **JD.com (JD Worldwide)** | Unclear — "work orders" + message-channel push exist, but exact endpoint blocked from direct doc access | Not self-serve — brand/licensee proof, USD bank account, **$15k refundable deposit**, $1k/yr fee, BD-approved application | **Business decision, not engineering** — the deposit alone makes this a cost call before any research/build time is justified |
| **Pinduoduo** | ❌ No confirmed messaging endpoint (only order-status webhooks found) | PDD Global/cross-border path exists (no mainland entity needed), but still approval-based, not self-serve | **Skip for now** — no messaging found, access still gated even via the Global path |
| **Vinted** | ❌ No messaging, no buyer note, no buyer email — nothing to build with | API is **allowlisted-only**, not open to general businesses at all | **Skip entirely** — not buildable regardless of messaging capability |
| **TikTok Shop** | Real Conversations API exists | Likely gated behind a 1,000-seller/1M-calls-per-day approval threshold | **Skip unless that gate is confirmed passable** for this org specifically |
| **Coupang** | ❌ CS-ticket-reply only, not live chat; buyer email explicitly removed Aug 2024 | — | **Skip** — nothing real to build |
| **Trendyol** | ❌ Only public pre-sale Q&A, not private order messaging | — | **Skip** — nothing real to build |
| **Otto** | Unconfirmed across the board (docs silent/unrenderable) | Unconfirmed | **Skip unless re-researched properly** — current findings too thin to act on |
| **Temu** | ❌ No evidence of any messaging API | Unconfirmed | **Skip** — lowest-confidence marketplace researched so far |

## 2. Recommended build order

1. **Best Buy Marketplace** — real, confirmed, corroborated messaging. Build
   the same way the last batch was built (connector + connect flow + registry),
   live-testing once a real Mirakl seller API key exists.
2. **Newegg** — thin, email-fallback-only connector (same shape as bol.com/
   Zalando/Flipkart in the messaging-only batch) — real masked email, no
   messaging API to build against.
3. **Shopee** — worth a proper research redo (try `WebFetch` with different
   headers/rendering approach, or find a real OpenAPI spec mirror) before
   deciding build-or-skip. Don't build blind against the low-confidence
   findings from this pass.

Everything else in the table: **skip**, for a real, cited reason each — not
because research ran out of time.

## 3. A strategic note on Mirakl

Best Buy Marketplace runs on **Mirakl**, a marketplace-technology platform
also confirmed to power Cdiscount (via Octopia, itself Mirakl-adjacent —
worth double-checking whether Octopia and raw Mirakl are the same platform
family) and known publicly to power many other retailers' marketplaces
(Kroger, Macy's, Urban Outfitters, Carrefour, and others — not independently
confirmed in this research pass, flagged as worth checking). If Mirakl's
Inbox Threads API generalizes across every Mirakl-powered marketplace with
just a different base URL/shop ID, **one well-built Mirakl connector could
cover several marketplaces at once**, not just Best Buy — worth confirming
before committing to a Best-Buy-specific implementation that might be
needlessly narrow.

## 4. Business decisions needed before any further research/build time

- **JD.com**: is the $15k refundable deposit + $1k/year fee + BD-approval
  process worth pursuing for messaging capability that isn't even confirmed
  yet? This should be decided before spending more research time on it, not
  after.
- **TikTok Shop**: does this org's actual seller volume clear the 1,000-
  seller/1M-calls approval threshold? If not, there's nothing to build
  regardless of how good the API is.

## 5. What was explicitly re-confirmed as out of scope

Same as `MARKETPLACE_ITSM_INTEGRATION_PLAN.md`'s original Group C/D findings,
not reopened here: AliExpress/Alibaba.com (approval-gated, no OpenAPI spec),
Taobao/Tmall (mainland China business entity required), Meta Commerce
(invite-only beta), Target Plus/Wayfair/Noon (no public self-serve API found).
