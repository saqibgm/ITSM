"""
Test configuration and JWT factory fixtures for IQ-ITSM.

All token factories produce RS256-signed JWTs using an in-process test RSA key.
The ``verify_token`` function can be patched via ``monkeypatch`` or
``pytest-mock`` to bypass JWKS network calls in unit tests.

Usage in tests
--------------
Direct factory call::

    token = make_platform_admin_token()

Via fixture::

    async def test_foo(platform_admin_token: str, async_client):
        resp = await async_client.get("/api/v1/platform/tenants",
                                      headers={"Authorization": f"Bearer {platform_admin_token}"})

Patching verify_token for pure unit tests::

    def test_auth(monkeypatch):
        monkeypatch.setattr("app.auth.jwks.verify_token", lambda t: make_platform_admin_payload())
"""

import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Generate a test RSA key pair once for the entire test session.
# Never use in production — key lives only in memory during tests.
# ---------------------------------------------------------------------------
_TEST_RSA_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
)
_TEST_RSA_PRIVATE_PEM = _TEST_RSA_PRIVATE_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

_TEST_KID = "test-key-001"
_TEST_ISS = "https://api.dev.iam.99technologies.com"  # matches IAM_BASE_URL in test config
_TEST_PLATFORM_ORG_ID = "org_99technologies"

TEST_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://itsm:itsm@localhost:5432/itsm_test",
)


def _sign(payload: dict[str, Any]) -> str:
    """Sign *payload* with the test RSA key, adding standard JWT headers."""
    now = int(time.time())
    payload.setdefault("iat", now)
    payload.setdefault("exp", now + 3600)
    payload.setdefault("iss", _TEST_ISS)
    payload.setdefault("aud", "itsm")
    return jwt.encode(
        payload,
        _TEST_RSA_PRIVATE_PEM,
        algorithm="RS256",
        headers={"kid": _TEST_KID},
    )


# ---------------------------------------------------------------------------
# Platform-tier token factories
# ---------------------------------------------------------------------------


def make_platform_admin_token() -> str:
    """JWT for a 99Technologies platform admin (IAM role: admin)."""
    return _sign(
        {
            "sub": f"usr_99t_admin_{uuid4().hex[:8]}",
            "org_id": _TEST_PLATFORM_ORG_ID,
            "roles": ["admin"],
            "products": [],
            "email": "platform-admin@99technologies.com",
            "given_name": "Platform",
            "family_name": "Admin",
        }
    )


def make_platform_support_token() -> str:
    """JWT for a 99Technologies platform support user (IAM role: operator)."""
    return _sign(
        {
            "sub": f"usr_99t_support_{uuid4().hex[:8]}",
            "org_id": _TEST_PLATFORM_ORG_ID,
            "roles": ["operator"],
            "products": [],
            "email": "platform-support@99technologies.com",
            "given_name": "Platform",
            "family_name": "Support",
        }
    )


# ---------------------------------------------------------------------------
# Tenant-tier token factories
# ---------------------------------------------------------------------------


def make_tenant_admin_token(org_id: str) -> str:
    """JWT for a customer tenant admin (IAM role: admin, product: itsm)."""
    return _sign(
        {
            "sub": f"usr_admin_{uuid4().hex[:8]}",
            "org_id": org_id,
            "roles": ["admin"],
            "products": ["itsm"],
            "email": f"admin@{org_id}.example.com",
            "given_name": "Tenant",
            "family_name": "Admin",
        }
    )


def make_agent_token(org_id: str) -> str:
    """JWT for a tenant agent/operator (IAM role: operator, product: itsm)."""
    return _sign(
        {
            "sub": f"usr_agent_{uuid4().hex[:8]}",
            "org_id": org_id,
            "roles": ["operator"],
            "products": ["itsm"],
            "email": f"agent@{org_id}.example.com",
            "given_name": "Agent",
            "family_name": "User",
        }
    )


