"""Composite tools that combine multiple operations."""

import asyncio
import json
import os

from langchain.tools import tool, ToolRuntime
from langgraph.graph.ui import push_ui_message

from ..context import Context
from ..qc_api import qc_request
from ..supabase_client import SupabaseClient
from .utils import format_error, format_success


def _format_error(message: str, details: dict | None = None) -> str:
    """Format an error response."""
    response = {"error": True, "message": message}
    if details:
        response.update(details)
    return json.dumps(response, indent=2)


def _format_success(message: str, data: dict | None = None) -> str:
    """Format a success response."""
    response = {"success": True, "message": message}
    if data:
        response.update(data)
    return json.dumps(response, indent=2)


async def _poll_compile(
    qc_project_id: int, compile_id: str, timeout: int = 30
) -> tuple[bool, str | None]:
    """Poll for compile completion."""
    for _ in range(timeout):
        await asyncio.sleep(1)
        try:
            status = await qc_request(
                "/compile/read",
                {"projectId": qc_project_id, "compileId": compile_id},
            )
            if status.get("state") == "BuildSuccess":
                return True, None
            elif status.get("state") == "BuildError":
                logs = status.get("logs", [])
                error_msg = "\n".join(logs) if isinstance(logs, list) else str(logs)
                return False, error_msg or "Unknown build error"
        except Exception as e:
            return False, str(e)
    return False, "Compilation timed out"


async def _poll_backtest(
    qc_project_id: int, backtest_id: str, timeout: int = 60
) -> tuple[dict | None, str | None]:
    """Poll for backtest completion."""
    for _ in range(timeout):
        await asyncio.sleep(3)
        try:
            status = await qc_request(
                "/backtests/read",
                {"projectId": qc_project_id, "backtestId": backtest_id},
            )
            bt = status.get("backtest", {})
            if isinstance(bt, list):
                bt = bt[0] if bt else {}

            if bt.get("error") or bt.get("hasInitializeError"):
                return None, bt.get("error", "Initialization error")

            if bt.get("completed"):
                return bt, None
        except Exception:
            pass
    return None, None


def _parse_percent(value: str | None) -> float | None:
    """Parse percentage string like '12.5%' to float 0.125."""
    if not value or value == "N/A":
        return None
    try:
        # Remove % and convert
        cleaned = str(value).replace("%", "").replace(",", "").strip()
        return float(cleaned) / 100
    except (ValueError, TypeError):
        return None


def _parse_decimal(value: str | None) -> float | None:
    """Parse decimal string to float."""
    if not value or value == "N/A":
        return None
    try:
        cleaned = str(value).replace(",", "").strip()
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_int(value: str | None) -> int | None:
    """Parse int string to int."""
    if not value or value == "N/A":
        return None
    try:
        cleaned = str(value).replace(",", "").strip()
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


async def _save_code_version(
    backtest_name: str,
    backtest_id: str,
    compile_id: str,
    code: str,
    stats: dict,
    qc_project_id: int | None,
    project_db_id: str | None,
    status: str = "completed",
    error_message: str | None = None,
) -> dict | None:
    """Save a code version to the database after backtest.

    Args:
        backtest_name: Name of the backtest
        backtest_id: QC backtest ID
        compile_id: QC compile ID
        code: Full code content
        stats: Backtest statistics from QC
        qc_project_id: QuantConnect project ID
        project_db_id: Database project ID
        status: "completed", "failed", or "error"
        error_message: Error message if failed

    Returns:
        The inserted code_version record or None on error
    """
    if not project_db_id:
        return None

    try:
        client = SupabaseClient(use_service_role=True)

        # Parse statistics
        record = {
            "project_id": int(project_db_id),
            "qc_project_id": qc_project_id,
            "compile_id": compile_id,
            "backtest_id": backtest_id,
            "backtest_name": backtest_name,
            "name": backtest_name,
            "code": code,
            "backtest_status": status,
            "error_message": error_message,
            # Metrics
            "total_return": _parse_percent(stats.get("Net Profit")),
            "sharpe_ratio": _parse_decimal(stats.get("Sharpe Ratio")),
            "max_drawdown": _parse_percent(stats.get("Drawdown")),
            "win_rate": _parse_percent(stats.get("Win Rate")),
            "total_trades": _parse_int(stats.get("Total Orders")),
        }

        result = await client.insert("code_versions", record)
        return result[0] if result else None

    except Exception:
        # Don't fail the backtest if saving fails
        return None


