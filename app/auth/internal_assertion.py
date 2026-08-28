"""
Internal service assertion — a narrow, second, EXPLICITLY SEPARATE trust
path for Project IQ's WhatsApp integration to authenticate as a specific,
already-known user without a live Keycloak-issued token.

Why this is its own module rather than a change inside app/auth/jwks.py:
that file's verify_token() carries a deliberate, documented, tested
guarantee — "RS256 only — symmetric algorithms rejected." This module signs
with HS256 by design, so it must never live inside that function. Instead,
app/auth/dependencies.py:get_current_user() tries this FIRST and falls
through to the completely untouched verify_token() for anything that isn't
unambiguously this shape — every existing caller (web portal, mobile app,
itsm-service's own UI, any real Keycloak token) hits the exact same code
path as before this file existed.

Two independent discriminators make it safe to try first without risk of
ever misidentifying a real token:
  1. alg == "HS256" — every genuine Keycloak-issued token is RS256.
  2. iss == "project-iq-whatsapp" — a claim value no real IAM token carries.
Failing either check returns None immediately; only a token that is BOTH
HS256-signed AND claims this exact issuer proceeds to signature
verification, and even then only against a secret that must be explicitly
configured (blank = disabled).

Background: Project-IQ-V2's actual first choice was Keycloak's own Standard
Token Exchange (RFC 8693) to mint a genuine Keycloak-signed token for the
target user — architecturally the right answer, needs zero itsm-service
change, since the result is indistinguishable from a normal login token.
That needs Keycloak's legacy Token Exchange feature (`--features=
token-exchange`, a server restart) plus an impersonation permission grant,
which this deployment's IAM admin access doesn't currently extend to. This
module is the fallback that doesn't depend on that access at all — see
Project-IQ-V2's docs/plans/WHATSAPP_INTEGRATION_PLAN.md 2026-08-27 note for
the full history.
"""

import logging
from typing import Optional

from jose import JWTError, jwt

from app.config import get_settings

logger = logging.getLogger(__name__)

_EXPECTED_ISSUER = "project-iq-whatsapp"
_EXPECTED_AUDIENCE = "itsm-service"


def try_verify_internal_assertion(token: str) -> Optional[dict]:
    """Return the decoded payload if `token` is a valid internal WhatsApp
    assertion; None for absolutely everything else, including malformed
    tokens — never raises, so callers always safely fall through to normal
    Keycloak verification (app/auth/jwks.py:verify_token) on any doubt."""
    settings = get_settings()
    secret = settings.WHATSAPP_INTERNAL_ASSERTION_SECRET
    if not secret:
        return None

    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        return None
    if header.get("alg") != "HS256":
        return None  # real Keycloak tokens are always RS256 — cheap, safe discriminator

    try:
        unverified_claims = jwt.get_unverified_claims(token)
    except JWTError:
        return None
    if unverified_claims.get("iss") != _EXPECTED_ISSUER:
        return None  # not this shape at all — let it fall through untouched

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=_EXPECTED_AUDIENCE,
            issuer=_EXPECTED_ISSUER,
            options={
                "require_exp": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
                "leeway": 10,
            },
        )
    except JWTError as exc:
        logger.warning("internal_assertion_rejected", extra={"error": str(exc)})
        return None

    if not payload.get("sub") or not payload.get("org_id"):
        logger.warning("internal_assertion_missing_required_claims")
        return None
    return payload
