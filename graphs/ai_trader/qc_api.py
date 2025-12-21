"""
QuantConnect API Client

SHA256 timestamped authentication matching the TypeScript implementation.
"""

import base64
import hashlib
import os
import time
from typing import Any

import httpx

QC_API_URL = "https://www.quantconnect.com/api/v2"


def get_qc_auth_headers() -> dict[str, str]:
    """Generate QuantConnect authentication headers with SHA256 timestamped token."""
    user_id = os.environ.get("QUANTCONNECT_USER_ID")
    api_token = os.environ.get("QUANTCONNECT_TOKEN")
    org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

    if not all([user_id, api_token, org_id]):
        raise ValueError("Missing QuantConnect credentials")

    timestamp = int(time.time())
    timestamped_token = f"{api_token}:{timestamp}"
    hashed_token = hashlib.sha256(timestamped_token.encode()).hexdigest()
    authentication = f"{user_id}:{hashed_token}"
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
) -> Any:
    """Make authenticated request to QuantConnect API."""
    headers = get_qc_auth_headers()
    url = f"{QC_API_URL}{endpoint}"

    async with httpx.AsyncClient(timeout=60.0) as client:
        if method == "GET":
            response = await client.get(url, headers=headers)
        else:
            response = await client.request(
                method, url, headers=headers, json=payload or {}
            )

        response.raise_for_status()
        data = response.json()

        # Handle QC API success: false pattern
        if isinstance(data, dict) and data.get("success") is False:
            errors = data.get("errors", [])
            error_msg = "; ".join(errors) if errors else data.get("error", str(data))
            raise Exception(f"QC API error: {error_msg}")

        return data
