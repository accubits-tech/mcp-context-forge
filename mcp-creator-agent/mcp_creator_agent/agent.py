"""Main agent for creating Python functions from API documentation.

This module implements a multi-step LLM workflow that replaces the CrewAI
multi-agent system with direct LLM API calls for better Python version compatibility.
"""

import logging
from typing import Optional

from .llm_client import LLMClient, LLMClientError, get_llm_client
from .models import FunctionCreationRequest, FunctionCreationResponse
from .tools import execute_python, validate_python_code, install_package

logger = logging.getLogger(__name__)

# System prompts for different roles
CODE_GENERATOR_PROMPT = """You are an expert Python developer with deep knowledge of APIs,
HTTP clients, data processing, and best practices. You excel at creating clean,
well-documented, and efficient Python functions that follow PEP 8 standards.

When generating code:
1. Include proper error handling
2. Add comprehensive docstrings
3. Follow PEP 8 standards
4. Make the code production-ready
5. Use type hints where appropriate
"""

CODE_REVIEWER_PROMPT = """You are a senior Python developer and code reviewer with expertise
in testing, debugging, and code quality. You ensure functions are robust,
handle errors gracefully, and meet the specified requirements.

When reviewing code:
1. Check for syntax errors
2. Verify error handling is robust
3. Ensure the function meets requirements
4. Suggest improvements if needed
5. Return the improved code
"""

DOCUMENTATION_PROMPT = """You are a technical writer and integration specialist who excels
at creating clear documentation, usage examples, and helping users understand
how to integrate and use generated functions effectively.

When creating documentation:
1. Write clear usage examples
2. List installation requirements
3. Provide integration guidance
4. Add troubleshooting tips
"""


class FunctionCreatorAgent:
    """Agent that creates Python functions from API documentation using LLM.

    This agent performs a multi-step workflow:
    1. Generate initial code from API documentation
    2. Review and improve the generated code
    3. Create documentation and usage examples
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        verbose: bool = True,
        max_iterations: int = 3
    ):
        """Initialize the FunctionCreatorAgent.

        Args:
            llm_client: LLM client instance (defaults to global instance)
            verbose: Whether to enable verbose logging
            max_iterations: Maximum number of improvement iterations
        """
        self.llm = llm_client or get_llm_client()
        self.verbose = verbose
        self.max_iterations = max_iterations

        if not self.llm.is_configured():
            logger.warning(
                "LLM client is not configured. Set MCP_CREATOR_LLM_API_KEY to enable function creation."
            )

    def _generate_code(self, request: FunctionCreationRequest) -> str:
        """Generate initial Python code from API documentation.

        Args:
            request: Function creation request with API documentation

        Returns:
            Generated Python code as a string
        """
        prompt = f"""Analyze the following API documentation and create a Python function:

API Documentation:
{request.api_documentation}

Function Name: {request.function_name or 'auto-generated'}
Description: {request.description or 'auto-generated'}
Requirements: {', '.join(request.requirements) if request.requirements else 'auto-detected'}
Additional Context: {request.additional_context or 'none'}

Create a complete, working Python function that:
1. Implements the API functionality described
2. Includes proper error handling
3. Has comprehensive docstrings
4. Follows PEP 8 standards
5. Is ready for production use

Return ONLY the Python code, no explanations."""

        messages = [
            {"role": "system", "content": CODE_GENERATOR_PROMPT},
            {"role": "user", "content": prompt}
        ]

        if self.verbose:
            logger.info(f"Generating code for: {request.function_name or 'auto-generated'}")

        response = self.llm.chat_completion(messages, temperature=0.3)

        # Clean up code if wrapped in markdown
        code = response.strip()
        if code.startswith("```python"):
            code = code[9:]
        elif code.startswith("```"):
            code = code[3:]
        if code.endswith("```"):
            code = code[:-3]

        return code.strip()

    def _review_code(self, code: str, request: FunctionCreationRequest) -> str:
        """Review and improve the generated code.

        Args:
            code: Generated Python code
            request: Original function creation request

        Returns:
            Improved Python code
        """
        # First validate syntax
        validation_result = validate_python_code(code)
        if "Syntax error" in validation_result:
            logger.warning(f"Syntax validation failed: {validation_result}")

        prompt = f"""Review and improve the following Python function:

```python
{code}
```

Original Requirements:
- Function Name: {request.function_name or 'auto-generated'}
- Description: {request.description or 'auto-generated'}
- Requirements: {', '.join(request.requirements) if request.requirements else 'auto-detected'}

Test Examples: {request.test_examples if request.test_examples else 'Create basic tests'}

