"""Object store tools for QuantConnect."""

import base64
import hashlib
import json
import os
import time

import httpx

from ai_trader.qc_api import qc_request


async def upload_object(key: str, content: str) -> str:
    """
    Upload data to QuantConnect object store.

    Args:
        key: Object key/name (use .txt for readable content)
        content: Content to upload
    """
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        user_id = os.environ.get("QUANTCONNECT_USER_ID")
        api_token = os.environ.get("QUANTCONNECT_TOKEN")

        if not all([org_id, user_id, api_token]):
            return json.dumps({"error": True, "message": "Missing QC credentials."})

        if not key or not content:
            return json.dumps(
                {"error": True, "message": "key and content are required."}
            )

        # Generate auth headers
        timestamp = int(time.time())
        timestamped_token = f"{api_token}:{timestamp}"
        hashed_token = hashlib.sha256(timestamped_token.encode()).hexdigest()
        auth_string = f"{user_id}:{hashed_token}"
        auth_header = f"Basic {base64.b64encode(auth_string.encode()).decode()}"

        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"objectData": (key, content.encode(), "application/octet-stream")}
            data = {"organizationId": org_id, "key": key}

            response = await client.post(
                "https://www.quantconnect.com/api/v2/object/set",
                headers={"Authorization": auth_header, "Timestamp": str(timestamp)},
                data=data,
                files=files,
            )

            result = (
                response.json()
                if response.headers.get("content-type", "").startswith(
                    "application/json"
                )
                else {"raw": response.text}
            )

            if not response.is_success or result.get("success") is False:
                return json.dumps(
                    {
                        "error": True,
                        "message": f"Upload failed: {result.get('errors', response.text)}",
                    }
                )

        return json.dumps(
            {
                "success": True,
                "message": f"Successfully uploaded object: {key}",
                "key": key,
            }
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to upload object: {e!s}"})


async def read_object_properties(key: str) -> str:
    """
    Read object store file metadata.

    Args:
        key: Object key to read properties for
    """
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        if not org_id:
            return json.dumps(
                {"error": True, "message": "Missing QUANTCONNECT_ORGANIZATION_ID."}
            )

        data = await qc_request(
            "/object/properties", {"organizationId": org_id, "key": key}
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read object properties: {e!s}"}
        )


async def list_object_store_files(path: str = "") -> str:
    """
    List object store files and get their keys.

    Args:
        path: Optional path to list (e.g., "/folder1"). Empty for root.
    """
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        if not org_id:
            return json.dumps(
                {"error": True, "message": "Missing QUANTCONNECT_ORGANIZATION_ID."}
            )

        data = await qc_request(
            "/object/list", {"organizationId": org_id, "path": path or ""}
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to list object store files: {e!s}"}
        )


async def delete_object(key: str) -> str:
    """
    Delete an object from the QuantConnect object store.

    Args:
        key: Object key to delete
    """
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        if not org_id:
            return json.dumps(
                {"error": True, "message": "Missing QUANTCONNECT_ORGANIZATION_ID."}
            )

        await qc_request("/object/delete", {"organizationId": org_id, "key": key})
        return json.dumps(
            {"success": True, "message": f"Successfully deleted object: {key}"}
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to delete object: {e!s}"})


# Export all tools
TOOLS = [upload_object, read_object_properties, list_object_store_files, delete_object]
