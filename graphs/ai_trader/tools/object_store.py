"""Object store tools for QuantConnect."""

import os
import json
import hashlib
import time
import base64
import httpx
from langchain_core.tools import tool
from qc_api import qc_request


@tool
async def upload_object(key: str, content: str) -> str:
    """
    Upload data to QuantConnect object store.

    WARNING: JSON/CSV files become write-only (cannot read content back).
    Use .txt extension for readable content (max 200 char preview).
    For retrievable numeric data, use self.Plot() in algorithms.

    Args:
        key: Object key/name (use .txt for readable content)
        content: Content to upload
    """
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        user_id = os.environ.get("QUANTCONNECT_USER_ID")
        api_token = os.environ.get("QUANTCONNECT_TOKEN")

        if not all([org_id, user_id, api_token]):
            return json.dumps({
                "error": True,
                "message": "Missing QC credentials (QUANTCONNECT_ORGANIZATION_ID, QUANTCONNECT_USER_ID, QUANTCONNECT_TOKEN).",
            })

        if not key:
            return json.dumps({"error": True, "message": "key is required."})

        if not content:
            return json.dumps({"error": True, "message": "content is required."})

        # Generate auth headers (same as qc_api.py)
        timestamp = int(time.time())
        timestamped_token = f"{api_token}:{timestamp}"
        hashed_token = hashlib.sha256(timestamped_token.encode()).hexdigest()
        auth_string = f"{user_id}:{hashed_token}"
        auth_header = f"Basic {base64.b64encode(auth_string.encode()).decode()}"

        # Upload uses multipart form data
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"objectData": (key, content.encode(), "application/octet-stream")}
            data = {"organizationId": org_id, "key": key}

            response = await client.post(
                "https://www.quantconnect.com/api/v2/object/set",
                headers={
                    "Authorization": auth_header,
                    "Timestamp": str(timestamp),
                },
                data=data,
                files=files,
                timeout=60.0,
            )

            result = response.json() if response.headers.get("content-type", "").startswith("application/json") else {"raw": response.text}

            if not response.is_success or result.get("success") is False:
                error_msg = result.get("errors", []) or result.get("error") or response.text
                return json.dumps({
                    "error": True,
                    "message": f"Upload failed: {error_msg}",
                    "key": key,
                })

        return json.dumps({
            "success": True,
            "message": f"Successfully uploaded object: {key}",
            "key": key,
        })

    except Exception as e:
        return json.dumps({
            "error": True,
            "message": f"Failed to upload object: {str(e)}",
            "key": key,
        })


@tool
async def read_object_properties(key: str) -> str:
    """
    Read object store file metadata (key, size, modified, created, md5, mime).

    NOTE: Preview field only works for .txt files (max 200 chars).
    JSON/CSV return metadata only - no content.

    Args:
        key: Object key to read properties for
    """
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        if not org_id:
            return json.dumps({"error": True, "message": "Missing QUANTCONNECT_ORGANIZATION_ID."})

        if not key:
            return json.dumps({"error": True, "message": "key is required."})

        data = await qc_request(
            "/object/properties",
            {"organizationId": org_id, "key": key},
        )

        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps({
            "error": True,
            "message": f"Failed to read object properties: {str(e)}",
            "key": key,
            "hint": "Use list_object_store_files to see available objects.",
        })


@tool
async def list_object_store_files(path: str = "") -> str:
    """
    List object store files and get their keys.

    Args:
        path: Optional path to list (e.g., "/folder1"). Empty for root.
    """
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        if not org_id:
            return json.dumps({"error": True, "message": "Missing QUANTCONNECT_ORGANIZATION_ID."})

        data = await qc_request(
            "/object/list",
            {"organizationId": org_id, "path": path or ""},
        )

        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps({
            "error": True,
            "message": f"Failed to list object store files: {str(e)}",
        })


@tool
async def delete_object(key: str) -> str:
    """
    Delete an object from the QuantConnect object store.

    Args:
        key: Object key to delete
    """
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        if not org_id:
            return json.dumps({"error": True, "message": "Missing QUANTCONNECT_ORGANIZATION_ID."})

        if not key:
            return json.dumps({"error": True, "message": "key is required."})

        await qc_request(
            "/object/delete",
            {"organizationId": org_id, "key": key},
        )

        return json.dumps({
            "success": True,
            "message": f"Successfully deleted object: {key}",
            "key": key,
        })

    except Exception as e:
        return json.dumps({
            "error": True,
            "message": f"Failed to delete object: {str(e)}",
            "key": key,
        })
