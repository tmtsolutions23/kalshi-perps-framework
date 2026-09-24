"""
Kalshi Perps API Authentication.
Mirrors the existing RSA auth from the predictions API, adapted for /margin endpoints.
"""

import json
import time
import base64
import logging
from pathlib import Path
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

import requests

log = logging.getLogger(__name__)


class KalshiAuth:
    """Handles RSA-PSS signing for Kalshi perps API (/margin/*) requests."""

    def __init__(self, key_config_path: str, private_key_path: str, api_base: str):
        self.api_base = api_base.rstrip("/")
        # Full API prefix (/trade-api/v2/margin) — used for request signing
        # which requires the FULL path, not the shortened endpoint path.
        self.full_prefix = "/trade-api/v2/margin"
        with open(key_config_path) as f:
            cfg = json.load(f)
        self.key_id = cfg["key_id"]

        with open(private_key_path, "rb") as f:
            self._private_key = load_pem_private_key(f.read(), password=None)

    def _full_path(self, path: str) -> str:
        """Expand a short endpoint path to the full signed path."""
        if path.startswith("/trade-api"):
            return path
        return f"{self.full_prefix}{path}"

    def sign_headers(self, method: str, path: str) -> dict:
        """
        Produce the three Kalshi auth headers for a given (method, path).
        path may be short ('/enabled') or full ('/trade-api/v2/margin/enabled').
        The signature is computed over the FULL path (without query string).
        """
        full_path = self._full_path(path)
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        msg = f"{ts_ms}{method}{full_path}".encode()
        sig = base64.b64encode(
            self._private_key.sign(
                msg,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        ).decode()
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
            "KALSHI-ACCESS-SIGNATURE": sig,
        }

    def api_url(self, path: str) -> str:
        """Full URL: {api_base}{path} (path is endpoint-short, e.g. '/markets/KXBTCPERP')."""
        return f"{self.api_base}{path}"

    def signed_get(self, path: str, **params) -> dict:
        """Authenticated GET request."""
        headers = self.sign_headers("GET", path)
        r = requests.get(self.api_url(path), headers=headers, params=params or None, timeout=15)
        if r.status_code == 401:
            log.error("Kalshi auth failure (401) on GET %s — may need fresh credentials", path)
        r.raise_for_status()
        return r.json()

    def signed_post(self, path: str, body: dict) -> dict:
        """Authenticated POST request."""
        headers = self.sign_headers("POST", path)
        headers["Content-Type"] = "application/json"
        r = requests.post(self.api_url(path), headers=headers, json=body, timeout=15)
        if r.status_code == 401:
            log.error("Kalshi auth failure (401) on POST %s", path)
        r.raise_for_status()
        return r.json()

    def public_get(self, path: str, **params) -> dict:
        """Unauthenticated GET (market data, orderbook)."""
        r = requests.get(self.api_url(path), params=params or None, timeout=15)
        r.raise_for_status()
        return r.json()

    def check_enabled(self) -> bool:
        """Check if perps trading is enabled for this account."""
        try:
            resp = self.signed_get("/enabled")
            return resp.get("enabled", False)
        except Exception as e:
            log.warning("Could not check perps enabled status: %s", e)
            return False