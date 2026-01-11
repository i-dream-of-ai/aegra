"""
QuantConnect Tools - Complete Tool Suite

All tools for interacting with QuantConnect API.
"""

# Path setup for Aegra compatibility
import sys
from pathlib import Path

_tools_dir = Path(__file__).parent
_parent_dir = _tools_dir.parent
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

# Files
# AI Services
from .ai_services import (
    check_initialization_errors,
    check_syntax,
    complete_code,
    enhance_error_message,
    get_algorithm_code,
    search_local_algorithms,
    search_quantconnect,
    update_code_to_pep8,
)

# Backtest
from .backtest import (
    create_backtest,
    delete_backtest,
    list_backtests,
    read_backtest,
    read_backtest_chart,
    read_backtest_insights,
    read_backtest_orders,
    update_backtest,
)

# Compile
from .compile import create_compile, read_compile

# Composite (preferred workflows)
from .composite import (
    qc_compile_and_backtest,
    qc_compile_and_optimize,
    qc_edit_and_run_backtest,
    qc_update_and_run_backtest,
)
from .files import (
    qc_create_file,
    qc_delete_file,
    qc_read_file,
    qc_rename_file,
    qc_update_file,
)

# Misc
from .misc import (
    get_code_version,
    get_code_versions,
    read_lean_versions,
    read_project_nodes,
    update_project_nodes,
    wait,
)

# Object Store
from .object_store import (
    delete_object,
    list_object_store_files,
    read_object_properties,
    upload_object,
)

# Optimization
from .optimization import (
    abort_optimization,
    create_optimization,
    delete_optimization,
    estimate_optimization,
    list_optimizations,
    read_optimization,
    update_optimization,
)

# Review (Doubtful Deacon) - Now handled by SubAgentMiddleware's task() tool
# The "code-reviewer" subagent is configured in graph.py

__all__ = [
    # Files (5)
    "qc_create_file",
    "qc_read_file",
    "qc_update_file",
    "qc_rename_file",
    "qc_delete_file",
    # Compile (2)
    "create_compile",
    "read_compile",
    # Backtest (8)
    "create_backtest",
    "read_backtest",
    "read_backtest_chart",
    "read_backtest_orders",
    "read_backtest_insights",
    "list_backtests",
    "update_backtest",
    "delete_backtest",
    # Optimization (7)
    "estimate_optimization",
    "create_optimization",
    "read_optimization",
    "list_optimizations",
    "update_optimization",
    "abort_optimization",
    "delete_optimization",
    # Object Store (4)
    "upload_object",
    "read_object_properties",
    "list_object_store_files",
    "delete_object",
    # Composite (4)
    "qc_compile_and_backtest",
    "qc_compile_and_optimize",
    "qc_update_and_run_backtest",
    "qc_edit_and_run_backtest",
    # AI Services (8)
    "check_initialization_errors",
    "complete_code",
    "enhance_error_message",
    "check_syntax",
    "update_code_to_pep8",
    "search_quantconnect",
    "search_local_algorithms",
    "get_algorithm_code",
    # Misc (6)
    "wait",
    "get_code_versions",
    "get_code_version",
    "read_project_nodes",
    "update_project_nodes",
    "read_lean_versions",
    # Review - handled by "code-reviewer" subagent via task() tool
]
