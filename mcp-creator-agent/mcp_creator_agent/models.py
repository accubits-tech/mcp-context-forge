"""Pydantic models for the MCP Creator Agent."""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class FunctionCreationRequest(BaseModel):
    """Request model for creating a Python function from API documentation."""
    
    api_documentation: str = Field(
        ..., 
        description="The API documentation or specification to create a function from"
    )
    
    function_name: Optional[str] = Field(
        None,
        description="Optional custom name for the function. If not provided, a name will be generated."
    )
    
    description: Optional[str] = Field(
        None,
        description="Optional description of what the function should do"
    )
    
    requirements: Optional[List[str]] = Field(
        default_factory=list,
        description="List of required Python packages for the function"
    )
    
    test_examples: Optional[List[Dict[str, Any]]] = Field(
        default_factory=list,
        description="Optional test examples with input/output pairs"
    )
    
    additional_context: Optional[str] = Field(
        None,
        description="Any additional context or requirements for the function"
    )


class FunctionCreationResponse(BaseModel):
    """Response model containing the created Python function."""
    
    function_code: str = Field(
        ..., 
        description="The complete Python function code"
    )
    
    function_name: str = Field(
        ..., 
        description="The name of the created function"
    )
    
    description: str = Field(
        ..., 
        description="Description of what the function does"
    )
    
    requirements: List[str] = Field(
        default_factory=list,
        description="List of required Python packages"
    )
    
    usage_example: str = Field(
        ..., 
        description="Example of how to use the function"
    )
    
    test_code: Optional[str] = Field(
        None,
        description="Optional test code for the function"
    )
    
    execution_result: Optional[str] = Field(
        None,
        description="Result of testing the function if test examples were provided"
    )
    
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata about the function creation process"
    )
