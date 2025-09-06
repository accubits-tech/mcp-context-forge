"""Tools for the MCP Creator Agent."""

import logging
from typing import Optional
from crewai.tools import tool
from e2b_code_interpreter import Sandbox

logger = logging.getLogger(__name__)


@tool("Python Interpreter")
def execute_python(code: str, timeout: Optional[int] = 300) -> str:
    """
    Execute Python code and return the results.
    
    Args:
        code: The Python code to execute
        timeout: Maximum execution time in seconds (default: 300)
    
    Returns:
        The execution result as a string
    """
    try:
        with Sandbox.create() as sandbox:
            execution = sandbox.run_code(code, timeout=timeout)
            return execution.text
    except Exception as e:
        logger.error(f"Error executing Python code: {e}")
        return f"Error executing code: {str(e)}"


@tool("Code Validator")
def validate_python_code(code: str) -> str:
    """
    Validate Python code syntax without executing it.
    
    Args:
        code: The Python code to validate
    
    Returns:
        Validation result message
    """
    try:
        compile(code, '<string>', 'exec')
        return "✅ Code syntax is valid"
    except SyntaxError as e:
        return f"❌ Syntax error: {e}"
    except Exception as e:
        return f"❌ Validation error: {e}"


@tool("Package Installer")
def install_package(package_name: str) -> str:
    """
    Install a Python package in the sandbox environment.
    
    Args:
        package_name: Name of the package to install
    
    Returns:
        Installation result message
    """
    try:
        with Sandbox.create() as sandbox:
            # Install the package
            result = sandbox.run_code(f"!pip install {package_name}")
            if result.exit_code == 0:
                return f"✅ Successfully installed {package_name}"
            else:
                return f"❌ Failed to install {package_name}: {result.text}"
    except Exception as e:
        return f"❌ Error installing package: {str(e)}"
