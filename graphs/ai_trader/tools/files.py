"""File tools for QuantConnect projects."""

import json

from langchain.tools import tool, ToolRuntime
from langgraph.graph.ui import push_ui_message
from pydantic import BaseModel, Field

from ..context import Context
from ..qc_api import qc_request


# ============================================================================
# Input Schemas
# ============================================================================

class CreateFileInput(BaseModel):
    """Input schema for qc_create_file tool."""
    file_name: str = Field(description="Name of the file (e.g., 'utils.py', 'research.py')")
    content: str = Field(description="Full content of the file")


class ReadFileInput(BaseModel):
    """Input schema for qc_read_file tool."""
    file_name: str = Field(description="Name of the file to read, or '*' to read all files")


class UpdateFileInput(BaseModel):
    """Input schema for qc_update_file tool."""
    file_name: str = Field(description="Name of the file to update")
    content: str = Field(description="New full content for the file - must be the COMPLETE file")


class RenameFileInput(BaseModel):
    """Input schema for qc_rename_file tool."""
    old_file_name: str = Field(description="Current name of the file")
    new_file_name: str = Field(description="New name for the file")


class DeleteFileInput(BaseModel):
    """Input schema for qc_delete_file tool."""
    file_name: str = Field(description="Name of the file to delete")


# ============================================================================
# Tools
# ============================================================================

@tool(args_schema=CreateFileInput)
async def qc_create_file(
    file_name: str,
    content: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Create a new file in the QuantConnect project."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/files/create",
            {
                "projectId": qc_project_id,
                "name": file_name,
                "content": content,
            },
            user_id=user_id,
        )

        # Emit file-operation UI
        push_ui_message("file-operation", {
            "operation": "create",
            "fileName": file_name,
            "success": True,
            "message": f"File '{file_name}' created successfully.",
        }, message={"id": runtime.tool_call_id})

        return json.dumps(
            {
                "success": True,
                "message": f"File '{file_name}' created successfully.",
                "file_name": file_name,
            }
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to create file: {e!s}"})


@tool(args_schema=ReadFileInput)
async def qc_read_file(
    file_name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Read a file from the QuantConnect project. Use '*' to read all files."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/files/read",
            {"projectId": qc_project_id, "name": file_name},
            user_id=user_id,
        )

        files = result.get("files", [])
        if not files:
            return json.dumps(
                {
                    "error": True,
                    "message": f"File '{file_name}' not found.",
                    "hint": 'Use read_file with "*" to list all files.',
                }
            )

        # Handle multiple files (when file_name is "*")
        if file_name == "*" and isinstance(files, list):
            file_list = []
            for f in files:
                file_list.append(
                    {
                        "name": f.get("name"),
                        "content": f.get("content"),
                    }
                )

            # Emit file-list UI
            push_ui_message("file-list", {
                "files": [{"name": f["name"], "lines": len(f.get("content", "").split("\n"))} for f in file_list],
                "count": len(file_list),
            }, message={"id": runtime.tool_call_id})

            return json.dumps(
                {
                    "success": True,
                    "files": file_list,
                },
                indent=2,
            )

        # Single file
        file_data = files[0] if isinstance(files, list) else files
        content = file_data.get("content", "")

        # Emit file-content UI
        push_ui_message("file-content", {
            "fileName": file_name,
            "content": content[:2000] if len(content) > 2000 else content,
            "truncated": len(content) > 2000,
            "lines": len(content.split("\n")),
        }, message={"id": runtime.tool_call_id})

        return json.dumps(
            {
                "success": True,
                "file_name": file_name,
                "content": content,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to read file: {e!s}"})


@tool(args_schema=UpdateFileInput)
async def qc_update_file(
    file_name: str,
    content: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Update an existing file in the QuantConnect project with new content."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/files/update",
            {
                "projectId": qc_project_id,
                "name": file_name,
                "content": content,
            },
            user_id=user_id,
        )

        # Emit file-operation UI
        push_ui_message("file-operation", {
            "operation": "update",
            "fileName": file_name,
            "success": True,
            "message": f"File '{file_name}' updated successfully.",
            "lines": len(content.split("\n")),
        }, message={"id": runtime.tool_call_id})

        return json.dumps(
            {
                "success": True,
                "message": f"File '{file_name}' updated successfully.",
                "file_name": file_name,
            }
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to update file: {e!s}"})


@tool(args_schema=RenameFileInput)
async def qc_rename_file(
    old_file_name: str,
    new_file_name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Rename a file in the QuantConnect project."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if not old_file_name or not new_file_name:
            return json.dumps(
                {"error": True, "message": "Both old and new file names are required."}
            )

        await qc_request(
            "/files/update",
            {
                "projectId": qc_project_id,
                "name": old_file_name,
                "newName": new_file_name,
            },
            user_id=user_id,
        )

        # Emit file-operation UI
        push_ui_message("file-operation", {
            "operation": "rename",
            "oldFileName": old_file_name,
            "newFileName": new_file_name,
            "success": True,
            "message": f"Renamed '{old_file_name}' to '{new_file_name}'.",
        }, message={"id": runtime.tool_call_id})

        return json.dumps(
            {
                "success": True,
                "message": f"Renamed '{old_file_name}' to '{new_file_name}'.",
                "old_name": old_file_name,
                "new_name": new_file_name,
            }
        )

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to rename file: {e!s}",
                "hint": 'Use read_file with "*" to list all files.',
            }
        )


@tool(args_schema=DeleteFileInput)
async def qc_delete_file(
    file_name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Delete a file from the QuantConnect project."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/files/delete",
            {"projectId": qc_project_id, "name": file_name},
            user_id=user_id,
        )

        # Emit file-operation UI
        push_ui_message("file-operation", {
            "operation": "delete",
            "fileName": file_name,
            "success": True,
            "message": f"File '{file_name}' deleted successfully.",
        }, message={"id": runtime.tool_call_id})

        return json.dumps(
            {
                "success": True,
                "message": f"File '{file_name}' deleted successfully.",
                "file_name": file_name,
            }
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to delete file: {e!s}"})


# Export all tools
TOOLS = [qc_create_file, qc_read_file, qc_update_file, qc_rename_file, qc_delete_file]
