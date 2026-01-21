"""
QuantConnect API Client

SHA256 timestamped authentication matching the TypeScript implementation.

This client routes requests to either:
- QuantConnect Cloud (https://www.quantconnect.com/api/v2) - using user's own credentials
- Self-hosted LEAN API (our built-in engine) - using internal service auth

The routing is determined by the user's `backtest_engine` setting:
- 'cloud': Use QuantConnect Cloud with user's credentials
- 'builtin': Use our self-hosted LEAN API

The APIs are 1:1 compatible, so we just swap the base URL and auth headers.
"""

import base64
import hashlib
import os
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx
import structlog

from .supabase_client import SupabaseClient

logger = structlog.getLogger(__name__)

# Self-hosted LEAN API URL (our built-in engine)
LEAN_API_URL = os.environ.get("LEAN_API_URL", "http://localhost:3001")

# Base URLs
QC_CLOUD_API_URL = "https://www.quantconnect.com/api/v2"
LEAN_API_BASE_URL = f"{LEAN_API_URL}/api/v2"

# Cache for user credentials (user_id -> {qc_user_id, api_token, fetched_at})
_user_credentials_cache: dict[str, dict[str, Any]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes

# Cache for user settings (user_id -> {backtest_engine, fetched_at})
_user_settings_cache: dict[str, dict[str, Any]] = {}
_SETTINGS_CACHE_TTL_SECONDS = 60  # 1 minute (settings can change more often)


class QCCredentialsNotConfiguredError(Exception):
    """Raised when user hasn't configured their QuantConnect credentials."""

    def __init__(self, user_id: str | None = None):
        self.user_id = user_id
        message = (
            "QuantConnect credentials not configured. "
            "Please add your QuantConnect API credentials in Settings → API Keys."
        )
        super().__init__(message)


def _decrypt_secret(encrypted_hex: str, iv_hex: str, auth_tag_hex: str) -> str:
    """Decrypt AES-256-GCM encrypted secret.

    Matches the TypeScript implementation in lib/crypto.ts
    """
    encryption_key_hex = os.environ.get("ENCRYPTION_KEY")
    if not encryption_key_hex:
        raise ValueError("ENCRYPTION_KEY not set in environment")

    if len(encryption_key_hex) != 64:
        raise ValueError("ENCRYPTION_KEY must be a 32-byte hex string (64 characters)")

    key = bytes.fromhex(encryption_key_hex)
    iv = bytes.fromhex(iv_hex)
    ciphertext = bytes.fromhex(encrypted_hex)
    auth_tag = bytes.fromhex(auth_tag_hex)

    # AES-GCM expects ciphertext + auth_tag concatenated
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(iv, ciphertext + auth_tag, None)

    return plaintext.decode('utf-8')


async def _fetch_user_settings(user_id: str) -> dict[str, str]:
    """Fetch user's settings from Supabase.

    Returns dict with 'backtest_engine' ('cloud' or 'builtin').
    Defaults to 'cloud' if not configured.
    """
    # Check cache first
    cached = _user_settings_cache.get(user_id)
    if cached and (time.time() - cached.get("fetched_at", 0)) < _SETTINGS_CACHE_TTL_SECONDS:
        return {"backtest_engine": cached["backtest_engine"]}

    try:
        client = SupabaseClient(use_service_role=True)
        rows = await client.select(
            "user_settings",
            {
                "user_id": f"eq.{user_id}",
                "select": "backtest_engine",
            },
        )

        if rows:
            backtest_engine = rows[0].get("backtest_engine", "cloud")
        else:
            backtest_engine = "cloud"  # Default

        # Cache the settings
        _user_settings_cache[user_id] = {
            "backtest_engine": backtest_engine,
            "fetched_at": time.time(),
        }

        return {"backtest_engine": backtest_engine}

    except Exception as e:
        logger.warning(
            "Failed to fetch user settings, defaulting to cloud",
            error=str(e),
            user_id=user_id,
        )
        return {"backtest_engine": "cloud"}


async def _fetch_user_qc_credentials(user_id: str) -> dict[str, str] | None:
    """Fetch user's QuantConnect credentials from Supabase.

    Returns dict with 'qc_user_id' and 'api_token' if found, None otherwise.
    """
    # Check cache first
    cached = _user_credentials_cache.get(user_id)
    if cached and (time.time() - cached.get("fetched_at", 0)) < _CACHE_TTL_SECONDS:
        return {
            "qc_user_id": cached["qc_user_id"],
            "api_token": cached["api_token"],
        }

    try:
        client = SupabaseClient(use_service_role=True)
        rows = await client.select(
            "user_api_keys",
            {
                "user_id": f"eq.{user_id}",
                "provider": "eq.quantconnect",
                "select": "key_id,encrypted_secret,iv,auth_tag",
            },
        )

        if not rows:
            logger.debug("No QC credentials found for user", user_id=user_id)
            return None

        row = rows[0]
        qc_user_id = row.get("key_id")
        encrypted_secret = row.get("encrypted_secret")
        iv = row.get("iv")
        auth_tag = row.get("auth_tag")

        if not all([qc_user_id, encrypted_secret, iv, auth_tag]):
            logger.warning("Incomplete QC credentials for user", user_id=user_id)
            return None

        # Decrypt the API token
        api_token = _decrypt_secret(encrypted_secret, iv, auth_tag)

        # Cache the credentials
        _user_credentials_cache[user_id] = {
            "qc_user_id": qc_user_id,
            "api_token": api_token,
            "fetched_at": time.time(),
        }

        logger.info(
            "Fetched user QC credentials", user_id=user_id, qc_user_id=qc_user_id
        )
        return {
            "qc_user_id": qc_user_id,
            "api_token": api_token,
        }

    except Exception as e:
        logger.warning(
            "Failed to fetch user QC credentials", error=str(e), user_id=user_id
        )
        return None


def _generate_qc_auth_headers(qc_user_id: str, api_token: str) -> dict[str, str]:
    """Generate QC-style auth headers (SHA256 timestamped token)."""
    timestamp = int(time.time())
    timestamped_token = f"{api_token}:{timestamp}"
    hashed_token = hashlib.sha256(timestamped_token.encode()).hexdigest()
    authentication = f"{qc_user_id}:{hashed_token}"
    auth_header = f"Basic {base64.b64encode(authentication.encode()).decode()}"

    return {
        "Authorization": auth_header,
        "Timestamp": str(timestamp),
        "Content-Type": "application/json",
    }


async def qc_request(
    endpoint: str,
    payload: dict[str, Any] | None = None,
    method: str = "POST",
    user_id: str | None = None,
) -> Any:
    """Make authenticated request to QuantConnect-compatible API.

    Routes to either:
    - QC Cloud: Uses user's own QC credentials (when backtest_engine='cloud')
    - Self-hosted LEAN: Uses internal service auth (when backtest_engine='builtin')

    The routing is determined by the user's settings, not an environment variable.
    The APIs are 1:1 compatible - we just swap URL and auth.

    Args:
        endpoint: API endpoint path (e.g., "/files/read")
        payload: Request body as dict
        method: HTTP method (default POST)
        user_id: User ID to fetch credentials/settings for (REQUIRED)

    Raises:
        QCCredentialsNotConfiguredError: If user hasn't configured QC credentials (cloud mode)
    """
    if not user_id:
        raise QCCredentialsNotConfiguredError()

    # Fetch user's engine preference
    settings = await _fetch_user_settings(user_id)
    use_builtin = settings.get("backtest_engine") == "builtin"

    if use_builtin:
        # Self-hosted LEAN - use internal service auth
        internal_secret = os.environ.get("INTERNAL_SERVICE_SECRET", "")
        headers = {
            "X-Internal-Service": internal_secret,
            "Content-Type": "application/json",
            "X-User-Id": user_id,
        }
        url = f"{LEAN_API_BASE_URL}{endpoint}"
        logger.debug("Using built-in LEAN engine", user_id=user_id, endpoint=endpoint)
    else:
        # QC Cloud - requires user's own credentials
        user_creds = await _fetch_user_qc_credentials(user_id)
        if not user_creds:
            raise QCCredentialsNotConfiguredError(user_id)

        headers = _generate_qc_auth_headers(
            user_creds["qc_user_id"], user_creds["api_token"]
        )
        url = f"{QC_CLOUD_API_URL}{endpoint}"
        logger.debug("Using QC Cloud", user_id=user_id, endpoint=endpoint)

    async with httpx.AsyncClient(timeout=60.0) as client:
        if method == "GET":
            response = await client.get(url, headers=headers)
        else:
            response = await client.request(
                method, url, headers=headers, json=payload or {}
            )

        # Parse JSON body BEFORE checking status - API errors include useful info
        data = None
        if response.content and response.content.strip():
            try:
                data = response.json()
            except Exception:
                pass  # Will handle below

        # Check for API-level errors in the response body (QC pattern: success: false)
        if isinstance(data, dict) and data.get("success") is False:
            errors = data.get("errors", [])
            error_msg = "; ".join(errors) if errors else data.get("error", str(data))
            raise Exception(f"QC API error ({response.status_code}): {error_msg}")

        # Now check HTTP status - but include body in error for debugging
        if response.status_code >= 400:
            error_detail = ""
            if data:
                error_detail = f" - {data}"
            elif response.text:
                error_detail = f" - {response.text[:200]}"
            raise Exception(f"QC API {response.status_code} for {endpoint}{error_detail}")

        # Handle empty response body
        if not response.content or response.content.strip() == b"":
            raise Exception(f"QC API returned empty response for {endpoint}")

        if data is None:
            raise Exception(
                f"QC API returned invalid JSON for {endpoint}: {response.text[:200]}"
            )

        # Handle case where API returns a string instead of dict
        if isinstance(data, str):
            raise Exception(f"QC API returned unexpected string: {data}")

        return data


def clear_credentials_cache(user_id: str | None = None) -> None:
    """Clear cached credentials for a user or all users.

    Call this when user updates their credentials to ensure fresh fetch.
    """
    if user_id:
        _user_credentials_cache.pop(user_id, None)
    else:
        _user_credentials_cache.clear()


def clear_settings_cache(user_id: str | None = None) -> None:
    """Clear cached settings for a user or all users.

    Call this when user updates their settings to ensure fresh fetch.
    """
    if user_id:
        _user_settings_cache.pop(user_id, None)
    else:
        _user_settings_cache.clear()
