from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    APP_ENV: str = "development"
    SECRET_KEY: str
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000"]

    DATABASE_URL: str
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_STATEMENT_TIMEOUT_SECONDS: int = 30

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

    AI_DEFAULT_MONTHLY_TOKEN_BUDGET: int = 5_000_000
    AI_DUPLICATE_THRESHOLD: float = 0.85
    AI_MAINTENANCE_BATCH_SIZE: int = 50

    SENTRY_DSN: str = ""
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""

    # Observability / hardening (S4.2)
    RATE_LIMIT_PER_MINUTE: int = 60
    RATE_LIMIT_BURST: int = 10
    OTEL_ENABLED: bool = False
    OTEL_ENDPOINT: str = "http://localhost:4317"
    PROMETHEUS_ENABLED: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
