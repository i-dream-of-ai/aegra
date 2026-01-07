"""File tools for QuantConnect projects."""

import json

from langchain.tools import tool, ToolRuntime

from ai_trader.context import Context
from ai_trader.qc_api import qc_request


@tool
async def qc_create_file(
    file_name: str,
    content: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Create a new file in the QuantConnect project.

    Args:
        file_name: Name of the file (e.g., "utils.py", "research.py")
        content: Full content of the file
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/files/create",
            {
                "projectId": qc_project_id,
                "name": file_name,
                "content": content,
            },
        )

        return json.dumps(
            {
                "success": True,
                "message": f"File '{file_name}' created successfully.",
                "file_name": file_name,
            }
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to create file: {e!s}"})


@tool
async def qc_read_file(
    file_name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Read a file from the QuantConnect project.
    Use "*" to read all files.

    Args:
        file_name: Name of the file to read, or "*" for all files
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/files/read",
            {"projectId": qc_project_id, "name": file_name},
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


@tool
async def qc_update_file(
    file_name: str,
    content: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Update an existing file in the QuantConnect project.

    Args:
        file_name: Name of the file to update
        content: New full content for the file
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/files/update",
            {
                "projectId": qc_project_id,
                "name": file_name,
                "content": content,
            },
        )

        return json.dumps(
            {
                "success": True,
                "message": f"File '{file_name}' updated successfully.",
                "file_name": file_name,
            }
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to update file: {e!s}"})


@tool
async def qc_rename_file(
    old_file_name: str,
    new_file_name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Rename a file in the QuantConnect project.

    Args:
        old_file_name: Current name of the file
        new_file_name: New name for the file
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

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
        )

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


@tool
async def qc_delete_file(
    file_name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Delete a file from the QuantConnect project.

    Args:
        file_name: Name of the file to delete
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/files/delete",
            {"projectId": qc_project_id, "name": file_name},
        )

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
