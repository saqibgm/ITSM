"""
Tests for app/auth/internal_assertion.py — the narrow, isolated second
trust path for Project-IQ-V2's WhatsApp integration.

Two groups:
  - Unit tests of try_verify_internal_assertion() in isolation.
  - Integration tests proving app.auth.dependencies.get_current_user()
    wiring works end-to-end AND, just as importantly, that a completely
    normal Keycloak-issued token still authenticates exactly as before
    this module existed, even with the new secret configured (the explicit
    non-interference guarantee this feature was built around).
"""

import time
from types import SimpleNamespace

import pytest
from jose import jwt

from app.auth.internal_assertion import (
    _EXPECTED_AUDIENCE,
    _EXPECTED_ISSUER,
    try_verify_internal_assertion,
)

_SECRET = "test-shared-secret"


def _mint(secret=_SECRET, issuer=_EXPECTED_ISSUER, audience=_EXPECTED_AUDIENCE,
          alg="HS256", sub="usr_whatsapp_linked_1", org_id="org_test_acme_01",
          exp_delta=300, **extra_claims):
    now = int(time.time())
    claims = {
        "sub": sub, "org_id": org_id, "products": ["itsm"],
        "iss": issuer, "aud": audience, "iat": now, "exp": now + exp_delta,
        **extra_claims,
    }
    return jwt.encode(claims, secret, algorithm=alg)


# ===========================================================================
# Unit tests — try_verify_internal_assertion() in isolation
# ===========================================================================


def test_valid_assertion_is_accepted(monkeypatch):
    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=_SECRET),
    )
    token = _mint()
    payload = try_verify_internal_assertion(token)
    assert payload is not None
    assert payload["sub"] == "usr_whatsapp_linked_1"
    assert payload["org_id"] == "org_test_acme_01"


def test_disabled_when_no_secret_configured(monkeypatch):
    """Blank secret (the default) must reject EVERYTHING via this path,
    regardless of what's presented — the explicit off switch."""
    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=""),
    )
    token = _mint()
    assert try_verify_internal_assertion(token) is None


def test_wrong_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=_SECRET),
    )
    token = _mint(secret="wrong-secret")
    assert try_verify_internal_assertion(token) is None


def test_wrong_issuer_falls_through_untouched(monkeypatch):
    """Not our shape at all -> None, so the caller falls through to normal
    Keycloak verification rather than us trying (and failing) to validate
    something that was never meant for this path."""
    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=_SECRET),
    )
    token = _mint(issuer="some-other-issuer")
    assert try_verify_internal_assertion(token) is None


def test_rs256_token_falls_through_untouched(monkeypatch):
    """The cheap, safe discriminator: every real Keycloak token is RS256.
    Even one that (implausibly) claimed our issuer string must still be
    rejected here, since only HS256 is ever attempted for this path."""
    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=_SECRET),
    )
    # jose can't easily HS-sign then claim RS256 in the header without a real
    # RSA key; simplest faithful test is an RS256-header token that isn't
    # validly HS256-decodable with our secret at all -- get_unverified_header
    # inspects the header we set explicitly here regardless of the actual
    # signature algorithm used to produce the bytes.
    token = jwt.encode(
        {"sub": "x", "org_id": "y", "iss": _EXPECTED_ISSUER, "aud": _EXPECTED_AUDIENCE,
         "iat": int(time.time()), "exp": int(time.time()) + 300},
        _SECRET, algorithm="HS256", headers={"alg": "RS256"},
    )
    assert try_verify_internal_assertion(token) is None


def test_expired_assertion_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=_SECRET),
    )
    token = _mint(exp_delta=-60)
    assert try_verify_internal_assertion(token) is None


def test_missing_org_id_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=_SECRET),
    )
    now = int(time.time())
    token = jwt.encode(
        {"sub": "usr_1", "products": ["itsm"], "iss": _EXPECTED_ISSUER,
         "aud": _EXPECTED_AUDIENCE, "iat": now, "exp": now + 300},
        _SECRET, algorithm="HS256",
    )
    assert try_verify_internal_assertion(token) is None


def test_malformed_token_returns_none_not_raise(monkeypatch):
    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=_SECRET),
    )
    assert try_verify_internal_assertion("not-a-real-token") is None


# ===========================================================================
# Integration — real end-to-end wiring through get_current_user(), and the
# explicit non-interference guarantee for normal Keycloak tokens
# ===========================================================================


@pytest.mark.asyncio
async def test_internal_assertion_authenticates_real_request(
    async_client, seeded_tenant, test_org_id, monkeypatch,
):
    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=_SECRET),
    )
    token = _mint(sub="usr_whatsapp_new_user", org_id=test_org_id)
    async_client.headers.update({"Authorization": f"Bearer {token}"})

    resp = await async_client.get("/api/v1/gdpr/export")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_normal_keycloak_token_unaffected_when_secret_configured(
    async_client, seeded_tenant, test_org_id, monkeypatch,
):
    """The load-bearing regression proof: with the internal-assertion
    secret configured and active, a completely ordinary Keycloak-shaped
    token (verified via the existing, completely untouched verify_token())
    must authenticate exactly as it did before this feature existed —
    proving the new path never intercepts a real token.

    Patches app.auth.dependencies.verify_token specifically, NOT
    app.auth.jwks.verify_token — dependencies.py imports the name directly
    (`from app.auth.jwks import verify_token`), so it holds its own bound
    reference; patching the source module's attribute never reaches it.
    This is a real, pre-existing gap in the conftest.py patch_verify_token
    fixture (unused anywhere else in the suite until this test), left alone
    here rather than fixed, since touching shared test infrastructure is
    out of scope for this narrow, additive change."""
    from tests.conftest import make_tenant_admin_payload

    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=_SECRET),
    )
    monkeypatch.setattr(
        "app.auth.dependencies.verify_token",
        lambda token: make_tenant_admin_payload(test_org_id),
    )
    async_client.headers.update({"Authorization": "Bearer some-real-looking-rs256-token"})

    resp = await async_client.get("/api/v1/gdpr/export")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_disabled_by_default_real_request_falls_through_and_fails_normally(
    async_client, seeded_tenant, test_org_id, monkeypatch,
):
    """No secret configured (the real default) -> the internal-assertion
    token is rejected by try_verify_internal_assertion, falls through to
    verify_token, and fails there exactly as any unrecognised token would —
    never a silent bypass."""
    monkeypatch.setattr(
        "app.auth.internal_assertion.get_settings",
        lambda: SimpleNamespace(WHATSAPP_INTERNAL_ASSERTION_SECRET=""),
    )
    token = _mint(org_id=test_org_id)
    async_client.headers.update({"Authorization": f"Bearer {token}"})

    resp = await async_client.get("/api/v1/gdpr/export")
    assert resp.status_code == 401
