"""Tools for the MCP Creator Agent.

These functions provide utilities for code execution, validation, and package management.
They can be used standalone or called by the FunctionCreatorAgent during code generation.
"""

import logging
from typing import Optional

from e2b_code_interpreter import Sandbox

logger = logging.getLogger(__name__)


def execute_python(code: str, timeout: Optional[int] = 300) -> str:
    """Execute Python code in a sandboxed environment and return the results.

    This function creates an isolated sandbox environment using e2b-code-interpreter
    to safely execute arbitrary Python code without affecting the host system.

    Args:
        code: The Python code to execute
        timeout: Maximum execution time in seconds (default: 300)

    Returns:
        The execution result as a string, or an error message if execution fails.

    Examples:
        >>> result = execute_python("print('Hello, World!')")
        >>> print(result)
        Hello, World!

        >>> result = execute_python("2 + 2")
        >>> print(result)
        4
    """
    try:
        with Sandbox.create() as sandbox:
            execution = sandbox.run_code(code, timeout=timeout)
            return execution.text
    except Exception as e:
        logger.error(f"Error executing Python code: {e}")
        return f"Error executing code: {str(e)}"


def validate_python_code(code: str) -> str:
    """Validate Python code syntax without executing it.

    This function uses Python's built-in compile() function to check if the
    provided code has valid syntax. It does not execute the code, making it
    safe to use for validation purposes.

    Args:
        code: The Python code to validate

    Returns:
        A validation result message indicating success or failure.
        - Returns "Code syntax is valid" if the code compiles successfully
        - Returns error details if syntax errors are found

    Examples:
        >>> result = validate_python_code("def hello(): print('hi')")
        >>> print(result)
        Code syntax is valid

        >>> result = validate_python_code("def hello( print('hi')")
        >>> print(result)
        Syntax error: ...
    """
    try:
        compile(code, '<string>', 'exec')
        return "Code syntax is valid"
    except SyntaxError as e:
        return f"Syntax error: {e}"
    except Exception as e:
        return f"Validation error: {e}"


def install_package(package_name: str) -> str:
    """Install a Python package in the sandbox environment.

    This function creates a sandbox environment and installs the specified
    package using pip. The installation is isolated and does not affect
    the host system.

    Args:
        package_name: Name of the package to install (e.g., "requests", "numpy>=1.20.0")

    Returns:
        A result message indicating success or failure of the installation.

    Examples:
        >>> result = install_package("requests")
        >>> print(result)
        Successfully installed requests

        >>> result = install_package("nonexistent-package-xyz")
        >>> print(result)
        Failed to install nonexistent-package-xyz: ...

    Note:
        This function requires an active e2b API key to create sandbox environments.
        The installation is performed in an isolated environment and will be lost
        when the sandbox is destroyed.
    """
    try:
        with Sandbox.create() as sandbox:
            # Install the package using pip
            result = sandbox.run_code(f"!pip install {package_name}")
            if result.exit_code == 0:
                return f"Successfully installed {package_name}"
            else:
                return f"Failed to install {package_name}: {result.text}"
    except Exception as e:
        return f"Error installing package: {str(e)}"
