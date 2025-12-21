"""Miscellaneous tools for QuantConnect projects."""

import json
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from langgraph.types import interrupt
from qc_api import qc_request
from supabase_client import SupabaseClient, get_project_db_id, get_qc_project_id


@tool
async def wait(reason: str) -> str:
    """
    Pause execution to check for updates (tool results or user messages).

    Use when waiting for a specific background job ID or to yield control
    back to the system for a moment.

    Args:
        reason: Why we are waiting (e.g., "Waiting for backtest result")
    """
    # Interrupt the graph execution - Aegra/LangGraph will handle this
    # by checking for new messages and resuming with results
    result = interrupt(
        {
            "type": "check_inbox",
            "reason": reason,
        }
    )

    # When resumed, result will be the item found in inbox (or "no_update")
    return result if result else json.dumps({"status": "no_update"})


@tool
async def get_code_versions(
    config: Annotated[RunnableConfig, InjectedToolArg],
    page: int = 1,
    page_size: int = 10,
) -> str:
    """
    List saved code versions for this project with pagination.

    Each version includes: backtest_name, symbols, strategy_type,
    key metrics (return, Sharpe, drawdown, win rate, trades), and timestamps.

    Use get_code_version with an ID to retrieve the full code.

    Args:
        page: Page number (default: 1)
        page_size: Results per page (default: 10, max: 20)
    """
    try:
        project_db_id = get_project_db_id(config)
        if not project_db_id:
            return json.dumps(
                {
                    "error": True,
                    "message": "Project database ID not found.",
                }
            )

        # Use user's token for RLS (code_versions is project-scoped)
        client = SupabaseClient(config)
        all_versions = await client.select(
            "code_versions",
            {
                "select": "*",
                "project_id": f"eq.{project_db_id}",
                "order": "created_at.desc",
            },
        )

        # Paginate
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
                    "compile_id": v.get("compile_id"),
                    "symbols": v.get("symbols"),
                    "strategy_type": v.get("strategy_type"),
                    "metrics": {
                        "total_return": format_percent(v.get("total_return")),
                        "sharpe_ratio": format_decimal(v.get("sharpe_ratio")),
                        "max_drawdown": format_percent(v.get("max_drawdown")),
                        "win_rate": format_percent(v.get("win_rate")),
                        "total_trades": v.get("total_trades"),
                    },
                    "backtest_period": (
                        f"{v.get('backtest_start')} to {v.get('backtest_end')}"
                        if v.get("backtest_start") and v.get("backtest_end")
                        else None
                    ),
                    "status": v.get("backtest_status"),
                    "error": v.get("error_message"),
                    "created_at": v.get("created_at"),
                    "notes": v.get("notes"),
                }
            )

        return json.dumps(
            {
                "pagination": {
                    "current_page": page,
                    "page_size": page_size,
                    "total_results": total,
                    "total_pages": total_pages,
                    "has_more_pages": page < total_pages,
                },
                "hint": (
                    "No code versions found. Run a backtest to create a snapshot."
                    if total == 0
                    else "Use get_code_version with an ID to retrieve the full code."
                ),
                "versions": versions,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to get code versions: {str(e)}"}
        )


@tool
async def get_code_version(
    version_id: int,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Get a specific code version by ID.

    Returns the full code snapshot, metadata (name, compile ID, backtest ID, notes),
    and timestamps. Use this to review or restore previous code states.

    Args:
        version_id: The ID of the code version to retrieve
    """
    try:
        if not version_id:
            return json.dumps(
                {
                    "error": True,
                    "message": "version_id is required. Use get_code_versions first.",
                }
            )

        # Use user's token for RLS (code_versions is project-scoped)
        client = SupabaseClient(config)
        data = await client.select(
            "code_versions",
            {
                "select": "*",
                "id": f"eq.{version_id}",
                "limit": "1",
            },
        )

        if not data:
            return json.dumps(
                {
                    "error": True,
                    "message": f"Code version {version_id} not found.",
                    "hint": "Use get_code_versions to see available version IDs.",
                }
            )

        return json.dumps(data[0], indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to get code version: {str(e)}"}
        )


@tool
async def read_project_nodes(
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Read available and active nodes for the current QuantConnect project.
    """
    try:
        qc_project_id = get_qc_project_id(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/projects/nodes/read",
            {"projectId": qc_project_id},
        )

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read project nodes: {str(e)}"}
        )


@tool
async def update_project_nodes(
    nodes: list[str],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Update the enabled nodes for a QuantConnect project.

    Args:
        nodes: List of node identifiers (e.g., ["L1-1", "L1-2"])
    """
    try:
        qc_project_id = get_qc_project_id(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if not nodes:
            return json.dumps({"error": True, "message": "nodes array is required."})

        await qc_request(
            "/projects/nodes/update",
            {"projectId": qc_project_id, "nodes": nodes},
        )

        return json.dumps(
            {
                "success": True,
                "message": f"Updated project nodes: {nodes}",
                "nodes": nodes,
            }
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to update project nodes: {str(e)}"}
        )


@tool
async def read_lean_versions(
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Get available LEAN versions on QuantConnect.
    """
    try:
        qc_project_id = get_qc_project_id(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/lean/versions",
            {"projectId": qc_project_id},
        )

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read LEAN versions: {str(e)}"}
        )