def make_end_user_token(org_id: str) -> str:
    """JWT for an end user (IAM role: user, product: itsm)."""
    return _sign(
        {
            "sub": f"usr_end_{uuid4().hex[:8]}",
            "org_id": org_id,
            "roles": ["user"],
            "products": ["itsm"],
            "email": f"user@{org_id}.example.com",
            "given_name": "End",
            "family_name": "User",
        }
    )


def make_bot_token(org_id: str) -> str:
    """JWT for a bot / virtual-agent service account (IAM role: bot, product: itsm)."""
    return _sign(
        {
            "sub": f"bot_va_{uuid4().hex[:8]}",
            "org_id": org_id,
            "roles": ["bot"],
            "products": ["itsm"],
            "email": f"bot@{org_id}.example.com",
            "given_name": "Bot",
            "family_name": "Agent",
        }
    )


# ---------------------------------------------------------------------------
# Payload-only helpers (useful when patching verify_token directly)
# ---------------------------------------------------------------------------


def make_platform_admin_payload() -> dict:
    """Return a decoded payload dict matching make_platform_admin_token()."""
    now = int(time.time())
    return {
        "sub": f"usr_99t_admin_{uuid4().hex[:8]}",
        "org_id": _TEST_PLATFORM_ORG_ID,
        "roles": ["admin"],
        "products": [],
        "email": "platform-admin@99technologies.com",
        "given_name": "Platform",
        "family_name": "Admin",
        "iat": now,
        "exp": now + 3600,
        "iss": _TEST_ISS,
        "aud": "itsm",
    }


def make_tenant_admin_payload(org_id: str) -> dict:
    now = int(time.time())
    return {
        "sub": f"usr_admin_{uuid4().hex[:8]}",
        "org_id": org_id,
        "roles": ["admin"],
        "products": ["itsm"],
        "email": f"admin@{org_id}.example.com",
        "given_name": "Tenant",
        "family_name": "Admin",
        "iat": now,
        "exp": now + 3600,
        "iss": _TEST_ISS,
        "aud": "itsm",
    }


def make_agent_payload(org_id: str) -> dict:
    now = int(time.time())
    return {
        "sub": f"usr_agent_{uuid4().hex[:8]}",
        "org_id": org_id,
        "roles": ["operator"],
        "products": ["itsm"],
        "email": f"agent@{org_id}.example.com",
        "given_name": "Agent",
        "family_name": "User",
        "iat": now,
        "exp": now + 3600,
        "iss": _TEST_ISS,
        "aud": "itsm",
    }


def make_end_user_payload(org_id: str) -> dict:
    now = int(time.time())
    return {
        "sub": f"usr_end_{uuid4().hex[:8]}",
        "org_id": org_id,
        "roles": ["user"],
        "products": ["itsm"],
        "email": f"user@{org_id}.example.com",
        "given_name": "End",
        "family_name": "User",
        "iat": now,
        "exp": now + 3600,
        "iss": _TEST_ISS,
        "aud": "itsm",
    }


# ---------------------------------------------------------------------------
# pytest fixtures — token strings
# ---------------------------------------------------------------------------


@pytest.fixture
def platform_admin_token() -> str:
    return make_platform_admin_token()


@pytest.fixture
def platform_support_token() -> str:
    return make_platform_support_token()


@pytest.fixture
def test_org_id() -> str:
    """A deterministic fake org_id for use across related fixtures."""
    return "org_test_acme_01"


@pytest.fixture
def tenant_admin_token(test_org_id: str) -> str:
    return make_tenant_admin_token(test_org_id)


@pytest.fixture
def agent_token(test_org_id: str) -> str:
    return make_agent_token(test_org_id)


@pytest.fixture
def end_user_token(test_org_id: str) -> str:
    return make_end_user_token(test_org_id)


@pytest.fixture
def bot_token(test_org_id: str) -> str:
    return make_bot_token(test_org_id)


# ---------------------------------------------------------------------------
# DB engine + session (transaction rollback per test)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def engine():
    _engine = create_async_engine(
        TEST_DATABASE_URL, echo=False, pool_pre_ping=True
    )
    yield _engine
    await _engine.dispose()


