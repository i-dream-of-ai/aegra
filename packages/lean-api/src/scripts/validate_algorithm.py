#!/usr/bin/env python3
"""
Algorithm Validation Script for LEAN

This script validates Python algorithms using Python's AST parser.
It runs inside the LEAN Docker container where AlgorithmImports is available.

Usage:
    python validate_algorithm.py <project_dir>

Output (JSON):
    {
        "success": true/false,
        "errors": ["error1", "error2"],
        "files_checked": ["main.py", "helper.py"]
    }
"""

import ast
import json
import os
import sys
from pathlib import Path


def validate_python_syntax(filepath: str, content: str) -> list[str]:
    """Validate Python syntax using AST parser."""
    errors = []
    try:
        ast.parse(content, filename=filepath)
    except SyntaxError as e:
        errors.append(f"{filepath}:{e.lineno}: SyntaxError: {e.msg}")
    return errors


def validate_main_file(filepath: str, content: str) -> list[str]:
    """Validate main.py has required LEAN structure."""
    errors = []

    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        # Syntax errors are caught by validate_python_syntax
        return errors

    # Check for QCAlgorithm class
    has_qc_algorithm = False
    has_initialize = False

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            # Check if inherits from QCAlgorithm
            for base in node.bases:
                base_name = None
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr

                if base_name == 'QCAlgorithm':
                    has_qc_algorithm = True

                    # Check for Initialize method
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            if item.name.lower() == 'initialize':
                                has_initialize = True
                                break

    if not has_qc_algorithm:
        errors.append(f"{filepath}: Algorithm must inherit from QCAlgorithm")

    if has_qc_algorithm and not has_initialize:
        errors.append(f"{filepath}: Algorithm must have an Initialize(self) method")

    # Check for AlgorithmImports
    has_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and 'AlgorithmImports' in node.module:
                has_import = True
                break
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if 'AlgorithmImports' in alias.name:
                    has_import = True
                    break

    if not has_import:
        errors.append(f"{filepath}: Missing required import: from AlgorithmImports import *")

    return errors


def validate_project(project_dir: str) -> dict:
    """Validate all Python files in a project directory."""
    project_path = Path(project_dir)

    if not project_path.exists():
        return {
            "success": False,
            "errors": [f"Project directory not found: {project_dir}"],
            "files_checked": []
        }

    all_errors = []
    files_checked = []
    main_file = None

    # Find all Python files
    py_files = list(project_path.glob("*.py"))

    if not py_files:
        return {
            "success": False,
            "errors": ["No Python files found in project"],
            "files_checked": []
        }

    # Find main.py
    for f in py_files:
        if f.name.lower() == 'main.py':
            main_file = f
            break

    if not main_file:
        return {
            "success": False,
            "errors": ["main.py not found in project"],
            "files_checked": []
        }

    # Validate all Python files for syntax
    for py_file in py_files:
        files_checked.append(py_file.name)
        content = py_file.read_text()

        # Syntax check for all files
        syntax_errors = validate_python_syntax(py_file.name, content)
        all_errors.extend(syntax_errors)

        # Additional checks only for main.py
        if py_file == main_file:
            main_errors = validate_main_file(py_file.name, content)
            all_errors.extend(main_errors)

    return {
        "success": len(all_errors) == 0,
        "errors": all_errors,
        "files_checked": files_checked
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({
            "success": False,
            "errors": ["Usage: python validate_algorithm.py <project_dir>"],
            "files_checked": []
        }))
        sys.exit(1)

    project_dir = sys.argv[1]
    result = validate_project(project_dir)
    print(json.dumps(result))
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
