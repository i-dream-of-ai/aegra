"""Optimization tools for QuantConnect."""

import os
import json
from typing import Annotated
from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from qc_api import qc_request


def get_qc_project_id(config: RunnableConfig) -> int | None:
    """Extract qc_project_id from RunnableConfig."""
    configurable = config.get("configurable", {})
    project_id = configurable.get("qc_project_id")
    if project_id is not None:
        return int(project_id)
    env_id = os.environ.get("QC_PROJECT_ID")
    return int(env_id) if env_id else None


@tool
async def estimate_optimization(
    compile_id: str,
    parameters: list[dict],
    config: Annotated[RunnableConfig, InjectedToolArg],
    node_type: str = "O2-8",
    parallel_nodes: int = 6,
) -> str:
    """
    Estimate optimization cost and runtime before creating.

    Args:
        compile_id: The compile ID
        parameters: List of parameter configs [{name, min, max, step}]
        node_type: Node type ("O2-8", "O4-12", "O8-16")
        parallel_nodes: Number of parallel nodes (default: 6)
    """
    try:
        qc_project_id = get_qc_project_id(config)
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        qc_params = [
            {"name": p["name"], "min": p.get("min", 0), "max": p.get("max", 100), "step": p.get("step", 1)}
            for p in parameters
        ]

        # Calculate estimated backtests
        estimated_runs = 1
        for p in parameters:
            steps = ((p.get("max", 100) - p.get("min", 0)) // p.get("step", 1)) + 1
            estimated_runs *= steps

        result = await qc_request(
            "/optimizations/estimate",
            {
                "projectId": qc_project_id,
                "organizationId": org_id,
                "compileId": compile_id,
                "parameters": qc_params,
                "nodeType": node_type,
                "parallelNodes": parallel_nodes,
            },
        )

        estimate = result.get("estimate", {})
        return json.dumps({
            "success": True,
            "compile_id": compile_id,
            "estimated_backtests": estimated_runs,
            "parameters": parameters,
            "node_type": node_type,
            "parallel_nodes": parallel_nodes,
            "qc_estimate": estimate,
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to estimate: {str(e)}"})


@tool
async def create_optimization(
    compile_id: str,
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
    Create a parameter optimization job on QuantConnect. Max 3 parameters.

    Args:
        compile_id: The compile ID
        optimization_name: Name format: "[Symbols] [Strategy] - Optimizing [Params]"
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

        if len(parameters) > 3:
            return json.dumps({
                "error": True,
                "message": "QC limits optimizations to 3 parameters max.",
            })

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
        for c in (constraints or []):
            op = c.get("operator", "").lower().replace("_", "").replace("-", "").replace(" ", "")
            transformed_constraints.append({
                "target": c["target"],
                "operator": operator_map.get(op, c["operator"]),
                "targetValue": c["targetValue"],
            })

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

        opt_id = result.get("optimizations", [{}])[0].get("optimizationId") or result.get("optimizationId")

        # Calculate estimated runs
        estimated_runs = 1
        for p in parameters:
            steps = ((p.get("max", 100) - p.get("min", 0)) // p.get("step", 1)) + 1
            estimated_runs *= steps

        return json.dumps({
            "success": True,
            "optimization_id": opt_id,
            "optimization_name": optimization_name,
            "compile_id": compile_id,
            "target": target,
            "target_to": target_to,
            "estimated_backtests": estimated_runs,
            "status": "running",
            "message": f'Optimization "{optimization_name}" created! Use read_optimization with ID: {opt_id}',
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to create optimization: {str(e)}"})


@tool
async def read_optimization(
    optimization_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
    page: int = 1,
    page_size: int = 20,
) -> str:
    """
    Read optimization status and paginated results.

    Args:
        optimization_id: The optimization ID
        page: Page number (default: 1)
        page_size: Results per page (default: 20, max: 50)
    """
    try:
        qc_project_id = get_qc_project_id(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/optimizations/read",
            {"projectId": qc_project_id, "optimizationId": optimization_id},
        )

        opt = result.get("optimization", {})
        all_backtests = opt.get("backtests", [])

        # Sort by target metric (Sharpe by default)
        sorted_bt = sorted(
            all_backtests,
            key=lambda x: float(x.get("statistics", {}).get("Sharpe Ratio", 0) or 0),
            reverse=True,
        )

        total = len(sorted_bt)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        start = (page - 1) * page_size
        end = start + page_size
        page_results = sorted_bt[start:end]

        # Format results
        results = []
        for i, bt in enumerate(page_results):
            stats = bt.get("statistics", {})
            params = bt.get("parameters", {})
            results.append({
                "rank": start + i + 1,
                "parameters": params,
                "net_profit": stats.get("Net Profit"),
                "cagr": stats.get("Compounding Annual Return"),
                "sharpe_ratio": stats.get("Sharpe Ratio"),
                "max_drawdown": stats.get("Drawdown"),
                "win_rate": stats.get("Win Rate"),
            })

        # Best result
        best = None
        if sorted_bt:
            best_bt = sorted_bt[0]
            best_stats = best_bt.get("statistics", {})
            best = {
                "parameters": best_bt.get("parameters", {}),
                "net_profit": best_stats.get("Net Profit"),
                "cagr": best_stats.get("Compounding Annual Return"),
                "sharpe_ratio": best_stats.get("Sharpe Ratio"),
            }

        return json.dumps({
            "optimization_id": optimization_id,
            "name": opt.get("name", "Unknown"),
            "status": opt.get("status", "Unknown"),
            "progress": f"{(opt.get('progress', 0) * 100):.1f}%",
            "best_result": best,
            "pagination": {
                "current_page": page,
                "page_size": page_size,
                "total_results": total,
                "total_pages": total_pages,
                "has_more_pages": page < total_pages,
            },
            "results": results,
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to read optimization: {str(e)}"})


@tool
async def list_optimizations(
    config: Annotated[RunnableConfig, InjectedToolArg],
    page: int = 1,
    page_size: int = 10,
) -> str:
    """
    List optimizations for the current project with pagination.

    Args:
        page: Page number (default: 1)
        page_size: Results per page (default: 10, max: 20)
    """
    try:
        qc_project_id = get_qc_project_id(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/optimizations/list",
            {"projectId": qc_project_id},
        )

        all_opts = result.get("optimizations", [])
        total = len(all_opts)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1

        start = (page - 1) * page_size
        end = start + page_size
        page_opts = all_opts[start:end]

        optimizations = []
        for opt in page_opts:
            optimizations.append({
                "optimization_id": opt.get("optimizationId"),
                "name": opt.get("name", "Unknown"),
                "status": opt.get("status", "Unknown"),
                "created": opt.get("created"),
            })

        return json.dumps({
            "pagination": {
                "current_page": page,
                "page_size": page_size,
                "total_results": total,
                "total_pages": total_pages,
                "has_more_pages": page < total_pages,
            },
            "optimizations": optimizations,
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to list optimizations: {str(e)}"})


@tool
async def update_optimization(
    optimization_id: str,
    name: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Update the name of an optimization.

    Args:
        optimization_id: The optimization ID
        name: New name
    """
    try:
        qc_project_id = get_qc_project_id(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/optimizations/update",
            {
                "projectId": qc_project_id,
                "optimizationId": optimization_id,
                "name": name,
            },
        )

        return json.dumps({
            "success": True,
            "message": f'Updated optimization name to "{name}"',
            "optimization_id": optimization_id,
        })

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to update optimization: {str(e)}"})


@tool
async def abort_optimization(
    optimization_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Abort a running optimization. Completed backtests will be kept.

    Args:
        optimization_id: The optimization ID to abort
    """
    try:
        qc_project_id = get_qc_project_id(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/optimizations/abort",
            {"projectId": qc_project_id, "optimizationId": optimization_id},
        )

        return json.dumps({
            "success": True,
            "message": f"Aborted optimization {optimization_id}. Completed backtests are preserved.",
            "optimization_id": optimization_id,
        })

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to abort optimization: {str(e)}"})


@tool
async def delete_optimization(
    optimization_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Delete an optimization and all its results. This cannot be undone.

    Args:
        optimization_id: The optimization ID to delete
    """
    try:
        qc_project_id = get_qc_project_id(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/optimizations/delete",
            {"projectId": qc_project_id, "optimizationId": optimization_id},
        )

        return json.dumps({
            "success": True,
            "message": f"Deleted optimization {optimization_id} and all results.",
            "optimization_id": optimization_id,
        })

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to delete optimization: {str(e)}"})