@pytest_asyncio.fixture
async def db(engine):  # noqa: F811
    """Each test gets a DB session wrapped in a connection that rolls back on teardown."""
    async with engine.connect() as conn:
        await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await conn.rollback()


# ---------------------------------------------------------------------------
# Test environment: patches module-level globals once per test function
# ---------------------------------------------------------------------------


def _make_mock_redis() -> AsyncMock:
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.incr = AsyncMock(return_value=1)
    mock_redis.expire = AsyncMock(return_value=True)
    mock_redis.delete = AsyncMock(return_value=1)
    mock_pipeline = MagicMock()
    mock_pipeline.incr = MagicMock(return_value=mock_pipeline)
    mock_pipeline.ttl = MagicMock(return_value=mock_pipeline)
    mock_pipeline.execute = AsyncMock(return_value=[1, 70])
    mock_redis.pipeline = MagicMock(return_value=mock_pipeline)
    return mock_redis


@pytest_asyncio.fixture
async def _test_env(engine, db):
    """Patch module-level engine + redis globals for one test.

    All async clients in the same test depend on this fixture so the globals
    are patched exactly once and restored when the test ends.
    """
    import app.database as _db_mod
    import app.redis_client as _redis_mod

    mock_redis = _make_mock_redis()

    _orig_engine = _db_mod.engine
    _orig_session_factory = _db_mod.AsyncSessionLocal
    _orig_redis = _redis_mod.redis_client
    _db_mod.engine = engine
    _db_mod.AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    _redis_mod.redis_client = mock_redis

    try:
        yield {"mock_redis": mock_redis, "db": db}
    finally:
        _db_mod.engine = _orig_engine
        _db_mod.AsyncSessionLocal = _orig_session_factory
        _redis_mod.redis_client = _orig_redis


# ---------------------------------------------------------------------------
# Low-level client builder — reused by every role fixture
# ---------------------------------------------------------------------------


async def _build_client(
    env: dict,
    current_user_override=None,
    auth_header: str = "Bearer fake-token",
):
    """Create an independent AsyncClient + FastAPI app sharing the test DB session.

    Module-level globals must already be patched by ``_test_env`` before calling.
    """
    import unittest.mock as _mock

    from app.database import get_db
    from app.main import create_app
    from app.redis_client import get_redis

    mock_redis = env["mock_redis"]
    db = env["db"]

    app = create_app()

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_redis] = lambda: mock_redis

    if current_user_override is not None:
        from app.auth.dependencies import get_current_user
        app.dependency_overrides[get_current_user] = current_user_override

    with _mock.patch("app.main.refresh_jwks", new=AsyncMock(return_value=None)):
        with _mock.patch("app.main._jwks_refresh_loop", new=AsyncMock(return_value=None)):
            client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            await client.__aenter__()
            client.headers.update({"Authorization": auth_header})
            client.app = app  # type: ignore[attr-defined]
            return client


# ---------------------------------------------------------------------------
# Async HTTP client fixture (no role pre-set — caller controls auth)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def async_client(_test_env):
    """httpx.AsyncClient with DB + Redis overridden. No role set by default."""
    client = await _build_client(_test_env)
    try:
        yield client
    finally:
        await client.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# verify_token patch fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_verify_token(monkeypatch):
    """Returns a setter: call set_payload(payload_dict) to control what verify_token returns."""
    _payload = {}

    def _verify(token: str) -> dict:
        return _payload

    monkeypatch.setattr("app.auth.jwks.verify_token", _verify)

    class Setter:
        def set(self, payload: dict):
            _payload.clear()
            _payload.update(payload)

    return Setter()


# ---------------------------------------------------------------------------
# CurrentUser override fixture  — bypasses all DB lookups in get_current_user
# ---------------------------------------------------------------------------


def _make_current_user_override(tenant_id: UUID, local_user_id: UUID, roles: list[str], org_id: str):
    """Return a FastAPI dependency that yields a fully-populated CurrentUser."""
    from app.auth.dependencies import CurrentUser

    async def _override():
        return CurrentUser(
            iam_user_id=f"usr_test_{uuid4().hex[:8]}",
            org_id=org_id,
            email=f"test@{org_id}.example.com",
            roles=roles,
            tier="tenant",
            platform_role=None,
            tenant_id=tenant_id,
            local_user_id=local_user_id,
            is_active=True,
        )

    return _override


