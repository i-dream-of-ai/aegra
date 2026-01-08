"""Optimization tools for QuantConnect."""

import json
import os

from langchain.tools import tool, ToolRuntime
from langgraph.graph.ui import push_ui_message

from ..context import Context
from ..qc_api import qc_request


@tool
async def estimate_optimization(
    compile_id: str,
    parameters: list[dict],
    runtime: ToolRuntime[Context],
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
        qc_project_id = runtime.context.get("qc_project_id")
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        qc_params = [
            {
                "name": p["name"],
                "min": p.get("min", 0),
                "max": p.get("max", 100),
                "step": p.get("step", 1),
            }
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
        return json.dumps(
            {
                "success": True,
                "compile_id": compile_id,
                "estimated_backtests": estimated_runs,
                "parameters": parameters,
                "node_type": node_type,
                "parallel_nodes": parallel_nodes,
                "qc_estimate": estimate,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to estimate: {e!s}"})


@tool
async def create_optimization(
    compile_id: str,
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
        qc_project_id = runtime.context.get("qc_project_id")
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if len(parameters) > 3:
            return json.dumps(
                {
                    "error": True,
                    "message": "QC limits optimizations to 3 parameters max.",
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
                "optimization_id": opt_id,
                "optimization_name": optimization_name,
                "compile_id": compile_id,
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
            {"error": True, "message": f"Failed to create optimization: {e!s}"}
        )


@tool
async def read_optimization(
    optimization_id: str,
    runtime: ToolRuntime[Context],
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
    # QC statistics array indices (from their docs):
    # [0]=alpha, [1]=annual std dev, [2]=annual variance, [3]=avg loss%, [4]=avg win%,
    # [5]=beta, [6]=cagr%, [7]=drawdown%, [8]=estimated capacity, [9]=expectancy,
    # [10]=info ratio, [11]=loss rate%, [12]=net profit%, [13]=probabilistic sharpe,
    # [14]=profit-loss ratio, [15]=sharpe ratio, [16]=total fees, [17]=total orders,
    # [18]=tracking error, [19]=treynor ratio, [20]=win rate%
    STAT_INDICES = {
        "alpha": 0,
        "annual_std_dev": 1,
        "cagr": 6,
        "drawdown": 7,
        "net_profit": 12,
        "sharpe_ratio": 15,
        "total_trades": 17,
        "win_rate": 20,
    }
    
    def get_stat(stats_array, key):
        """Extract a statistic from the QC stats array."""
        if not stats_array or not isinstance(stats_array, list):
            return None
        idx = STAT_INDICES.get(key)
        if idx is None or idx >= len(stats_array):
            return None
        return stats_array[idx]
    
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        # QC API only needs optimizationId for read (not projectId)
        result = await qc_request(
            "/optimizations/read",
            {"optimizationId": optimization_id},
        )

        opt = result.get("optimization", {})
        if isinstance(opt, str):
             return json.dumps({"error": True, "message": f"Unexpected API response: optimization field is a string ({opt}). Check ID."})

        # backtests can be dict (keyed by id) or list - normalize to list
        backtests_raw = opt.get("backtests", {})
        if isinstance(backtests_raw, dict):
            all_backtests = list(backtests_raw.values())
        elif isinstance(backtests_raw, list):
            all_backtests = backtests_raw
        else:
            all_backtests = []
        
        # Debug: Log the structure of first backtest to understand format
        sample_backtest = all_backtests[0] if all_backtests else None
        debug_info = {
            "backtests_type": type(backtests_raw).__name__,
            "backtests_count": len(all_backtests),
            "sample_keys": list(sample_backtest.keys()) if sample_backtest else [],
            "sample_stats_type": type(sample_backtest.get("statistics")).__name__ if sample_backtest else None,
            "sample_stats_length": len(sample_backtest.get("statistics", [])) if sample_backtest and isinstance(sample_backtest.get("statistics"), (list, dict)) else 0,
            "sample_stats_preview": sample_backtest.get("statistics", [])[:5] if sample_backtest and isinstance(sample_backtest.get("statistics"), list) else sample_backtest.get("statistics") if sample_backtest else None,
        }

        # Sort by Sharpe ratio (index 15 in stats array)
        def get_sharpe(bt):
            stats = bt.get("statistics", [])
            if isinstance(stats, list) and len(stats) > 15:
                return float(stats[15] or 0)
            # Fallback: maybe it's a dict (older format)
            if isinstance(stats, dict):
                return float(stats.get("Sharpe Ratio", 0) or 0)
            return 0
        
        sorted_bt = sorted(all_backtests, key=get_sharpe, reverse=True)

        total = len(sorted_bt)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        start = (page - 1) * page_size
        end = start + page_size
        page_results = sorted_bt[start:end]

        # Format results
        results = []
        for i, bt in enumerate(page_results):
            stats = bt.get("statistics", [])
            params = bt.get("parameterSet", bt.get("parameters", {}))
            
            # Handle both array and dict stats format
            if isinstance(stats, list):
                results.append({
                    "rank": start + i + 1,
                    "parameters": params,
                    "net_profit": f"{get_stat(stats, 'net_profit'):.2f}%" if get_stat(stats, 'net_profit') else None,
                    "cagr": f"{get_stat(stats, 'cagr'):.2f}%" if get_stat(stats, 'cagr') else None,
                    "sharpe_ratio": f"{get_stat(stats, 'sharpe_ratio'):.3f}" if get_stat(stats, 'sharpe_ratio') else None,
                    "max_drawdown": f"{get_stat(stats, 'drawdown'):.2f}%" if get_stat(stats, 'drawdown') else None,
                    "win_rate": f"{get_stat(stats, 'win_rate'):.2f}%" if get_stat(stats, 'win_rate') else None,
                })
            else:
                # Dict format (legacy)
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
            best_stats = best_bt.get("statistics", [])
            best_params = best_bt.get("parameterSet", best_bt.get("parameters", {}))
            
            if isinstance(best_stats, list):
                best = {
                    "parameters": best_params,
                    "net_profit": f"{get_stat(best_stats, 'net_profit'):.2f}%" if get_stat(best_stats, 'net_profit') else None,
                    "cagr": f"{get_stat(best_stats, 'cagr'):.2f}%" if get_stat(best_stats, 'cagr') else None,
                    "sharpe_ratio": f"{get_stat(best_stats, 'sharpe_ratio'):.3f}" if get_stat(best_stats, 'sharpe_ratio') else None,
                }
            else:
                best = {
                    "parameters": best_params,
                    "net_profit": best_stats.get("Net Profit"),
                    "cagr": best_stats.get("Compounding Annual Return"),
                    "sharpe_ratio": best_stats.get("Sharpe Ratio"),
                }

        # Runtime stats from QC
        runtime_stats = opt.get("runtimeStatistics", {})

        # Build UI-friendly data structure
        ui_data = {
            "optimizationId": optimization_id,
            "name": opt.get("name", "Unknown"),
            "status": opt.get("status", "Unknown"),
            "progress": runtime_stats.get("Completed", "0") + "/" + runtime_stats.get("Total", "0"),
            "bestResult": best,
            "pagination": {
                "currentPage": page,
                "pageSize": page_size,
                "totalResults": total,
                "totalPages": total_pages,
                "hasMorePages": page < total_pages,
            },
            "results": results,
        }
        
        # Emit optimization results UI component via generative UI
        push_ui_message("optimization-results", ui_data)

        return json.dumps(
            {
                "optimization_id": optimization_id,
                "name": opt.get("name", "Unknown"),
                "status": opt.get("status", "Unknown"),
                "completed": runtime_stats.get("Completed", "0"),
                "total": runtime_stats.get("Total", "0"),
                "failed": runtime_stats.get("Failed", "0"),
                "best_result": best,
                "pagination": {
                    "current_page": page,
                    "page_size": page_size,
                    "total_results": total,
                    "total_pages": total_pages,
                    "has_more_pages": page < total_pages,
                },
                "results": results,
                "_debug": debug_info,  # Temporary: shows what QC returns for stats
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read optimization: {e!s}"}
        )


@tool
async def list_optimizations(
    runtime: ToolRuntime[Context],
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
        qc_project_id = runtime.context.get("qc_project_id")

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
            optimizations.append(
                {
                    "optimization_id": opt.get("optimizationId"),
                    "name": opt.get("name", "Unknown"),
                    "status": opt.get("status", "Unknown"),
                    "created": opt.get("created"),
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
                "optimizations": optimizations,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to list optimizations: {e!s}"}
        )


@tool
async def update_optimization(
    optimization_id: str,
    name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Update the name of an optimization.

    Args:
        optimization_id: The optimization ID
        name: New name
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

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

        return json.dumps(
            {
                "success": True,
                "message": f'Updated optimization name to "{name}"',
                "optimization_id": optimization_id,
            }
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to update optimization: {e!s}"}
        )


@tool
async def abort_optimization(
    optimization_id: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Abort a running optimization. Completed backtests will be kept.

    Args:
        optimization_id: The optimization ID to abort
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/optimizations/abort",
            {"projectId": qc_project_id, "optimizationId": optimization_id},
        )

        return json.dumps(
            {
                "success": True,
                "message": f"Aborted optimization {optimization_id}. Completed backtests are preserved.",
                "optimization_id": optimization_id,
            }
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to abort optimization: {e!s}"}
        )


@tool
async def delete_optimization(
    optimization_id: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Delete an optimization and all its results. This cannot be undone.

    Args:
        optimization_id: The optimization ID to delete
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/optimizations/delete",
            {"projectId": qc_project_id, "optimizationId": optimization_id},
        )

        return json.dumps(
            {
                "success": True,
                "message": f"Deleted optimization {optimization_id} and all results.",
                "optimization_id": optimization_id,
            }
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to delete optimization: {e!s}"}
        )


# Export all tools
TOOLS = [
    estimate_optimization,
    create_optimization,
    read_optimization,
    list_optimizations,
    update_optimization,
    abort_optimization,
    delete_optimization,
]
