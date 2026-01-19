"""Object store tools for QuantConnect."""

import base64
import hashlib
import json
import os
import time

import httpx
from langchain.tools import tool, ToolRuntime
from langgraph.graph.ui import push_ui_message
from pydantic import BaseModel, Field

from ..context import Context
from ..qc_api import qc_request


# ============================================================================
# Input Schemas
# ============================================================================

class UploadObjectInput(BaseModel):
    """Input schema for upload_object tool."""
    key: str = Field(description="Object key/name. Use .txt extension for readable content, .json for structured data.")
    content: str = Field(description="Content to upload")


class ReadObjectPropertiesInput(BaseModel):
    """Input schema for read_object_properties tool."""
    key: str = Field(description="Object key to read properties for")


class ListObjectStoreFilesInput(BaseModel):
    """Input schema for list_object_store_files tool."""
    path: str = Field(default="", description="Optional path to list (e.g., '/folder1'). Empty for root.")


class DeleteObjectInput(BaseModel):
    """Input schema for delete_object tool."""
    key: str = Field(description="Object key to delete")


# ============================================================================
# Tools
# ============================================================================

@tool(args_schema=UploadObjectInput)
async def upload_object(
    key: str,
    content: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Upload data to QuantConnect object store."""
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

        # Emit object-store UI
        push_ui_message("object-store-operation", {
            "operation": "upload",
            "key": key,
            "success": True,
            "size": len(content),
            "message": f"Successfully uploaded object: {key}",
        }, message={"id": runtime.tool_call_id})

        return json.dumps(
            {
                "success": True,
                "message": f"Successfully uploaded object: {key}",
                "key": key,
            }
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to upload object: {e!s}"})


@tool(args_schema=ReadObjectPropertiesInput)
async def read_object_properties(
    key: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Read object store file metadata."""
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        user_id = runtime.context.get("user_id")
        if not org_id:
            return json.dumps(
                {"error": True, "message": "Missing QUANTCONNECT_ORGANIZATION_ID."}
            )

        data = await qc_request(
            "/object/properties", {"organizationId": org_id, "key": key}, user_id=user_id
        )
        
        # Emit object properties UI
        push_ui_message("object-store-properties", {
            "key": key,
            "size": data.get("size"),
            "modified": data.get("modified"),
        }, message={"id": runtime.tool_call_id})
        
        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read object properties: {e!s}"}
        )


@tool(args_schema=ListObjectStoreFilesInput)
async def list_object_store_files(
    runtime: ToolRuntime[Context],
    path: str = "",
) -> str:
    """List object store files and get their keys."""
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        user_id = runtime.context.get("user_id")
        if not org_id:
            return json.dumps(
                {"error": True, "message": "Missing QUANTCONNECT_ORGANIZATION_ID."}
            )

        data = await qc_request(
            "/object/list", {"organizationId": org_id, "path": path or ""}, user_id=user_id
        )
        
        objects = data.get("objects", [])
        # Emit object store list UI
        push_ui_message("object-store-list", {
            "path": path or "/",
            "objects": [{"key": o.get("key"), "size": o.get("size")} for o in objects[:10]],
            "count": len(objects),
        }, message={"id": runtime.tool_call_id})
        
        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to list object store files: {e!s}"}
        )


@tool(args_schema=DeleteObjectInput)
async def delete_object(
    key: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Delete an object from the QuantConnect object store."""
    try:
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        user_id = runtime.context.get("user_id")
        if not org_id:
            return json.dumps(
                {"error": True, "message": "Missing QUANTCONNECT_ORGANIZATION_ID."}
            )

        await qc_request("/object/delete", {"organizationId": org_id, "key": key}, user_id=user_id)
        
        # Emit object-store delete UI
        push_ui_message("object-store-operation", {
            "operation": "delete",
            "key": key,
            "success": True,
            "message": f"Successfully deleted object: {key}",
        }, message={"id": runtime.tool_call_id})
        
        return json.dumps(
            {"success": True, "message": f"Successfully deleted object: {key}"}
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to delete object: {e!s}"})


# Export all tools
TOOLS = [upload_object, read_object_properties, list_object_store_files, delete_object]
