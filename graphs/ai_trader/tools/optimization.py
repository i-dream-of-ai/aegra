"""Optimization tools for QuantConnect."""

import json
import os
from typing import Literal

from langchain.tools import tool, ToolRuntime
from langgraph.graph.ui import push_ui_message
from pydantic import BaseModel, Field

from ..context import Context
from ..qc_api import qc_request


# ============================================================================
# Input Schemas
# ============================================================================

class ParameterConfig(BaseModel):
    """Configuration for a single optimization parameter."""
    name: str = Field(description="Parameter name as defined in the algorithm using self.get_parameter()")
    min: float = Field(description="Minimum value for the parameter")
    max: float = Field(description="Maximum value for the parameter")
    step: float = Field(description="Step size between values")


class ConstraintConfig(BaseModel):
    """Configuration for an optimization constraint."""
    target: str = Field(description="Target metric (e.g., 'TotalPerformance.PortfolioStatistics.Drawdown')")
    operator: Literal["less", "lessorequal", "greater", "greaterorequal", "equals", "notequal"] = Field(
        description="Comparison operator"
    )
    targetValue: float = Field(description="Value to compare against")


class EstimateOptimizationInput(BaseModel):
    """Input schema for estimate_optimization tool."""
    compile_id: str = Field(description="The compile ID from a successful compilation")
    parameters: list[ParameterConfig] = Field(description="List of parameter configs (max 3). Each has name, min, max, step.")
    node_type: Literal["O2-8", "O4-12", "O8-16"] = Field(
        default="O2-8",
        description="Node type for optimization. O2-8 (2 CPU, 8GB RAM), O4-12 (4 CPU, 12GB RAM), O8-16 (8 CPU, 16GB RAM)"
    )
    parallel_nodes: int = Field(default=6, description="Number of parallel nodes (1-12)")


class CreateOptimizationInput(BaseModel):
    """Input schema for create_optimization tool."""
    compile_id: str = Field(description="The compile ID from a successful compilation")
    optimization_name: str = Field(
        description="Descriptive name. Format: '[Symbols] [Strategy] - Optimizing [Params]'. Example: 'SPY Momentum - Optimizing RSI Period'"
    )
    target: str = Field(
        description="Target metric to optimize. Common: 'TotalPerformance.PortfolioStatistics.SharpeRatio', 'TotalPerformance.PortfolioStatistics.NetProfit'"
    )
    target_to: Literal["max", "min"] = Field(description="Direction: 'max' to maximize, 'min' to minimize")
    parameters: list[ParameterConfig] = Field(description="List of parameter configs (max 3). Each has name, min, max, step.")
    constraints: list[ConstraintConfig] | None = Field(
        default=None,
        description="Optional constraints like minimum Sharpe or maximum drawdown"
    )
    node_type: Literal["O2-8", "O4-12", "O8-16"] = Field(
        default="O2-8",
        description="Node type for optimization"
    )
    parallel_nodes: int = Field(default=4, description="Number of parallel nodes (1-12)")


class ReadOptimizationInput(BaseModel):
    """Input schema for read_optimization tool."""
    optimization_id: str = Field(description="The optimization ID to read")
    page: int = Field(default=1, description="Page number (starts at 1)")
    page_size: int = Field(default=20, description="Results per page (max 50)")


class ListOptimizationsInput(BaseModel):
    """Input schema for list_optimizations tool."""
    page: int = Field(default=1, description="Page number (starts at 1)")
    page_size: int = Field(default=10, description="Results per page (max 20)")


class UpdateOptimizationInput(BaseModel):
    """Input schema for update_optimization tool."""
    optimization_id: str = Field(description="The optimization ID to update")
    name: str = Field(description="New name for the optimization")


class AbortOptimizationInput(BaseModel):
    """Input schema for abort_optimization tool."""
    optimization_id: str = Field(description="The optimization ID to abort")


class DeleteOptimizationInput(BaseModel):
    """Input schema for delete_optimization tool."""
    optimization_id: str = Field(description="The optimization ID to delete")


# ============================================================================
# Tools
# ============================================================================

@tool(args_schema=EstimateOptimizationInput)
async def estimate_optimization(
    compile_id: str,
    parameters: list[dict],
    runtime: ToolRuntime[Context],
    node_type: str = "O2-8",
    parallel_nodes: int = 6,
) -> str:
    """Estimate optimization cost and runtime before creating."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")
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
            user_id=user_id,
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


@tool(args_schema=CreateOptimizationInput)
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
    """Create a parameter optimization job on QuantConnect. Max 3 parameters."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")
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
            user_id=user_id,
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


