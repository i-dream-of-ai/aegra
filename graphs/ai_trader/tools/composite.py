"""Composite tools that combine multiple operations."""

import asyncio
import json
import os

from langgraph.runtime import get_runtime

from ai_trader.context import Context
from ai_trader.qc_api import qc_request


async def _poll_compile(qc_project_id: int, compile_id: str, timeout: int = 30) -> tuple[bool, str | None]:
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


async def _poll_backtest(qc_project_id: int, backtest_id: str, timeout: int = 60) -> tuple[dict | None, str | None]:
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


async def compile_and_backtest(backtest_name: str) -> str:
    """
    Compile code and create a backtest using default parameter values.

    Args:
        backtest_name: Format: "[Symbols] [Strategy Type]" (e.g., "AAPL Momentum Strategy")
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        # Compile
        compile_data = await qc_request("/compile/create", {"projectId": qc_project_id})
        compile_id = compile_data.get("compileId")
        if not compile_id:
            return json.dumps({"error": True, "message": "No compile ID returned."})

        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)
        if not is_compiled:
            return json.dumps({"error": True, "compile_id": compile_id, "message": f"Compilation failed: {compile_error}"})

        # Backtest
        backtest_data = await qc_request(
            "/backtests/create",
            {"projectId": qc_project_id, "organizationId": org_id, "compileId": compile_id, "backtestName": backtest_name},
        )
        backtest = backtest_data.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}
        backtest_id = backtest.get("backtestId")

        return json.dumps({
            "success": True,
            "compile_id": compile_id,
            "backtest_id": backtest_id,
            "backtest_name": backtest_name,
            "message": f"Backtest created! Use read_backtest with ID: {backtest_id}",
        })

    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {e!s}"})


async def compile_and_optimize(
    optimization_name: str,
    target: str,
    target_to: str,
    parameters: list[dict],
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
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if len(parameters) > 3:
            return json.dumps({"error": True, "message": "QC limits optimizations to 3 parameters max."})

        # Compile
        compile_data = await qc_request("/compile/create", {"projectId": qc_project_id})
        compile_id = compile_data.get("compileId")

        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)
        if not is_compiled:
            return json.dumps({"error": True, "compile_id": compile_id, "message": f"Compilation failed: {compile_error}"})

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

        opt_id = result.get("optimizations", [{}])[0].get("optimizationId") or result.get("optimizationId")

        estimated_runs = 1
        for p in parameters:
            steps = ((p.get("max", 100) - p.get("min", 0)) // p.get("step", 1)) + 1
            estimated_runs *= steps

        return json.dumps({
            "success": True,
            "compile_id": compile_id,
            "optimization_id": opt_id,
            "optimization_name": optimization_name,
            "estimated_backtests": estimated_runs,
            "message": f'Optimization "{optimization_name}" created! Use read_optimization with ID: {opt_id}',
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {e!s}"})


async def update_and_run_backtest(file_name: str, file_content: str, backtest_name: str) -> str:
    """
    Update file with COMPLETE new content, compile, and run backtest.

    Args:
        file_name: Name of the file to update (e.g., "main.py")
        file_content: Complete new contents of the file
        backtest_name: Format: "[Symbols] [Strategy Type]"
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"success": False, "error": "No project context."})

        # Update file
        await qc_request("/files/update", {"projectId": qc_project_id, "name": file_name, "content": file_content})

        # Compile
        compile_data = await qc_request("/compile/create", {"projectId": qc_project_id})
        compile_id = compile_data.get("compileId")

        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)
        if not is_compiled:
            return json.dumps({"success": False, "compile_id": compile_id, "error": f"Compilation failed: {compile_error}"})

        # Backtest
        backtest_data = await qc_request(
            "/backtests/create",
            {"projectId": qc_project_id, "organizationId": org_id, "compileId": compile_id, "backtestName": backtest_name},
        )
        backtest = backtest_data.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}
        backtest_id = backtest.get("backtestId")

        # Poll for results
        backtest_result, backtest_error = await _poll_backtest(qc_project_id, backtest_id)

        if backtest_error:
            return json.dumps({"success": False, "backtest_id": backtest_id, "error": backtest_error})

        if backtest_result:
            stats = backtest_result.get("statistics", {})
            return json.dumps({
                "success": True,
                "file_updated": file_name,
                "backtest_id": backtest_id,
                "completed": True,
                "statistics": {
                    "net_profit": stats.get("Net Profit", "N/A"),
                    "sharpe_ratio": stats.get("Sharpe Ratio", "N/A"),
                    "max_drawdown": stats.get("Drawdown", "N/A"),
                    "total_trades": stats.get("Total Trades", "N/A"),
                },
            }, indent=2)

        return json.dumps({"success": True, "backtest_id": backtest_id, "status": "Running"})

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