@tool
async def qc_compile_and_backtest(
    backtest_name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Compile code and create a backtest using default parameter values.

    Args:
        backtest_name: Format: "[Symbols] [Strategy Type]" (e.g., "AAPL Momentum Strategy")
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return _format_error("No project context.")

        # Compile
        compile_data = await qc_request("/compile/create", {"projectId": qc_project_id})
        compile_id = compile_data.get("compileId")
        if not compile_id:
            return format_error("No compile ID returned.")

        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)
        if not is_compiled:
            return format_error(
                f"Compilation failed: {compile_error}", {"compile_id": compile_id}
            )

        # Backtest
        backtest_data = await qc_request(
            "/backtests/create",
            {
                "projectId": qc_project_id,
                "organizationId": org_id,
                "compileId": compile_id,
                "backtestName": backtest_name,
            },
        )
        backtest = backtest_data.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}
        backtest_id = backtest.get("backtestId")

        # Emit UI for backtest started
        push_ui_message("backtest-stats", {
            "backtestId": backtest_id,
            "name": backtest_name,
            "status": "Running",
            "completed": False,
            "summary": {},
        }, message={"id": runtime.tool_call_id})

        return format_success(
            f"Backtest created! Use read_backtest with ID: {backtest_id}",
            {
                "compile_id": compile_id,
                "backtest_id": backtest_id,
                "backtest_name": backtest_name,
            },
        )

    except Exception as e:
        return format_error(f"Unexpected error: {str(e)}")


