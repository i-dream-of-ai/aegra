"""Composite tools that combine multiple operations."""

import asyncio
import json
import os
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from qc_api import qc_request


def get_qc_project_id(config: RunnableConfig) -> int | None:
    """Extract qc_project_id from RunnableConfig."""
    configurable = config.get("configurable", {})
    project_id = configurable.get("qc_project_id")
    if project_id is not None:
        return int(project_id)
    env_id = os.environ.get("QC_PROJECT_ID")
    return int(env_id) if env_id else None


async def _poll_compile(
    qc_project_id: int, compile_id: str, timeout: int = 30
) -> tuple[bool, str | None]:
    """
    Poll for compile completion.

    Returns:
        (is_compiled, error_message) - if is_compiled is True, error_message is None
    """
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
    """
    Poll for backtest completion.

    Returns:
        (result, error_message) - if result is not None, backtest completed successfully
    """
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
    return None, None  # Still running, no error


@tool
async def compile_and_backtest(
    backtest_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Compile code and create a backtest using default parameter values.
    Returns immediately - does NOT wait for completion.

    Args:
        backtest_name: Format: "[Symbols] [Strategy Type]" (e.g., "AAPL Momentum Strategy")
    """
    try:
        qc_project_id = get_qc_project_id(config)
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        # Step 1: Compile
        try:
            compile_data = await qc_request(
                "/compile/create", {"projectId": qc_project_id}
            )
            compile_id = compile_data.get("compileId")
            if not compile_id:
                return json.dumps({"error": True, "message": "No compile ID returned."})
        except Exception as e:
            return json.dumps(
                {"error": True, "message": f"Failed to compile: {str(e)}"}
            )

        # Step 2: Poll compile
        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)

        if not is_compiled:
            return json.dumps(
                {
                    "error": True,
                    "compile_id": compile_id,
                    "message": f"Compilation failed: {compile_error}",
                }
            )

        # Step 3: Create backtest
        try:
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

            return json.dumps(
                {
                    "success": True,
                    "compile_id": compile_id,
                    "backtest_id": backtest_id,
                    "backtest_name": backtest_name,
                    "message": f"Backtest created! Use read_backtest with ID: {backtest_id}",
                }
            )
        except Exception as e:
            return json.dumps(
                {
                    "error": True,
                    "compile_id": compile_id,
                    "message": f"Failed to create backtest: {str(e)}",
                }
            )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {str(e)}"})


@tool
async def compile_and_optimize(
    optimization_name: str,
    target: str,
    target_to: str,
    parameters: list[dict],
    config: Annotated[RunnableConfig, InjectedToolArg],
    constraints: list[dict] = None,
    node_type: str = "O2-8",
    parallel_nodes: int = 4,
) -> str:
    """
    Compile code and create an optimization job. Max 3 parameters.
    Returns immediately - does NOT wait for completion.

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
        qc_project_id = get_qc_project_id(config)
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if not parameters or len(parameters) == 0:
            return json.dumps(
                {"error": True, "message": "At least one parameter is required."}
            )

        if len(parameters) > 3:
            return json.dumps(
                {
                    "error": True,
                    "message": "QC limits optimizations to 3 parameters max.",
                }
            )

        # Step 1: Compile
        try:
            compile_data = await qc_request(
                "/compile/create", {"projectId": qc_project_id}
            )
            compile_id = compile_data.get("compileId")
        except Exception as e:
            return json.dumps(
                {"error": True, "message": f"Failed to compile: {str(e)}"}
            )

        # Step 2: Poll compile
        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)

        if not is_compiled:
            return json.dumps(
                {
                    "error": True,
                    "compile_id": compile_id,
                    "message": f"Compilation failed: {compile_error}",
                }
            )

        # Transform constraint operators
        operator_map = {
            "less": "Less",
            "lessorequal": "LessOrEqual",
            "greater": "Greater",
            "greaterorequal": "GreaterOrEqual",
            "equals": "Equals",
            "notequal": "NotEqual",
        }
        transformed_constraints = []
        for c in constraints or []:
            op = (
                c.get("operator", "")
                .lower()
                .replace("_", "")
                .replace("-", "")
                .replace(" ", "")
            )
            transformed_constraints.append(
                {
                    "target": c["target"],
                    "operator": operator_map.get(op, c["operator"]),
                    "targetValue": c["targetValue"],
                }
            )

        # Step 3: Create optimization
        try:
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
                    "constraints": transformed_constraints,
                    "nodeType": node_type,
                    "parallelNodes": parallel_nodes,
                },
            )

            opt_id = result.get("optimizations", [{}])[0].get(
                "optimizationId"
            ) or result.get("optimizationId")

            # Calculate estimated runs
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
                    "target": target,
                    "target_to": target_to,
                    "estimated_backtests": estimated_runs,
                    "status": "running",
                    "message": f'Optimization "{optimization_name}" created! Use read_optimization with ID: {opt_id}',
                },
                indent=2,
            )
        except Exception as e:
            return json.dumps(
                {
                    "error": True,
                    "compile_id": compile_id,
                    "message": f"Failed to create optimization: {str(e)}",
                }
            )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Unexpected error: {str(e)}"})