async def edit_and_run_backtest(file_name: str, edits: list[dict], backtest_name: str) -> str:
    """
    Edit file using search-and-replace, then compile and run backtest.

    Args:
        file_name: Name of the file to edit
        edits: List of edits, each with old_content and new_content
        backtest_name: Format: "[Symbols] [Strategy Type]"
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"success": False, "error": "No project context."})

        # Read current file
        files_data = await qc_request("/files/read", {"projectId": qc_project_id, "name": file_name})
        files = files_data.get("files", [])
        if not files:
            return json.dumps({"success": False, "error": f"File '{file_name}' not found"})
        current_content = files[0].get("content", "") if isinstance(files, list) else files_data.get("content", "")

        # Apply edits
        updated_content = current_content
        for i, edit in enumerate(edits):
            old_content = edit.get("old_content", "")
            new_content = edit.get("new_content", "")
            if not old_content:
                return json.dumps({"success": False, "error": f"Edit {i + 1}: old_content required"})
            if updated_content.count(old_content) == 0:
                return json.dumps({"success": False, "error": f"Edit {i + 1}: old_content not found"})
            if updated_content.count(old_content) > 1:
                return json.dumps({"success": False, "error": f"Edit {i + 1}: old_content not unique"})
            updated_content = updated_content.replace(old_content, new_content)

        # Update file
        await qc_request("/files/update", {"projectId": qc_project_id, "name": file_name, "content": updated_content})

        # Compile
        compile_data = await qc_request("/compile/create", {"projectId": qc_project_id})
        compile_id = compile_data.get("compileId")

        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)
        if not is_compiled:
            return json.dumps({"success": False, "error": f"Compilation failed: {compile_error}"})

        # Backtest
        backtest_data = await qc_request(
            "/backtests/create",
            {"projectId": qc_project_id, "organizationId": org_id, "compileId": compile_id, "backtestName": backtest_name},
        )
        backtest = backtest_data.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}
        backtest_id = backtest.get("backtestId")

        # Poll for results
        backtest_result, backtest_error = await _poll_backtest(qc_project_id, backtest_id)

        if backtest_error:
            return json.dumps({"success": False, "backtest_id": backtest_id, "error": backtest_error})

        if backtest_result:
            stats = backtest_result.get("statistics", {})
            return json.dumps({
                "success": True,
                "file_updated": file_name,
                "edits_applied": len(edits),
                "backtest_id": backtest_id,
                "completed": True,
                "statistics": {
                    "net_profit": stats.get("Net Profit", "N/A"),
                    "sharpe_ratio": stats.get("Sharpe Ratio", "N/A"),
                    "max_drawdown": stats.get("Drawdown", "N/A"),
                    "total_trades": stats.get("Total Trades", "N/A"),
                },
            }, indent=2)

        return json.dumps({"success": True, "backtest_id": backtest_id, "status": "Running"})

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# Export all tools
TOOLS = [
    compile_and_backtest,
    compile_and_optimize,
    update_and_run_backtest,
    edit_and_run_backtest,
]
