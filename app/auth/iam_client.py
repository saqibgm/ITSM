"""
Async httpx client for the IAM internal API.

Used by background sync tasks (Celery) and the tenant-provisioning webhook handler.
All calls use a Bearer token obtained via the service-account credentials.

Never log token values or client secrets.
"""

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.exceptions import ExternalServiceError

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 10.0


async def _get_service_token() -> str:
    """Obtain a service-account Bearer token from IAM using client credentials.

    Stub implementation — returns a cached/placeholder token.
    The real implementation will call IAM's OAuth2 token endpoint using
    settings.IAM_CLIENT_ID and settings.IAM_CLIENT_SECRET (client_credentials
    grant) and cache the result until near-expiry.
    """
    # TODO(S0.3): Implement client_credentials token exchange with token caching.
    # For now, return empty string so callers can be wired up before IAM is live.
    settings = get_settings()
    logger.debug("iam_service_token_stub", extra={"client_id": settings.IAM_CLIENT_ID})
    return ""


class IAMClient:
    """Lightweight async wrapper around the IAM REST API."""

    def __init__(self) -> None:
        self.base_url = get_settings().IAM_BASE_URL

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute a request, raise ExternalServiceError on connectivity problems."""
        token = await _get_service_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        url = self.base_url.rstrip("/") + path
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException:
            logger.error(
                "iam_request_timeout",
                extra={"method": method, "path": path},
            )
            raise ExternalServiceError("iam")
        except httpx.ConnectError:
            logger.error(
                "iam_connection_error",
                extra={"method": method, "path": path},
            )
            raise ExternalServiceError("iam")
        except httpx.HTTPStatusError as exc:
            logger.error(
                "iam_http_error",
                extra={"method": method, "path": path, "status": exc.response.status_code},
            )
            raise ExternalServiceError("iam")

    async def get_org_users(self, org_id: str) -> list[dict]:
        """GET /v1/user?organizationId={org_id}

        Returns the list of IAM user objects belonging to the given organisation.
        """
        data = await self._request("GET", "/v1/user", params={"organizationId": org_id})
        # IAM may return a list directly or wrap it; normalise both shapes.
        if isinstance(data, list):
            return data
        return data.get("users", data.get("data", []))

    async def get_org(self, org_id: str) -> dict:
        """GET /v1/organization/{org_id}

        Returns the IAM organisation object.
        """
        return await self._request("GET", f"/v1/organization/{org_id}")