def _make_platform_user_override(tenant_id: UUID | None = None):
    """CurrentUser dependency for a 99T platform admin (tier=platform).

    tenant_id None = "all tenants" view → reference-data writes are global.
    """
    from app.auth.dependencies import CurrentUser

    async def _override():
        return CurrentUser(
            iam_user_id="usr_platform_test",
            org_id="org_99technologies",
            email="platform-admin@99technologies.com",
            roles=["admin"],
            tier="platform",
            platform_role="platform_admin",
            tenant_id=tenant_id,
            local_user_id=None,
            is_active=True,
        )

    return _override


# ---------------------------------------------------------------------------
# Shared tenant/user IDs used across role fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_tenant_id() -> UUID:
    """Stable UUID representing the test tenant (seeded via _seed_tenant)."""
    return UUID("10000000-0000-0000-0000-000000000001")


@pytest.fixture
def test_agent_user_id() -> UUID:
    return UUID("20000000-0000-0000-0000-000000000002")


@pytest.fixture
def test_admin_user_id() -> UUID:
    return UUID("20000000-0000-0000-0000-000000000003")


@pytest.fixture
def test_end_user_id() -> UUID:
    return UUID("20000000-0000-0000-0000-000000000004")


# ---------------------------------------------------------------------------
# DB seed helper: insert Tenant + User rows so FK constraints are satisfied
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seeded_tenant(db, test_org_id, test_tenant_id, test_agent_user_id, test_admin_user_id, test_end_user_id):
    """Insert minimal Tenant and User rows needed for endpoint tests."""
    from sqlalchemy import text

    # Tenant row
    await db.execute(
        text(
            """
            INSERT INTO tenants (id, iam_org_id, name, slug, is_active, settings)
            VALUES (:id, :iam_org_id, :name, :slug, true, '{}')
            ON CONFLICT (iam_org_id) DO NOTHING
            """
        ),
        {
            "id": str(test_tenant_id),
            "iam_org_id": test_org_id,
            "name": "Acme Corp",
            "slug": "acme-corp-test",
        },
    )

    # Also seed a tenant_sequences row for ticket numbering
    for prefix in ("INC", "REQ", "PRB", "CHG", "AST"):
        await db.execute(
            text(
                """
                INSERT INTO tenant_sequences (tenant_id, prefix, last_value)
                VALUES (:tenant_id, :prefix, 0)
                ON CONFLICT DO NOTHING
                """
            ),
            {"tenant_id": str(test_tenant_id), "prefix": prefix},
        )

    # Agent user
    await db.execute(
        text(
            """
            INSERT INTO users (id, iam_user_id, tenant_id, email, first_name, last_name, is_active)
            VALUES (:id, :iam_user_id, :tenant_id, :email, 'Agent', 'Test', true)
            ON CONFLICT (iam_user_id, tenant_id) DO NOTHING
            """
        ),
        {
            "id": str(test_agent_user_id),
            "iam_user_id": "iam_agent_fixed",
            "tenant_id": str(test_tenant_id),
            "email": "agent@acme.example.com",
        },
    )

    # Admin user
    await db.execute(
        text(
            """
            INSERT INTO users (id, iam_user_id, tenant_id, email, first_name, last_name, is_active)
            VALUES (:id, :iam_user_id, :tenant_id, :email, 'Admin', 'Test', true)
            ON CONFLICT (iam_user_id, tenant_id) DO NOTHING
            """
        ),
        {
            "id": str(test_admin_user_id),
            "iam_user_id": "iam_admin_fixed",
            "tenant_id": str(test_tenant_id),
            "email": "admin@acme.example.com",
        },
    )

    # End user
    await db.execute(
        text(
            """
            INSERT INTO users (id, iam_user_id, tenant_id, email, first_name, last_name, is_active)
            VALUES (:id, :iam_user_id, :tenant_id, :email, 'EndUser', 'Test', true)
            ON CONFLICT (iam_user_id, tenant_id) DO NOTHING
            """
        ),
        {
            "id": str(test_end_user_id),
            "iam_user_id": "iam_enduser_fixed",
            "tenant_id": str(test_tenant_id),
            "email": "enduser@acme.example.com",
        },
    )

    await db.flush()
    return test_tenant_id


