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
from .files import (
    create_file,
    read_file,
    update_file,
    rename_file,
    delete_file,
)

# Compile
from .compile import create_compile, read_compile

# Backtest
from .backtest import (
    create_backtest,
    read_backtest,
    read_backtest_chart,
    read_backtest_orders,
    read_backtest_insights,
    list_backtests,
    update_backtest,
    delete_backtest,
)

# Optimization
from .optimization import (
    estimate_optimization,
    create_optimization,
    read_optimization,
    list_optimizations,
    update_optimization,
    abort_optimization,
    delete_optimization,
)

# Object Store
from .object_store import (
    upload_object,
    read_object_properties,
    list_object_store_files,
    delete_object,
)

# Composite (preferred workflows)
from .composite import (
    compile_and_backtest,
    compile_and_optimize,
    update_and_run_backtest,
    edit_and_run_backtest,
)

# AI Services
from .ai_services import (
    check_initialization_errors,
    complete_code,
    enhance_error_message,
    check_syntax,
    update_code_to_pep8,
    search_quantconnect,
    search_local_algorithms,
    get_algorithm_code,
)

# Misc
from .misc import (
    wait,
    get_code_versions,
    get_code_version,
    read_project_nodes,
    update_project_nodes,
    read_lean_versions,
)

__all__ = [
    # Files (5)
    "create_file",
    "read_file",
    "update_file",
    "rename_file",
    "delete_file",
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
    "compile_and_backtest",
    "compile_and_optimize",
    "update_and_run_backtest",
    "edit_and_run_backtest",
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
]
