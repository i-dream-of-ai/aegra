"""Miscellaneous tools for QuantConnect projects."""

import asyncio
import json

from langchain.tools import tool, ToolRuntime
from langgraph.graph.ui import push_ui_message
from pydantic import BaseModel, Field

from ..context import Context
from ..qc_api import qc_request
from ..supabase_client import SupabaseClient


# ============================================================================
# Input Schemas
# ============================================================================

class WaitInput(BaseModel):
    """Input schema for wait tool."""
    seconds: int = Field(description="Number of seconds to wait (1-60)")
    reason: str = Field(description="Why we are waiting (e.g., 'Waiting for backtest to complete')")


class GetCodeVersionsInput(BaseModel):
    """Input schema for get_code_versions tool."""
    page: int = Field(default=1, description="Page number (starts at 1)")
    page_size: int = Field(default=10, description="Results per page (max 20)")


class GetCodeVersionInput(BaseModel):
    """Input schema for get_code_version tool."""
    version_id: int = Field(description="The ID of the code version to retrieve")


class UpdateProjectNodesInput(BaseModel):
    """Input schema for update_project_nodes tool."""
    nodes: list[str] = Field(description="List of node identifiers (e.g., ['L1-1', 'L1-2'])")


# ============================================================================
# Tools
# ============================================================================

@tool(args_schema=WaitInput)
async def wait(
    seconds: int,
    reason: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Wait for a specified duration before continuing."""
    # Clamp to reasonable bounds
    wait_time = max(1, min(60, seconds))
    
    push_ui_message("wait-status", {
        "seconds": wait_time,
        "reason": reason,
        "status": "waiting",
    }, message={"id": runtime.tool_call_id})
    
    await asyncio.sleep(wait_time)
    
    push_ui_message("wait-status", {
        "seconds": wait_time,
        "reason": reason,
        "status": "completed",
    }, message={"id": runtime.tool_call_id})
    
    return json.dumps(
        {"status": "completed", "waited_seconds": wait_time, "reason": reason}
    )


@tool(args_schema=GetCodeVersionsInput)
async def get_code_versions(
    runtime: ToolRuntime[Context],
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List saved code versions for this project with pagination."""
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

        # Emit code versions list UI
        push_ui_message("code-versions-list", {
            "versions": versions[:5],
            "pagination": {
                "currentPage": page,
                "totalPages": total_pages,
                "totalResults": total,
            },
        }, message={"id": runtime.tool_call_id})

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


@tool(args_schema=GetCodeVersionInput)
async def get_code_version(
    version_id: int,
    runtime: ToolRuntime[Context],
) -> str:
    """Get a specific code version by ID."""
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

        version = data[0]
        
        # Emit code version UI
        push_ui_message("code-version-detail", {
            "id": version.get("id"),
            "name": version.get("backtest_name") or version.get("name"),
            "backtestId": version.get("backtest_id"),
            "totalReturn": version.get("total_return"),
            "sharpeRatio": version.get("sharpe_ratio"),
            "lines": len(version.get("code", "").split("\n")) if version.get("code") else 0,
        }, message={"id": runtime.tool_call_id})

        return json.dumps(version, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to get code version: {e!s}"}
        )


@tool
async def read_project_nodes(runtime: ToolRuntime[Context]) -> str:
    """Read available and active nodes for the current QuantConnect project."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request("/projects/nodes/read", {"projectId": qc_project_id}, user_id=user_id)
        
        nodes = result.get("nodes", [])
        push_ui_message("project-nodes", {
            "nodes": nodes[:10] if nodes else [],
            "count": len(nodes),
        }, message={"id": runtime.tool_call_id})
        
        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read project nodes: {e!s}"}
        )


@tool(args_schema=UpdateProjectNodesInput)
async def update_project_nodes(
    nodes: list[str],
    runtime: ToolRuntime[Context],
) -> str:
    """Update the enabled nodes for a QuantConnect project."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/projects/nodes/update", {"projectId": qc_project_id, "nodes": nodes}, user_id=user_id
        )
        
        push_ui_message("project-nodes-update", {
            "success": True,
            "nodes": nodes,
            "message": f"Updated project nodes: {', '.join(nodes)}",
        }, message={"id": runtime.tool_call_id})
        
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
        user_id = runtime.context.get("user_id")
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request("/lean/versions", {"projectId": qc_project_id}, user_id=user_id)
        
        versions = result.get("versions", [])
        push_ui_message("lean-versions", {
            "versions": versions[:10] if versions else [],
            "count": len(versions),
        }, message={"id": runtime.tool_call_id})
        
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