@tool
async def update_and_run_backtest(
    file_name: str,
    file_content: str,
    backtest_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Update file with COMPLETE new content, compile, and run backtest.

    Use for:
    - Creating new algorithms from scratch
    - Rewriting most of the file (>50% changes)

    For small changes, prefer edit_and_run_backtest instead.

    Args:
        file_name: Name of the file to update (e.g., "main.py")
        file_content: Complete new contents of the file
        backtest_name: Format: "[Symbols] [Strategy Type]" (e.g., "AAPL Momentum Strategy")
    """
    try:
        qc_project_id = get_qc_project_id(config)
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"success": False, "error": "No project context."})

        if not file_name:
            return json.dumps({"success": False, "error": "file_name is required."})

        # Step 1: Update file
        try:
            await qc_request(
                "/files/update",
                {
                    "projectId": qc_project_id,
                    "name": file_name,
                    "content": file_content,
                },
            )
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "step": "file_update",
                    "error": f"Failed to update {file_name}: {str(e)}",
                }
            )

        # Step 2: Create compile
        try:
            compile_data = await qc_request(
                "/compile/create", {"projectId": qc_project_id}
            )
            compile_id = compile_data.get("compileId")
            if not compile_id:
                return json.dumps(
                    {
                        "success": False,
                        "step": "compile_create",
                        "error": "No compile ID returned",
                        "file_updated": file_name,
                    }
                )
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "step": "compile_create",
                    "error": f"Failed to compile: {str(e)}",
                    "file_updated": file_name,
                }
            )

        # Step 3: Poll for compilation
        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)

        if not is_compiled:
            return json.dumps(
                {
                    "success": False,
                    "step": "compilation",
                    "compile_id": compile_id,
                    "error": f"Compilation failed: {compile_error}",
                    "file_updated": file_name,
                }
            )

        # Step 4: Create backtest
        if not org_id:
            return json.dumps(
                {
                    "success": False,
                    "step": "backtest_create",
                    "error": "Missing QUANTCONNECT_ORGANIZATION_ID",
                }
            )

        try:
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

            if not backtest_id:
                return json.dumps(
                    {
                        "success": False,
                        "step": "backtest_create",
                        "compile_id": compile_id,
                        "error": "No backtest ID returned",
                    }
                )
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "step": "backtest_create",
                    "compile_id": compile_id,
                    "error": f"Failed to create backtest: {str(e)}",
                }
            )

        # Step 5: Poll for backtest completion
        backtest_result, backtest_error = await _poll_backtest(
            qc_project_id, backtest_id
        )

        if backtest_error:
            return json.dumps(
                {
                    "success": False,
                    "step": "backtest_execution",
                    "file_updated": file_name,
                    "compile_id": compile_id,
                    "backtest_id": backtest_id,
                    "error": f"Backtest failed: {backtest_error}",
                }
            )

        if backtest_result:
            stats = backtest_result.get("statistics", {})
            return json.dumps(
                {
                    "success": True,
                    "file_updated": file_name,
                    "compile_id": compile_id,
                    "backtest_id": backtest_id,
                    "backtest_name": backtest_name,
                    "completed": True,
                    "statistics": {
                        "net_profit": stats.get("Net Profit", "N/A"),
                        "cagr": stats.get("Compounding Annual Return", "N/A"),
                        "sharpe_ratio": stats.get("Sharpe Ratio", "N/A"),
                        "max_drawdown": stats.get("Drawdown", "N/A"),
                        "win_rate": stats.get("Win Rate", "N/A"),
                        "total_trades": stats.get("Total Trades", "N/A"),
                    },
                    "message": f"Updated {file_name}, compiled, and backtest completed!",
                },
                indent=2,
            )

        return json.dumps(
            {
                "success": True,
                "file_updated": file_name,
                "compile_id": compile_id,
                "backtest_id": backtest_id,
                "backtest_name": backtest_name,
                "status": "Running",
                "message": "Backtest started. Use read_backtest to check results.",
            }
        )

    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": f"Unexpected error: {str(e)}",
            }
        )


@tool
async def edit_and_run_backtest(
    file_name: str,
    edits: list[dict],
    backtest_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Edit file using search-and-replace, then compile and run backtest.

    PREFERRED for small changes - each edit has old_content and new_content.
    All edits are applied, then ONE backtest runs.

    Args:
        file_name: Name of the file to edit (e.g., "main.py")
        edits: List of edits, each with old_content (exact text to find) and new_content (replacement)
        backtest_name: Format: "[Symbols] [Strategy Type]"
    """
    try:
        qc_project_id = get_qc_project_id(config)
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"success": False, "error": "No project context."})

        if not edits:
            return json.dumps(
                {"success": False, "error": "At least one edit required."}
            )

        # Step 1: Read current file
        try:
            files_data = await qc_request(
                "/files/read",
                {"projectId": qc_project_id, "name": file_name},
            )
            files = files_data.get("files", [])
            if not files:
                return json.dumps(
                    {
                        "success": False,
                        "error": f"File '{file_name}' not found",
                    }
                )
            current_content = (
                files[0].get("content", "")
                if isinstance(files, list)
                else files_data.get("content", "")
            )
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "step": "file_read",
                    "error": f"Failed to read {file_name}: {str(e)}",
                }
            )

        # Step 2: Apply edits
        updated_content = current_content
        for i, edit in enumerate(edits):
            old_content = edit.get("old_content", "")
            new_content = edit.get("new_content", "")

            if not old_content:
                return json.dumps(
                    {
                        "success": False,
                        "edit_index": i + 1,
                        "error": f"Edit {i + 1}: old_content is required",
                    }
                )

            occurrences = updated_content.count(old_content)
            if occurrences == 0:
                return json.dumps(
                    {
                        "success": False,
                        "step": "search_replace",
                        "edit_index": i + 1,
                        "error": f"Edit {i + 1}: old_content not found in file",
                        "hint": "Use read_file to get current content and copy exact text.",
                    }
                )

            if occurrences > 1:
                return json.dumps(
                    {
                        "success": False,
                        "step": "search_replace",
                        "edit_index": i + 1,
                        "error": f"Edit {i + 1}: old_content appears {occurrences} times. Must be unique.",
                    }
                )

            updated_content = updated_content.replace(old_content, new_content)

        # Step 3: Update file
        try:
            await qc_request(
                "/files/update",
                {
                    "projectId": qc_project_id,
                    "name": file_name,
                    "content": updated_content,
                },
            )
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "step": "file_update",
                    "error": f"Failed to update {file_name}: {str(e)}",
                }
            )

        # Step 4: Compile
        try:
            compile_data = await qc_request(
                "/compile/create", {"projectId": qc_project_id}
            )
            compile_id = compile_data.get("compileId")
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "step": "compile_create",
                    "error": str(e),
                    "file_updated": file_name,
                    "edits_applied": len(edits),
                }
            )

        # Step 5: Poll compile
        is_compiled, compile_error = await _poll_compile(qc_project_id, compile_id)

        if not is_compiled:
            return json.dumps(
                {
                    "success": False,
                    "step": "compilation",
                    "compile_id": compile_id,
                    "error": f"Compilation failed: {compile_error}",
                }
            )

        # Step 6: Create backtest
        try:
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
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "step": "backtest_create",
                    "error": str(e),
                }
            )

        # Step 7: Poll backtest
        backtest_result, backtest_error = await _poll_backtest(
            qc_project_id, backtest_id
        )

        if backtest_error:
            return json.dumps(
                {
                    "success": False,
                    "step": "backtest_execution",
                    "backtest_id": backtest_id,
                    "error": backtest_error,
                }
            )

        if backtest_result:
            stats = backtest_result.get("statistics", {})
            return json.dumps(
                {
                    "success": True,
                    "file_updated": file_name,
                    "edits_applied": len(edits),
                    "compile_id": compile_id,
                    "backtest_id": backtest_id,
                    "backtest_name": backtest_name,
                    "completed": True,
                    "statistics": {
                        "net_profit": stats.get("Net Profit", "N/A"),
                        "cagr": stats.get("Compounding Annual Return", "N/A"),
                        "sharpe_ratio": stats.get("Sharpe Ratio", "N/A"),
                        "max_drawdown": stats.get("Drawdown", "N/A"),
                        "win_rate": stats.get("Win Rate", "N/A"),
                        "total_trades": stats.get("Total Trades", "N/A"),
                    },
                    "message": f"Applied {len(edits)} edit(s), compiled, backtest completed!",
                },
                indent=2,
            )

        return json.dumps(
            {
                "success": True,
                "file_updated": file_name,
                "edits_applied": len(edits),
                "backtest_id": backtest_id,
                "status": "Running",
                "message": "Backtest started. Use read_backtest to check results.",
            }
        )

    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": f"Unexpected error: {str(e)}",
            }
        )
