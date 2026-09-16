from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    APP_ENV: str = "development"
    SECRET_KEY: str
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000"]
    # Base URL of the Project-IQ-V2 web app that serves this repo's frontend
    # (itsm-service has no frontend of its own — web/ui/src/itsm-app/ lives in
    # Project-IQ-V2, served at /itsm). Needed because the marketplace OAuth
    # callbacks below are hit directly by the marketplace (Shopify/Amazon/eBay/
    # Etsy) against THIS API's own origin — a bare RedirectResponse("/admin/...")
    # resolves against that origin (this API, port 8000, no such route -> 404),
    # not the frontend's origin. Confirmed live 2026-09-14 (Shopify connect
    # succeeded but landed on a 404 for exactly this reason).
    ITSM_FRONTEND_URL: str = "http://localhost:8181"

    DATABASE_URL: str
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_STATEMENT_TIMEOUT_SECONDS: int = 30
    # Celery worker/beat processes run each task via its own asyncio.run()
    # call — a fresh event loop every time — but app/database.py's engine
    # (and its connection pool) is one long-lived object shared across all
    # of them in the same forked process. asyncpg connections are bound to
    # the event loop they were opened on, so a pooled connection from task
    # N's (now-closed) loop is invalid if handed to task N+1's loop: raises
    # "InterfaceError: cannot perform operation: another operation is in
    # progress". Set true (via docker-compose.yml) only for worker/worker-beat
    # so they use NullPool (no connection reuse, so nothing to go stale across
    # loops) — the FastAPI api process keeps normal pooling since it runs one
    # event loop for its whole lifetime and benefits from it.
    IS_CELERY_WORKER: bool = False

    REDIS_URL: str = "redis://localhost:6379/0"

    IAM_BASE_URL: str
    IAM_TOKEN_URL: str = ""
    IAM_ISSUER: str = ""
    IAM_AUDIENCE: str = "itsm"
    IAM_CLIENT_ID: str
    IAM_CLIENT_SECRET: str
    IAM_JWKS_URL: str
    # Optional proxy for just the JWKS fetch (e.g. "socks5://172.21.0.1:1080").
    # Unset in normal operation — only needed when this host's own network
    # can't reach IAM_JWKS_URL directly (see refresh_jwks() in app/auth/jwks.py).
    IAM_JWKS_PROXY: str = ""
    IAM_WEBHOOK_SECRET: str
    # Static bearer token for inbound IAM→ITSM internal calls (sync endpoint).
    IAM_INTERNAL_TOKEN: str = ""
    PLATFORM_ORG_ID: str

    # Shared secret with Project-IQ-V2's WhatsApp integration — see
    # app/auth/internal_assertion.py. Blank (default) disables that path
    # entirely; every request then goes through normal Keycloak verification
    # exactly as before this existed. Never log it.
    WHATSAPP_INTERNAL_ASSERTION_SECRET: str = ""

    STORAGE_ENDPOINT: str = "http://localhost:9000"
    # Browser-reachable endpoint used when SIGNING presigned URLs. Defaults to
    # STORAGE_ENDPOINT; set this when the internal endpoint (e.g. docker
    # "minio:9000") isn't resolvable by the end user's browser (e.g.
    # "http://localhost:9000"). Without it, uploads fail with "Failed to fetch".
    STORAGE_PUBLIC_ENDPOINT: str = ""
    STORAGE_BUCKET: str = "itsm-files"
    STORAGE_ACCESS_KEY: str = "minioadmin"
    STORAGE_SECRET_KEY: str = "minioadmin"
    STORAGE_PRESIGN_EXPIRY_SECONDS: int = 3600

    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    EMAIL_FROM_NAME: str = "ITSM Support"

    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    ANTHROPIC_API_KEY: str
    CLAUDE_MODEL: str = "claude-sonnet-4-6"
    CLAUDE_MAX_TOKENS: int = 2048
    CLAUDE_TIMEOUT_SECONDS: int = 30

    OPENAI_API_KEY: str
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSIONS: int = 1536

    # Generation provider switch — "anthropic" (default, prod), "gemini", or
    # "groq" (free-tier fallbacks for pre-prod demo use, see AIService.
    # generate()). Does not affect embed() — that's always OpenAI regardless
    # of this setting; no free-tier swap wired for embeddings yet.
    AI_PROVIDER: str = "anthropic"
    GOOGLE_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.6-flash"
    # Separate (larger) budget from CLAUDE_MAX_TOKENS — this model spends a
    # meaningful chunk of max_output_tokens on internal "thinking" tokens
    # before any visible output (confirmed live: ~148 thinking tokens for a
    # one-word answer), so curation's longer prompts (full article sections)
    # need real headroom on top of that or finish_reason=MAX_TOKENS truncates
    # to empty content before writing anything visible.
    GEMINI_MAX_OUTPUT_TOKENS: int = 8192

    # Groq — OpenAI-compatible endpoint, so reuses the openai SDK directly
    # (see AIService.generate()) rather than a separate client library.
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"

    AI_DEFAULT_MONTHLY_TOKEN_BUDGET: int = 5_000_000
    AI_DUPLICATE_THRESHOLD: float = 0.85
    AI_MAINTENANCE_BATCH_SIZE: int = 50

    # Project-IQ-V2's Postgres (KB_WIKI_CURATION_RAG_PLAN Phase 5) — the
    # gap-clustering job reads kb_search_gaps directly from that repo's DB,
    # mirroring Project-IQ-V2's own kb_sync.py ITSM_DB_* pattern in reverse.
    PIQ_DB_HOST: str = "localhost"
    PIQ_DB_PORT: int = 5432
    PIQ_DB_NAME: str = "Chatbot"
    PIQ_DB_USER: str = "postgres"
    PIQ_DB_PASSWORD: str = ""
    KB_GAP_CLUSTER_SIMILARITY_THRESHOLD: float = 0.85
    KB_GAP_CLUSTER_MIN_COUNT: int = 5
    KB_GAP_CLUSTER_LOOKBACK_DAYS: int = 7

    SENTRY_DSN: str = ""
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""

    # Native marketplace integration (V3-Marketplaces) — Shopify connector,
    # docs/plans/NATIVE_MARKETPLACE_CONNECTORS_PLAN.md §5 pilot batch.
    # One Shopify "app" (these credentials) shared across every tenant that
    # connects a store, same model as Project-IQ-V2's existing Shopify
    # integration — each tenant's own shop_domain/tokens live in
    # MarketplaceConnection, not here.
    SHOPIFY_ENABLED: bool = False
    SHOPIFY_CLIENT_ID: str = ""
    SHOPIFY_CLIENT_SECRET: str = ""
    SHOPIFY_REDIRECT_URI: str = ""
    SHOPIFY_SCOPES: str = "read_orders,read_returns"
    SHOPIFY_API_VERSION: str = "2026-01"

    # Native marketplace integration — Amazon connector (pilot batch #2)
    AMAZON_ENABLED: bool = False
    AMAZON_APP_ID: str = ""
    AMAZON_CLIENT_ID: str = ""
    AMAZON_CLIENT_SECRET: str = ""
    AMAZON_MARKETPLACE_IDS: str = ""  # comma-separated, e.g. "ATVPDKIKX0DER" (US)
    AMAZON_ENVIRONMENT: str = "sandbox"  # 'sandbox' | 'production'

    # Native marketplace integration — Walmart connector (pilot batch #3).
    # No app-level client_id/secret here (unlike Shopify/Amazon) — Walmart's
    # model is per-tenant API keys (client_credentials grant), not a single
    # app many sellers OAuth-consent into. See connectors/walmart.py.
    WALMART_ENABLED: bool = False

    # Native marketplace integration — eBay connector (pilot batch #4).
    EBAY_ENABLED: bool = False
    EBAY_CLIENT_ID: str = ""
    EBAY_CLIENT_SECRET: str = ""
    EBAY_REDIRECT_URI: str = ""  # eBay calls this a "RuName", not a raw URL — see connectors/ebay.py
    EBAY_ENVIRONMENT: str = "sandbox"  # 'sandbox' | 'production'
    # sell.post-order dropped (2026-09-14, diagnostic) — eBay kept rejecting
    # the authorize request with error=invalid_scope even after confirming
    # both sell.fulfillment and sell.post-order were checked+saved on the
    # OAuth Scopes page. The Select-OAuth-Scopes UI lets you REQUEST a
    # scope; it doesn't guarantee your account is actually ENTITLED to it —
    # eBay's Post-Order API has historically been a separately-gated API,
    # not automatically granted to every sandbox keyset. Testing with just
    # sell.fulfillment to isolate whether that's the actual blocker; if this
    # connects, fetch_returns() (which needs post-order) stays broken until
    # that entitlement is granted separately — flagged, not silently dropped.
    EBAY_SCOPES: str = "https://api.ebay.com/oauth/api_scope/sell.fulfillment"

    # Native marketplace integration — Etsy connector (pilot batch #5).
    ETSY_ENABLED: bool = False
    # ETSY_CLIENT_ID = the "Keystring" — used as OAuth client_id AND the
    # x-api-key header on every API call (see connectors/etsy.py).
    ETSY_CLIENT_ID: str = ""
    # Captured but NOT currently sent by connectors/etsy.py's token-exchange
    # call — Etsy's documented v3 OAuth flow is PKCE-based and doesn't take a
    # client_secret parameter in that request. The developer portal issued
    # one anyway (confirmed live, 2026-09-14), so it's stored here rather
    # than discarded — if the connector's token exchange ever needs it (e.g.
    # confirm-worthy detail this org's own Shopify/Amazon builds ran into
    # more than once: docs vs. live behavior diverging), it's already
    # available without another portal trip.
    ETSY_CLIENT_SECRET: str = ""
    ETSY_REDIRECT_URI: str = ""
    ETSY_SCOPES: str = "transactions_r"

    # --- Messaging-only marketplaces (2026-09-15) — added to give buyer
    # communication coverage for marketplaces beyond the original 5-connector
    # pilot batch, per explicit request. See each connector module's
    # docstring for the "messaging-only, no orders/returns sync" scope note
    # and the "UNVERIFIED — no live sandbox credentials" caveat that applies
    # to every one of these, unlike the pilot batch which all got live
    # sandbox testing.
    MERCADOLIBRE_ENABLED: bool = False
    MERCADOLIBRE_CLIENT_ID: str = ""
    MERCADOLIBRE_CLIENT_SECRET: str = ""
    MERCADOLIBRE_REDIRECT_URI: str = ""
    # Country-specific — the seller's Mercado Libre account belongs to ONE
    # site (.com.ar, .com.mx, .com.br, ...); the auth SCREEN is on that
    # site's own subdomain even though token exchange is on the single
    # global api.mercadolibre.com host. Defaulted to Argentina; override
    # per the actual seller account's site.
    MERCADOLIBRE_AUTH_DOMAIN: str = "https://auth.mercadolibre.com.ar"

    ALLEGRO_ENABLED: bool = False
    ALLEGRO_CLIENT_ID: str = ""
    ALLEGRO_CLIENT_SECRET: str = ""
    ALLEGRO_REDIRECT_URI: str = ""
    # Allegro has a genuinely separate sandbox environment (not just a flag
    # on the production host) — allegrosandbox.pl vs allegro.pl, both auth
    # and API hosts. See connectors/allegro.py.
    ALLEGRO_ENVIRONMENT: str = "sandbox"

    CDISCOUNT_ENABLED: bool = False
    # Cdiscount's marketplace API is run through Octopia (their marketplace
    # tech platform, not Cdiscount-branded hosts) — client naming kept
    # Cdiscount-prefixed since that's the marketplace a tenant is actually
    # connecting to, even though the underlying API is Octopia's.
    CDISCOUNT_SELLER_ID: str = ""
    CDISCOUNT_API_KEY: str = ""

    LAZADA_ENABLED: bool = False
    LAZADA_CLIENT_ID: str = ""  # Lazada calls this "App Key"
    LAZADA_CLIENT_SECRET: str = ""  # "App Secret"
    LAZADA_REDIRECT_URI: str = ""

    WILDBERRIES_ENABLED: bool = False
    # No OAuth — a single long-lived API token generated per-seller in the
    # Wildberries seller portal, submitted directly (client_credentials-
    # style, same shape as this repo's existing Walmart connector).
    WILDBERRIES_API_TOKEN: str = ""

    # --- Thin, email-fallback-only marketplaces (2026-09-15) — none of
    # these have ANY native messaging API (confirmed via research); each
    # connector exists solely to capture a real buyer_email on the order so
    # the existing email-fallback mechanism (marketplace_sync.py's
    # send_message_to_buyer) has something to address. See each connector's
    # module docstring.
    BOLCOM_ENABLED: bool = False
    BOLCOM_CLIENT_ID: str = ""
    BOLCOM_CLIENT_SECRET: str = ""

    ZALANDO_ENABLED: bool = False
    ZALANDO_CLIENT_ID: str = ""
    ZALANDO_CLIENT_SECRET: str = ""

    FLIPKART_ENABLED: bool = False
    FLIPKART_CLIENT_ID: str = ""
    FLIPKART_CLIENT_SECRET: str = ""
    FLIPKART_REDIRECT_URI: str = ""

    # Observability / hardening (S4.2)
    RATE_LIMIT_PER_MINUTE: int = 60
    RATE_LIMIT_BURST: int = 10
    OTEL_ENABLED: bool = False
    OTEL_ENDPOINT: str = "http://localhost:4317"
    PROMETHEUS_ENABLED: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
