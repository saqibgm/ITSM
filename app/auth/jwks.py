"""
JWKS cache and JWT verification for IQ-ITSM.

Rules enforced here:
- Never log the token value.
- RS256 only — symmetric algorithms rejected.
- On unknown kid: refresh JWKS once and retry (handles key rotation).
"""

import asyncio
import json
import logging
import os
from typing import Any

import httpx
from jose import JWTError, jwt
from jose.backends import RSAKey

from app.config import get_settings
from app.exceptions import AuthenticationError

logger = logging.getLogger(__name__)

# In-memory key store: kid → jose RSAKey (public, RS256)
_jwks_cache: dict[str, Any] = {}

# How often to refresh keys in the background (seconds)
_JWKS_REFRESH_INTERVAL_SECONDS = 6 * 3600  # 6 hours

# A last-known-good snapshot of IAM's signing keys, bundled with the app.
# Realm signing keys rotate rarely (a deliberate admin action), so a
# stale-but-valid snapshot is far more useful than an outright auth outage
# when this host can reach IAM through neither the proxy nor directly.
# Live network paths are always tried first — this is a safety net, not a
# soft dependency. Refresh the file whenever keys actually rotate.
_JWKS_FALLBACK_FILE = os.path.join(os.path.dirname(__file__), "jwks_fallback.json")


async def _fetch_jwks(jwks_url: str, proxy: str | None) -> dict:
    """GET the JWKS document, preferring *proxy* but falling back to a direct
    connection if the proxy is unreachable, then to the bundled fallback
    snapshot if neither network path works at all.
    """
    errors = []
    if proxy:
        try:
            async with httpx.AsyncClient(timeout=10.0, proxy=proxy) as client:
                response = await client.get(jwks_url)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            errors.append(f"proxy: {e}")
            logger.warning("jwks_proxy_fetch_failed_falling_back_direct", exc_info=True)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(jwks_url)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        errors.append(f"direct: {e}")
        logger.warning("jwks_direct_fetch_failed_falling_back_to_bundled_snapshot", exc_info=True)

    with open(_JWKS_FALLBACK_FILE, encoding="utf-8") as f:
        return json.load(f)


async def refresh_jwks() -> None:
    """Fetch JWKS from IAM and rebuild the in-memory key cache.

    Safe to call concurrently — replaces the cache dict atomically.
    Never raises; logs ERROR on failure (stale cache continues to serve).
    """
    settings = get_settings()
    try:
        jwks = await _fetch_jwks(settings.IAM_JWKS_URL, settings.IAM_JWKS_PROXY)

        new_cache: dict[str, Any] = {}
        for key_data in jwks.get("keys", []):
            kid = key_data.get("kid")
            if not kid:
                continue
            if key_data.get("kty") != "RSA":
                # Only RS256 is accepted — skip non-RSA keys
                continue
            try:
                new_cache[kid] = RSAKey(key_data, algorithm="RS256")
            except Exception:
                logger.warning(
                    "jwks_key_parse_failed",
                    extra={"kid": kid},
                    exc_info=True,
                )

        # Atomic replace
        _jwks_cache.clear()
        _jwks_cache.update(new_cache)
        logger.info("jwks_refreshed", extra={"key_count": len(_jwks_cache)})

    except Exception:
        logger.error(
            "jwks_refresh_failed",
            extra={"jwks_url": settings.IAM_JWKS_URL},
            exc_info=True,
        )


async def _jwks_refresh_loop() -> None:
    """Background task: refresh JWKS every 6 hours indefinitely."""
    while True:
        await asyncio.sleep(_JWKS_REFRESH_INTERVAL_SECONDS)
        await refresh_jwks()


def _get_rsa_key(kid: str) -> Any:
    """Return the cached RSA key for *kid*, or None if not found."""
    return _jwks_cache.get(kid)


def verify_token(token: str) -> dict:
    """Decode and verify an RS256 JWT issued by IAM.

    Steps:
    1. Extract ``kid`` from the unverified header.
    2. Look up cached key; if missing, refresh JWKS once and retry.
    3. Verify signature, expiry, issuer, and audience.
    4. Return the decoded payload dict.

    Raises:
        AuthenticationError: on any validation failure. The token value is
            never logged or included in the exception message.
    """
    settings = get_settings()

    # Step 1: read header without verification to get the kid
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        raise AuthenticationError("Invalid token format")

    alg = header.get("alg", "")
    if alg != "RS256":
        raise AuthenticationError(f"Unsupported algorithm: {alg}")

    kid = header.get("kid")
    if not kid:
        raise AuthenticationError("Token header missing 'kid'")

    # Step 2: look up the key
    rsa_key = _get_rsa_key(kid)

    # Step 2a: unknown kid — refresh once and retry (handles key rotation)
    if rsa_key is None:
        logger.info("jwks_kid_not_found_refreshing", extra={"kid": kid})

        # We need to run the async refresh synchronously from this sync context.
        # Use asyncio.get_event_loop().run_until_complete only if no loop is
        # running; if a loop is running we cannot block here — raise and let
        # callers use the async refresh path in the lifespan.
        #
        # In practice verify_token is always called inside a running async loop
        # (FastAPI request handling), so we schedule the coroutine and wait.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Cannot block — create a future and wait with a short internal
                # mechanism. Use run_coroutine_threadsafe if possible; otherwise
                # raise immediately so the caller retries after the background
                # refresh completes.
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(refresh_jwks(), loop)
                try:
                    future.result(timeout=12)
                except concurrent.futures.TimeoutError:
                    logger.error("jwks_refresh_timeout_on_rotation")
            else:
                loop.run_until_complete(refresh_jwks())
        except Exception:
            logger.error("jwks_refresh_exception_on_rotation", exc_info=True)

        rsa_key = _get_rsa_key(kid)
        if rsa_key is None:
            raise AuthenticationError("Token signing key not found")

    # Step 3: decode and validate
    issuer = settings.IAM_ISSUER or settings.IAM_BASE_URL
    audience = settings.IAM_AUDIENCE or None
    verify_aud = bool(audience)
    try:
        payload = jwt.decode(
            token,
            rsa_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={
                "require_exp": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": verify_aud,
                # A few seconds of clock drift between this host and the IAM
                # server otherwise makes a freshly-minted token look "not yet
                # valid" — python-jose takes leeway inside `options`, not as
                # a top-level decode() kwarg (unlike PyJWT).
                "leeway": 10,
            },
        )
    except JWTError as exc:
        # Map jose errors to our AuthenticationError without exposing token value
        raise AuthenticationError(f"Token validation failed: {exc}") from exc

    return payload
