"""
QuantConnect API Client

SHA256 timestamped authentication matching the TypeScript implementation.
Supports both QuantConnect Cloud and self-hosted LEAN API.
"""

import base64
import hashlib
import os
import time
from typing import Any

import httpx

# Check if we should use self-hosted LEAN
USE_SELF_HOSTED = os.environ.get("USE_SELF_HOSTED_LEAN", "").lower() == "true"
LEAN_API_URL = os.environ.get("LEAN_API_URL", "http://localhost:3001")

QC_API_URL = f"{LEAN_API_URL}/api/v2" if USE_SELF_HOSTED else "https://www.quantconnect.com/api/v2"


def get_qc_auth_headers(user_id_for_request: str | None = None) -> dict[str, str]:
    """Generate authentication headers.

    For self-hosted LEAN: Uses internal service auth.
    For QuantConnect Cloud: Uses SHA256 timestamped token.

    Args:
        user_id_for_request: User ID to associate with the request (self-hosted only)
    """
    if USE_SELF_HOSTED:
        # Internal service-to-service auth for self-hosted LEAN
        internal_secret = os.environ.get("INTERNAL_SERVICE_SECRET", "")
        headers = {
            "X-Internal-Service": internal_secret,
            "Content-Type": "application/json",
        }
        if user_id_for_request:
            headers["X-User-Id"] = user_id_for_request
        return headers

    # QuantConnect Cloud auth
    qc_user_id = os.environ.get("QUANTCONNECT_USER_ID")
    api_token = os.environ.get("QUANTCONNECT_TOKEN")
    org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

    if not all([qc_user_id, api_token, org_id]):
        raise ValueError("Missing QuantConnect credentials")

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
    """Make authenticated request to QuantConnect API.

    Args:
        endpoint: API endpoint path (e.g., "/files/read")
        payload: Request body as dict
        method: HTTP method (default POST)
        user_id: User ID for ownership verification (required for self-hosted LEAN)
    """
    headers = get_qc_auth_headers(user_id)
    url = f"{QC_API_URL}{endpoint}"

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
            raise Exception(
                f"QC API {response.status_code} for {endpoint}{error_detail}"
            )

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
