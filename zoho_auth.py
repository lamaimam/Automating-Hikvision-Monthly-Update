"""
Zoho OAuth access-token manager.

Zoho access tokens expire after 1 hour. We use the long-lived refresh_token
to mint new access tokens on demand and cache them in memory for the rest
of the run.
"""
import logging
import time
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)


class ZohoAuth:
    """Singleton-style access token cache. Reuses one token across all clients."""

    _access_token: Optional[str] = None
    _expires_at: float = 0.0

    @classmethod
    def get_token(cls) -> str:
        """Return a valid access token, refreshing if needed."""
        # Refresh 60s before actual expiry to avoid edge-of-expiry 401s.
        if cls._access_token and time.time() < cls._expires_at - 60:
            return cls._access_token

        log.info("Refreshing Zoho access token...")
        resp = requests.post(
            f"{config.ZOHO_ACCOUNTS_BASE}/oauth/v2/token",
            data={
                "refresh_token": config.ZOHO_REFRESH_TOKEN,
                "client_id": config.ZOHO_CLIENT_ID,
                "client_secret": config.ZOHO_CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if "access_token" not in data:
            raise RuntimeError(f"Zoho token refresh failed: {data}")

        cls._access_token = data["access_token"]
        cls._expires_at = time.time() + data.get("expires_in", 3600)
        log.info("Got new Zoho access token (expires in %ds)", data.get("expires_in", 3600))
        return cls._access_token

    @classmethod
    def auth_header(cls) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {cls.get_token()}"}