@tool
async def qc_compile_and_optimize(
    optimization_name: str,
    target: str,
    target_to: str,
    parameters: list[dict],
    runtime: ToolRuntime[Context],
    constraints: list[dict] = None,
    node_type: str = "O2-8",
    parallel_nodes: int = 4,
) -> str:
    """
    Compile code and create an optimization job. Max 3 parameters.

    Args:
        optimization_name: Format: "[Symbols] [Strategy] - Optimizing [Params]"
        target: Target metric (e.g., "TotalPerformance.PortfolioStatistics.SharpeRatio")
        target_to: Direction: "max" or "min"
        parameters: List of parameter configs (max 3) [{name, min, max, step}]
        constraints: Optional constraints [{target, operator, targetValue}]
        node_type: Node type ("O2-8", "O4-12", "O8-16")
        parallel_nodes: Number of parallel nodes (default: 4)
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return _format_error("No project context.")

        if len(parameters) > 3:
            return format_error("QC limits optimizations to 3 parameters max.")

        # Compile
        compile_data = await qc_request("/compile/create", {"projectId": qc_project_id})
        compile_id = compile_data.get("compileId")

        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)
        if not is_compiled:
            return json.dumps(
                {
                    "error": True,
                    "compile_id": compile_id,
                    "message": f"Compilation failed: {compile_error}",
                }
            )

        # Optimization
        result = await qc_request(
            "/optimizations/create",
            {
                "projectId": qc_project_id,
                "organizationId": org_id,
                "compileId": compile_id,
                "name": optimization_name,
                "target": target,
                "targetTo": target_to,
                "targetValue": None,
                "strategy": "QuantConnect.Optimizer.Strategies.GridSearchOptimizationStrategy",
                "parameters": parameters,
                "constraints": constraints or [],
                "nodeType": node_type,
                "parallelNodes": parallel_nodes,
            },
        )

        opt_id = result.get("optimizations", [{}])[0].get(
            "optimizationId"
        ) or result.get("optimizationId")

        estimated_runs = 1
        for p in parameters:
            steps = ((p.get("max", 100) - p.get("min", 0)) // p.get("step", 1)) + 1
            estimated_runs *= steps

        return json.dumps(
            {
                "success": True,
                "compile_id": compile_id,
                "optimization_id": opt_id,
                "optimization_name": optimization_name,
                "estimated_backtests": estimated_runs,
                "message": f'Optimization "{optimization_name}" created! Use read_optimization with ID: {opt_id}',
            },
            indent=2,
        )

    except Exception as e:
        return _format_error(f"Unexpected error: {e!s}")


@tool
async def qc_update_and_run_backtest(
    file_name: str,
    file_content: str,
    backtest_name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Update file with COMPLETE new content, compile, and run backtest.

    Args:
        file_name: Name of the file to update (e.g., "main.py")
        file_content: Complete new contents of the file
        backtest_name: Format: "[Symbols] [Strategy Type]"
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        project_db_id = runtime.context.get("project_db_id")
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return _format_error("No project context.")

        # Update file
        await qc_request(
            "/files/update",
            {"projectId": qc_project_id, "name": file_name, "content": file_content},
        )

        # Compile
        compile_data = await qc_request("/compile/create", {"projectId": qc_project_id})
        compile_id = compile_data.get("compileId")

        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)
        if not is_compiled:
            return json.dumps(
                {
                    "success": False,
                    "compile_id": compile_id,
                    "error": f"Compilation failed: {compile_error}",
                }
            )

        # Backtest
        backtest_data = await qc_request(
            "/backtests/create",
            {
                "projectId": qc_project_id,
                "organizationId": org_id,
                "compileId": compile_id,
                "backtestName": backtest_name,
            },
        )
        backtest = backtest_data.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}
        backtest_id = backtest.get("backtestId")

        # Poll for results
        backtest_result, backtest_error = await _poll_backtest(
            qc_project_id, backtest_id
        )

        if backtest_error:
            return json.dumps(
                {"success": False, "backtest_id": backtest_id, "error": backtest_error}
            )

        if backtest_result:
            stats = backtest_result.get("statistics", {})

            # Save code version to database
            saved_version = await _save_code_version(
                backtest_name=backtest_name,
                backtest_id=backtest_id,
                compile_id=compile_id,
                code=file_content,
                stats=stats,
                qc_project_id=qc_project_id,
                project_db_id=project_db_id,
                status="completed",
            )

            # Parse total orders as integer
            total_orders_raw = stats.get("Total Orders")
            total_orders = None
            if total_orders_raw is not None:
                try:
                    total_orders = int(float(str(total_orders_raw).replace(",", "")))
                except (ValueError, TypeError):
                    total_orders = None

            # Emit custom UI for backtest stats
            push_ui_message("backtest-stats", {
                "backtestId": backtest_id,
                "name": backtest_name,
                "status": "Completed",
                "completed": True,
                "summary": {
                    "totalReturn": stats.get("Net Profit"),
                    "annualReturn": stats.get("Compounding Annual Return"),
                    "sharpeRatio": stats.get("Sharpe Ratio"),
                    "drawdown": stats.get("Drawdown"),
                    "totalTrades": total_orders,
                    "winRate": stats.get("Win Rate"),
                    "profitFactor": stats.get("Profit-Loss Ratio", stats.get("Expectancy")),
                    "averageWin": stats.get("Average Win"),
                    "averageLoss": stats.get("Average Loss"),
                },
            }, message={"id": runtime.tool_call_id})

            return json.dumps(
                {
                    "success": True,
                    "file_updated": file_name,
                    "backtest_id": backtest_id,
                    "completed": True,
                    "code_version_id": saved_version.get("id")
                    if saved_version
                    else None,
                    "statistics": {
                        "net_profit": stats.get("Net Profit", "N/A"),
                        "cagr": stats.get("Compounding Annual Return", "N/A"),
                        "sharpe_ratio": stats.get("Sharpe Ratio", "N/A"),
                        "max_drawdown": stats.get("Drawdown", "N/A"),
                        "total_orders": stats.get("Total Orders", "N/A"),
                        "profit_factor": stats.get("Profit-Loss Ratio", stats.get("Expectancy", "N/A")),
                    },
                },
                indent=2,
            )

        return json.dumps(
            {"success": True, "backtest_id": backtest_id, "status": "Running"}
        )

    except Exception as e:
        return _format_error(str(e))


@tool
async def qc_edit_and_run_backtest(
    file_name: str,
    edits: list[dict],
    backtest_name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Edit file using search-and-replace, then compile and run backtest.

    Args:
        file_name: Name of the file to edit
        edits: List of edits, each with old_content and new_content
        backtest_name: Format: "[Symbols] [Strategy Type]"
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        project_db_id = runtime.context.get("project_db_id")
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return _format_error("No project context.")

        # Read current file
        files_data = await qc_request(
            "/files/read", {"projectId": qc_project_id, "name": file_name}
        )
        files = files_data.get("files", [])
        if not files:
            return _format_error(f"File '{file_name}' not found")

        current_content = (
            files[0].get("content", "")
            if isinstance(files, list)
            else files_data.get("content", "")
        )

        # Apply edits
        updated_content = current_content
        for i, edit in enumerate(edits):
            old_content = edit.get("old_content", "")
            new_content = edit.get("new_content", "")

            if not old_content:
                return _format_error(f"Edit {i + 1}: old_content required")

            # Robust matching with whitespace stripping
            old_stripped = old_content.strip()
            occurrences = updated_content.count(old_content)

            # If explicit match fails, try fuzzy match on stripped usage
            if occurrences == 0 and old_stripped:
                if updated_content.strip() == old_stripped:
                    # Whole file match
                    updated_content = new_content
                    continue
                else:
                    # Try regex for whitespace-insensitive match
                    import re

                    escaped_old = re.escape(old_stripped)
                    # Allow variable whitespace
                    pattern = re.sub(r"\s+", r"\\s+", escaped_old)
                    matches = list(re.finditer(pattern, updated_content))

                    if len(matches) == 1:
                        match = matches[0]
                        updated_content = (
                            updated_content[: match.start()]
                            + new_content
                            + updated_content[match.end() :]
                        )
                        continue
                    elif len(matches) > 1:
                        return _format_error(
                            f"Edit {i + 1}: old_content appears {len(matches)} times (fuzzy match). Must be unique."
                        )

            if occurrences == 0:
                return _format_error(
                    f"Edit {i + 1}: old_content not found in file",
                    {"hint": "Use read_file to check content. Whitespace matters."},
                )

            if occurrences > 1:
                return _format_error(
                    f"Edit {i + 1}: old_content not unique ({occurrences} found)"
                )

            updated_content = updated_content.replace(old_content, new_content)

        # Update file
        await qc_request(
            "/files/update",
            {"projectId": qc_project_id, "name": file_name, "content": updated_content},
        )

        # Compile
        compile_data = await qc_request("/compile/create", {"projectId": qc_project_id})
        compile_id = compile_data.get("compileId")

        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)
        if not is_compiled:
            return json.dumps(
                {"success": False, "error": f"Compilation failed: {compile_error}"}
            )

        # Backtest
        backtest_data = await qc_request(
            "/backtests/create",
            {
                "projectId": qc_project_id,
                "organizationId": org_id,
                "compileId": compile_id,
                "backtestName": backtest_name,
            },
        )
        backtest = backtest_data.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}
        backtest_id = backtest.get("backtestId")

        # Poll for results
        backtest_result, backtest_error = await _poll_backtest(
            qc_project_id, backtest_id
        )

        if backtest_error:
            return json.dumps(
                {"success": False, "backtest_id": backtest_id, "error": backtest_error}
            )

        if backtest_result:
            stats = backtest_result.get("statistics", {})

            # Save code version to database
            saved_version = await _save_code_version(
                backtest_name=backtest_name,
                backtest_id=backtest_id,
                compile_id=compile_id,
                code=updated_content,
                stats=stats,
                qc_project_id=qc_project_id,
                project_db_id=project_db_id,
                status="completed",
            )

            # Parse total orders as integer
            total_orders_raw = stats.get("Total Orders")
            total_orders = None
            if total_orders_raw is not None:
                try:
                    total_orders = int(float(str(total_orders_raw).replace(",", "")))
                except (ValueError, TypeError):
                    total_orders = None

            # Emit custom UI component for backtest stats
            push_ui_message("backtest-stats", {
                "backtestId": backtest_id,
                "name": backtest_name,
                "status": "Completed",
                "completed": True,
                "summary": {
                    "totalReturn": stats.get("Net Profit"),
                    "annualReturn": stats.get("Compounding Annual Return"),
                    "sharpeRatio": stats.get("Sharpe Ratio"),
                    "drawdown": stats.get("Drawdown"),
                    "totalTrades": total_orders,
                    "winRate": stats.get("Win Rate"),
                    "profitFactor": stats.get("Profit-Loss Ratio", stats.get("Expectancy")),
                    "averageWin": stats.get("Average Win"),
                    "averageLoss": stats.get("Average Loss"),
                },
            }, message={"id": runtime.tool_call_id})

            return json.dumps(
                {
                    "success": True,
                    "file_updated": file_name,
                    "edits_applied": len(edits),
                    "backtest_id": backtest_id,
                    "completed": True,
                    "code_version_id": saved_version.get("id")
                    if saved_version
                    else None,
                    "statistics": {
                        "net_profit": stats.get("Net Profit", "N/A"),
                        "cagr": stats.get("Compounding Annual Return", "N/A"),
                        "sharpe_ratio": stats.get("Sharpe Ratio", "N/A"),
                        "max_drawdown": stats.get("Drawdown", "N/A"),
                        "total_orders": stats.get("Total Orders", "N/A"),
                        "profit_factor": stats.get("Profit-Loss Ratio", stats.get("Expectancy", "N/A")),
                    },
                },
                indent=2,
            )

        return json.dumps(
            {"success": True, "backtest_id": backtest_id, "status": "Running"}
        )

    except Exception as e:
        return _format_error(str(e))


# Export all tools
TOOLS = [
    qc_compile_and_backtest,
    qc_compile_and_optimize,
    qc_update_and_run_backtest,
    qc_edit_and_run_backtest,
]
