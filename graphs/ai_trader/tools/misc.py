"""Miscellaneous tools for QuantConnect projects."""

import asyncio
import json

from langchain.tools import tool, ToolRuntime

from ..context import Context
from ..qc_api import qc_request
from ..supabase_client import SupabaseClient


@tool
async def wait(seconds: int, reason: str) -> str:
    """
    Wait for a specified duration before continuing.

    Args:
        seconds: Number of seconds to wait (1-60)
        reason: Why we are waiting (e.g., "Waiting for backtest to complete")
    """
    # Clamp to reasonable bounds
    wait_time = max(1, min(60, seconds))
    await asyncio.sleep(wait_time)
    return json.dumps(
        {"status": "completed", "waited_seconds": wait_time, "reason": reason}
    )


@tool
async def get_code_versions(
    runtime: ToolRuntime[Context],
    page: int = 1,
    page_size: int = 10,
) -> str:
    """
    List saved code versions for this project with pagination.

    Args:
        page: Page number (default: 1)
        page_size: Results per page (default: 10, max: 20)
    """
    try:
        project_db_id = runtime.context.get("project_db_id")
        if not project_db_id:
            return json.dumps(
                {"error": True, "message": "Project database ID not found."}
            )

        # Use service role key for internal DB access
        client = SupabaseClient(use_service_role=True)
        all_versions = await client.select(
            "code_versions",
            {
                "select": "*",
                "project_id": f"eq.{project_db_id}",
                "order": "created_at.desc",
            },
        )

        total = len(all_versions)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        start = (page - 1) * page_size
        end = start + page_size
        page_versions = all_versions[start:end]

        def format_percent(val):
            return f"{val * 100:.1f}%" if val is not None else None

        def format_decimal(val):
            return f"{val:.2f}" if val is not None else None

        versions = []
        for i, v in enumerate(page_versions):
            versions.append(
                {
                    "rank": start + i + 1,
                    "id": v.get("id"),
                    "backtest_name": v.get("backtest_name") or v.get("name"),
                    "backtest_id": v.get("backtest_id"),
                    "metrics": {
                        "total_return": format_percent(v.get("total_return")),
                        "sharpe_ratio": format_decimal(v.get("sharpe_ratio")),
                        "max_drawdown": format_percent(v.get("max_drawdown")),
                        "win_rate": format_percent(v.get("win_rate")),
                        "total_trades": v.get("total_trades"),
                    },
                    "created_at": v.get("created_at"),
                }
            )

        return json.dumps(
            {
                "pagination": {
                    "current_page": page,
                    "page_size": page_size,
                    "total_results": total,
                    "total_pages": total_pages,
                },
                "versions": versions,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to get code versions: {e!s}"}
        )


@tool
async def get_code_version(version_id: int) -> str:
    """
    Get a specific code version by ID.

    Args:
        version_id: The ID of the code version to retrieve
    """
    try:
        if not version_id:
            return json.dumps({"error": True, "message": "version_id is required."})

        # Use service role key for internal DB access
        client = SupabaseClient(use_service_role=True)
        data = await client.select(
            "code_versions",
            {"select": "*", "id": f"eq.{version_id}", "limit": "1"},
        )

        if not data:
            return json.dumps(
                {"error": True, "message": f"Code version {version_id} not found."}
            )

        return json.dumps(data[0], indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to get code version: {e!s}"}
        )


@tool
async def read_project_nodes(runtime: ToolRuntime[Context]) -> str:
    """Read available and active nodes for the current QuantConnect project."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request("/projects/nodes/read", {"projectId": qc_project_id})
        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read project nodes: {e!s}"}
        )


@tool
async def update_project_nodes(
    nodes: list[str],
    runtime: ToolRuntime[Context],
) -> str:
    """
    Update the enabled nodes for a QuantConnect project.

    Args:
        nodes: List of node identifiers (e.g., ["L1-1", "L1-2"])
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/projects/nodes/update", {"projectId": qc_project_id, "nodes": nodes}
        )
        return json.dumps(
            {"success": True, "message": f"Updated project nodes: {nodes}"}
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to update project nodes: {e!s}"}
        )


@tool
async def read_lean_versions(runtime: ToolRuntime[Context]) -> str:
    """Get available LEAN versions on QuantConnect."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request("/lean/versions", {"projectId": qc_project_id})
        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read LEAN versions: {e!s}"}
        )


# Export all tools
TOOLS = [
    wait,
    get_code_versions,
    get_code_version,
    read_project_nodes,
    update_project_nodes,
    read_lean_versions,
]