Validation Result: {validation_result}

Tasks:
1. Fix any syntax errors
2. Ensure error handling is robust
3. Verify the function meets all requirements
4. Improve code quality if needed

Return ONLY the improved Python code, no explanations."""

        messages = [
            {"role": "system", "content": CODE_REVIEWER_PROMPT},
            {"role": "user", "content": prompt}
        ]

        if self.verbose:
            logger.info("Reviewing and improving generated code")

        response = self.llm.chat_completion(messages, temperature=0.2)

        # Clean up code if wrapped in markdown
        code = response.strip()
        if code.startswith("```python"):
            code = code[9:]
        elif code.startswith("```"):
            code = code[3:]
        if code.endswith("```"):
            code = code[:-3]

        return code.strip()

    def _create_documentation(self, code: str, request: FunctionCreationRequest) -> str:
        """Create documentation and usage examples for the function.

        Args:
            code: Final Python code
            request: Original function creation request

        Returns:
            Complete response with code, documentation, and examples
        """
        prompt = f"""Create comprehensive documentation for the following Python function:

```python
{code}
```

Original Request:
- Function Name: {request.function_name or 'auto-generated'}
- Description: {request.description or 'auto-generated'}

Create:
1. Clear usage examples (at least 2)
2. Installation requirements (list any pip packages needed)
3. Integration guidance
4. Troubleshooting tips

Format your response as follows:

## Function Code
[The function code]

## Usage Examples
[Examples]

## Requirements
[List of pip packages]

## Integration Guide
[How to integrate]

## Troubleshooting
[Common issues and solutions]"""

        messages = [
            {"role": "system", "content": DOCUMENTATION_PROMPT},
            {"role": "user", "content": prompt}
        ]

        if self.verbose:
            logger.info("Creating documentation and usage examples")

        return self.llm.chat_completion(messages, temperature=0.5)

    def create_function(self, request: FunctionCreationRequest) -> FunctionCreationResponse:
        """Create a Python function based on the provided API documentation.

        Args:
            request: FunctionCreationRequest containing API documentation and requirements

        Returns:
            FunctionCreationResponse containing the created function and metadata

        Raises:
            RuntimeError: If function creation fails
        """
        if not self.llm.is_configured():
            raise RuntimeError(
                "LLM client is not configured. Set MCP_CREATOR_LLM_API_KEY environment variable."
            )

        try:
            logger.info(f"Starting function creation for: {request.function_name or 'auto-generated'}")

            # Step 1: Generate initial code
            code = self._generate_code(request)

            # Step 2: Review and improve code (can iterate multiple times)
            for i in range(self.max_iterations):
                validation = validate_python_code(code)
                if "valid" in validation.lower():
                    if self.verbose:
                        logger.info(f"Code validated successfully on iteration {i + 1}")
                    break
                if self.verbose:
                    logger.info(f"Iteration {i + 1}: Improving code")
                code = self._review_code(code, request)

            # Step 3: Create documentation
            full_response = self._create_documentation(code, request)

            # Create response
            response = self._parse_result(code, full_response, request)

            logger.info(f"Successfully created function: {response.function_name}")
            return response

        except LLMClientError as e:
            logger.error(f"LLM error creating function: {e}")
            raise RuntimeError(f"Failed to create function: {e}") from e
        except Exception as e:
            logger.error(f"Error creating function: {e}")
            raise RuntimeError(f"Failed to create function: {e}") from e

    def _parse_result(
        self,
        code: str,
        full_response: str,
        request: FunctionCreationRequest
    ) -> FunctionCreationResponse:
        """Parse the creation result and create a structured response.

        Args:
            code: Final function code
            full_response: Full response including documentation
            request: Original function creation request

        Returns:
            Structured FunctionCreationResponse
        """
        function_name = request.function_name or "generated_function"

        # Extract usage example from full response
        usage_example = f"# Usage example for {function_name}\nresult = {function_name}(your_params)"
        if "## Usage Examples" in full_response:
            start = full_response.find("## Usage Examples")
            end = full_response.find("##", start + 17)
            if end > start:
                usage_example = full_response[start:end].strip()

        return FunctionCreationResponse(
            function_code=code,
            function_name=function_name,
            description=request.description or f"Function generated from API documentation",
            requirements=request.requirements or [],
            usage_example=usage_example,
            metadata={
                "source": "llm_agent",
                "api_documentation_length": len(request.api_documentation),
                "test_examples_provided": len(request.test_examples) if request.test_examples else 0,
                "full_documentation": full_response
            }
        )