@tool(args_schema=ReadOptimizationInput)
async def read_optimization(
    optimization_id: str,
    runtime: ToolRuntime[Context],
    page: int = 1,
    page_size: int = 20,
) -> str:
    """Read optimization status and paginated results."""
    # QC statistics indices (from their docs):
    # [0]=alpha, [1]=annual std dev, [2]=annual variance, [3]=avg loss%, [4]=avg win%,
    # [5]=beta, [6]=cagr%, [7]=drawdown%, [8]=estimated capacity, [9]=expectancy,
    # [10]=info ratio, [11]=loss rate%, [12]=net profit%, [13]=probabilistic sharpe,
    # [14]=profit-loss ratio, [15]=sharpe ratio, [16]=total fees, [17]=total orders,
    # [18]=tracking error, [19]=treynor ratio, [20]=win rate%
    # NOTE: QC returns stats as a dict with string keys ("0", "1", etc.), not a list!
    STAT_INDICES = {
        "alpha": "0",
        "annual_std_dev": "1",
        "cagr": "6",
        "drawdown": "7",
        "net_profit": "12",
        "sharpe_ratio": "15",
        "total_trades": "17",
        "win_rate": "20",
    }
    
    def get_stat(stats_obj, key):
        """Extract a statistic from the QC stats dict/list."""
        if not stats_obj:
            return None
        idx = STAT_INDICES.get(key)
        if idx is None:
            return None
        # QC returns dict with string keys like {"0": 0.123, "1": 0.456, ...}
        if isinstance(stats_obj, dict):
            return stats_obj.get(idx) or stats_obj.get(int(idx))
        # Fallback: handle as list (just in case)
        if isinstance(stats_obj, list):
            int_idx = int(idx)
            if int_idx < len(stats_obj):
                return stats_obj[int_idx]
        return None
    
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        # QC API only needs optimizationId for read (not projectId)
        result = await qc_request(
            "/optimizations/read",
            {"optimizationId": optimization_id},
            user_id=user_id,
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

        # Sort by Sharpe ratio using get_stat helper
        def get_sharpe(bt):
            stats = bt.get("statistics", {})
            val = get_stat(stats, "sharpe_ratio")
            return float(val or 0)
        
        sorted_bt = sorted(all_backtests, key=get_sharpe, reverse=True)

        total = len(sorted_bt)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1
        start = (page - 1) * page_size
        end = start + page_size
        page_results = sorted_bt[start:end]

        # Format results - get_stat handles both dict and list formats
        results = []
        for i, bt in enumerate(page_results):
            stats = bt.get("statistics", {})
            params = bt.get("parameterSet", bt.get("parameters", {}))
            
            net_profit = get_stat(stats, 'net_profit')
            cagr = get_stat(stats, 'cagr')
            sharpe = get_stat(stats, 'sharpe_ratio')
            drawdown = get_stat(stats, 'drawdown')
            win_rate = get_stat(stats, 'win_rate')
            
            results.append({
                "rank": start + i + 1,
                "parameters": params,
                "net_profit": f"{net_profit:.2f}%" if net_profit is not None else None,
                "cagr": f"{cagr:.2f}%" if cagr is not None else None,
                "sharpe_ratio": f"{sharpe:.3f}" if sharpe is not None else None,
                "max_drawdown": f"{drawdown:.2f}%" if drawdown is not None else None,
                "win_rate": f"{win_rate:.2f}%" if win_rate is not None else None,
            })

        # Best result
        best = None
        if sorted_bt:
            best_bt = sorted_bt[0]
            best_stats = best_bt.get("statistics", [])
            best_params = best_bt.get("parameterSet", best_bt.get("parameters", {}))
            
            net_profit = get_stat(best_stats, 'net_profit')
            cagr = get_stat(best_stats, 'cagr')
            sharpe = get_stat(best_stats, 'sharpe_ratio')
            
            best = {
                "parameters": best_params,
                "net_profit": f"{net_profit:.2f}%" if net_profit is not None else None,
                "cagr": f"{cagr:.2f}%" if cagr is not None else None,
                "sharpe_ratio": f"{sharpe:.3f}" if sharpe is not None else None,
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
        
        # Emit optimization results UI component via generative UI (linked to tool call message)
        push_ui_message("optimization-results", ui_data, message={"id": runtime.tool_call_id})

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

            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read optimization: {e!s}"}
        )


@tool(args_schema=ListOptimizationsInput)
async def list_optimizations(
    runtime: ToolRuntime[Context],
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List optimizations for the current project with pagination."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/optimizations/list",
            {"projectId": qc_project_id},
            user_id=user_id,
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


@tool(args_schema=UpdateOptimizationInput)
async def update_optimization(
    optimization_id: str,
    name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Update the name of an optimization."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/optimizations/update",
            {
                "projectId": qc_project_id,
                "optimizationId": optimization_id,
                "name": name,
            },
            user_id=user_id,
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


@tool(args_schema=AbortOptimizationInput)
async def abort_optimization(
    optimization_id: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Abort a running optimization. Completed backtests will be kept."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/optimizations/abort",
            {"projectId": qc_project_id, "optimizationId": optimization_id},
            user_id=user_id,
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


@tool(args_schema=DeleteOptimizationInput)
async def delete_optimization(
    optimization_id: str,
    runtime: ToolRuntime[Context],
) -> str:
    """Delete an optimization and all its results. This cannot be undone."""
    try:
        qc_project_id = runtime.context.get("qc_project_id")
        user_id = runtime.context.get("user_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        await qc_request(
            "/optimizations/delete",
            {"projectId": qc_project_id, "optimizationId": optimization_id},
            user_id=user_id,
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