# ---------------------------------------------------------------------------
# Asset reference data — needed by tests that create Asset records
# ---------------------------------------------------------------------------

_TEST_ASSET_CATEGORY_ID = UUID("30000000-0000-0000-0000-000000000001")
_TEST_ASSET_TYPE_ID = UUID("30000000-0000-0000-0000-000000000002")


@pytest.fixture
def test_asset_type_id() -> UUID:
    return _TEST_ASSET_TYPE_ID


@pytest_asyncio.fixture
async def seeded_asset_type(db, seeded_tenant, test_tenant_id):
    """Seed one AssetCategory + AssetType for tests that create Asset records."""
    from sqlalchemy import text

    await db.execute(
        text(
            """
            INSERT INTO asset_categories (id, tenant_id, name, is_active)
            VALUES (:id, :tenant_id, 'Hardware', true)
            ON CONFLICT DO NOTHING
            """
        ),
        {"id": str(_TEST_ASSET_CATEGORY_ID), "tenant_id": str(test_tenant_id)},
    )
    await db.execute(
        text(
            """
            INSERT INTO asset_types (id, tenant_id, category_id, name, custom_fields_schema)
            VALUES (:id, :tenant_id, :category_id, 'Laptop', '{}')
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "id": str(_TEST_ASSET_TYPE_ID),
            "tenant_id": str(test_tenant_id),
            "category_id": str(_TEST_ASSET_CATEGORY_ID),
        },
    )
    await db.flush()
    return _TEST_ASSET_TYPE_ID


# ---------------------------------------------------------------------------
# Role-specific HTTP client fixtures
# Each creates its OWN independent AsyncClient so tests that request multiple
# role clients don't overwrite each other's dependency_overrides.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tenant_admin_client(_test_env, seeded_tenant, test_org_id, test_tenant_id, test_admin_user_id):
    """Independent AsyncClient with tenant admin identity."""
    client = await _build_client(
        _test_env,
        current_user_override=_make_current_user_override(
            tenant_id=test_tenant_id,
            local_user_id=test_admin_user_id,
            roles=["admin"],
            org_id=test_org_id,
        ),
        auth_header="Bearer fake-tenant-admin-token",
    )
    try:
        yield client
    finally:
        await client.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def agent_client(_test_env, seeded_tenant, test_org_id, test_tenant_id, test_agent_user_id):
    """Independent AsyncClient with agent identity."""
    client = await _build_client(
        _test_env,
        current_user_override=_make_current_user_override(
            tenant_id=test_tenant_id,
            local_user_id=test_agent_user_id,
            roles=["agent"],
            org_id=test_org_id,
        ),
        auth_header="Bearer fake-agent-token",
    )
    try:
        yield client
    finally:
        await client.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def end_user_client(_test_env, seeded_tenant, test_org_id, test_tenant_id, test_end_user_id):
    """Independent AsyncClient with end-user identity."""
    client = await _build_client(
        _test_env,
        current_user_override=_make_current_user_override(
            tenant_id=test_tenant_id,
            local_user_id=test_end_user_id,
            roles=["end_user"],
            org_id=test_org_id,
        ),
        auth_header="Bearer fake-end-user-token",
    )
    try:
        yield client
    finally:
        await client.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def platform_admin_client(_test_env, seeded_tenant):
    """Independent AsyncClient for a 99T platform admin in the all-tenants view
    (tier=platform, no tenant selected) — reference-data writes are global."""
    client = await _build_client(
        _test_env,
        current_user_override=_make_platform_user_override(tenant_id=None),
        auth_header="Bearer fake-platform-admin-token",
    )
    try:
        yield client
    finally:
        await client.__aexit__(None, None, None)
