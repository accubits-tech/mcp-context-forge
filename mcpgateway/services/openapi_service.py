# -*- coding: utf-8 -*-
"""OpenAPI Service Implementation.

Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

This module implements OpenAPI specification parsing and tool generation for the MCP Gateway.
It handles:
- OpenAPI specification parsing (JSON/YAML)
- Endpoint extraction and analysis
- Tool schema generation
- Authentication mapping
- Parameter conversion
"""

# Standard
import json
import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin, urlparse

# Third-Party
import yaml
from openapi_spec_validator import validate_spec
from pydantic import ValidationError

# First-Party
from mcpgateway.schemas import ToolCreate
from mcpgateway.services.logging_service import LoggingService

# Initialize logging service
logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


class OpenAPIError(Exception):
    """Base class for OpenAPI-related errors."""


class OpenAPIValidationError(OpenAPIError):
    """Raised when OpenAPI specification validation fails."""


class OpenAPIParsingError(OpenAPIError):
    """Raised when OpenAPI parsing fails."""


class OpenAPIService:
    """Service for parsing OpenAPI specifications and generating tools."""

    def __init__(self):
        """Initialize the OpenAPI service."""
        self.supported_versions = ["3.0.0", "3.0.1", "3.0.2", "3.0.3", "3.1.0"]

    async def parse_openapi_spec(self, spec_content: Union[str, Dict[str, Any]], content_type: str = "json") -> Dict[str, Any]:
        """Parse OpenAPI specification from string or dict.

        Args:
            spec_content: OpenAPI specification content
            content_type: Content type - 'json' or 'yaml'

        Returns:
            Parsed OpenAPI specification dictionary

        Raises:
            OpenAPIParsingError: If parsing fails
            OpenAPIValidationError: If validation fails
        """
        try:
            if isinstance(spec_content, str):
                if content_type.lower() == "yaml":
                    spec = yaml.safe_load(spec_content)
                else:
                    spec = json.loads(spec_content)
            else:
                spec = spec_content

            # Validate OpenAPI specification
            await self._validate_openapi_spec(spec)
            logger.info(f"Successfully parsed OpenAPI spec with {len(spec.get('paths', {}))} endpoints")
            
            return spec

        except yaml.YAMLError as e:
            raise OpenAPIParsingError(f"Failed to parse YAML: {str(e)}")
        except json.JSONDecodeError as e:
            raise OpenAPIParsingError(f"Failed to parse JSON: {str(e)}")
        except Exception as e:
            raise OpenAPIParsingError(f"Failed to parse OpenAPI specification: {str(e)}")

    async def _validate_openapi_spec(self, spec: Dict[str, Any]) -> None:
        """Validate OpenAPI specification.

        Args:
            spec: OpenAPI specification dictionary

        Raises:
            OpenAPIValidationError: If validation fails
        """
        try:
            # Check OpenAPI version
            version = spec.get("openapi", "")
            if not version:
                raise OpenAPIValidationError("Missing 'openapi' version field")
            
            if version not in self.supported_versions:
                logger.warning(f"OpenAPI version {version} may not be fully supported")

            # Use openapi-spec-validator to validate
            validate_spec(spec)
            
        except Exception as e:
            raise OpenAPIValidationError(f"OpenAPI specification validation failed: {str(e)}")

    async def generate_tools_from_spec(
        self, 
        spec: Dict[str, Any], 
        base_url: Optional[str] = None,
        gateway_id: Optional[str] = None,
        tags: Optional[List[str]] = None
    ) -> List[ToolCreate]:
        """Generate MCP Gateway tools from OpenAPI specification.

        Args:
            spec: Parsed OpenAPI specification
            base_url: Base URL for the API (overrides servers in spec)
            gateway_id: Gateway ID to associate tools with
            tags: Additional tags for generated tools

        Returns:
            List of ToolCreate objects

        Raises:
            OpenAPIParsingError: If tool generation fails
        """
        try:
            tools = []
            
            # Get base URL
            if not base_url:
                base_url = self._get_base_url(spec)
                logger.info(f"Extracted base URL from OpenAPI spec: {base_url}")
            
            # Validate base URL has proper scheme
            if not base_url.startswith(('http://', 'https://', 'ws://', 'wss://')):
                error_msg = (
                    f"Base URL '{base_url}' does not have a valid scheme. "
                    "Please provide a base_url parameter with a valid scheme "
                    "(http://, https://, ws://, or wss://) when calling this method."
                )
                logger.error(error_msg)
                raise OpenAPIParsingError(error_msg)
            
            # Get global security schemes
            security_schemes = spec.get("components", {}).get("securitySchemes", {})
            global_security = spec.get("security", [])
            
            # Parse paths and operations
            paths = spec.get("paths", {})
            for path, path_item in paths.items():
                if not isinstance(path_item, dict):
                    continue
                
                for method, operation in path_item.items():
                    if method.startswith("x-") or not isinstance(operation, dict):
                        continue
                    
                    if method.upper() not in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                        continue
                    
                    tool = await self._create_tool_from_operation(
                        path=path,
                        method=method.upper(),
                        operation=operation,
                        base_url=base_url,
                        security_schemes=security_schemes,
                        global_security=global_security,
                        gateway_id=gateway_id,
                        additional_tags=tags or []
                    )
                    
                    if tool:
                        tools.append(tool)
            
            logger.info(f"Generated {len(tools)} tools from OpenAPI specification")
            return tools
            
        except Exception as e:
            raise OpenAPIParsingError(f"Failed to generate tools: {str(e)}")

    def _get_base_url(self, spec: Dict[str, Any]) -> str:
        """Extract base URL from OpenAPI specification.

        Args:
            spec: OpenAPI specification

        Returns:
            Base URL for the API
        """
        servers = spec.get("servers", [])
        if servers and isinstance(servers[0], dict):
            server_url = servers[0].get("url", "")
            if server_url:
                # Check if the server URL is relative (no scheme)
                if not server_url.startswith(('http://', 'https://', 'ws://', 'wss://')):
                    # If it starts with //, it's protocol-relative - add https
                    if server_url.startswith('//'):
                        return f"https:{server_url}"
                    # If it starts with /, it's host-relative - use localhost
                    elif server_url.startswith('/'):
                        return f"http://localhost{server_url}"
                    # Otherwise, assume it's a hostname without protocol
                    else:
                        return f"https://{server_url}"
                return server_url
        
        # Fallback to localhost for local development
        return "http://localhost"

    async def _create_tool_from_operation(
        self,
        path: str,
        method: str,
        operation: Dict[str, Any],
        base_url: str,
        security_schemes: Dict[str, Any],
        global_security: List[Dict[str, Any]],
        gateway_id: Optional[str],
        additional_tags: List[str]
    ) -> Optional[ToolCreate]:
        """Create a tool from an OpenAPI operation.

        Args:
            path: API path
            method: HTTP method
            operation: OpenAPI operation object
            base_url: Base URL for the API
            security_schemes: Security schemes from components
            global_security: Global security requirements
            gateway_id: Gateway ID
            additional_tags: Additional tags

        Returns:
            ToolCreate object or None if creation fails
        """
        try:
            # Generate tool name
            operation_id = operation.get("operationId")
            if not operation_id:
                # Generate operation ID from path and method
                operation_id = self._generate_operation_id(path, method)
            
            # Sanitize tool name
            tool_name = re.sub(r'[^a-zA-Z0-9_]', '_', operation_id)
            
            # Build full URL
            full_url = urljoin(base_url.rstrip('/') + '/', path.lstrip('/'))
            
            # Validate that the constructed URL is absolute and has a valid scheme
            if not full_url.startswith(('http://', 'https://', 'ws://', 'wss://')):
                logger.error(f"Constructed URL '{full_url}' for operation {method} {path} does not have a valid scheme")
                # Try to construct a proper URL with localhost fallback
                if path.startswith('/'):
                    full_url = f"http://localhost{path}"
                else:
                    full_url = f"http://localhost/{path}"
                logger.info(f"Using fallback URL: {full_url}")
            
            # Generate description
            description = operation.get("summary") or operation.get("description") or f"{method} {path}"
            
            # Generate input schema
            input_schema = await self._generate_input_schema(operation, path)
            
            # Extract authentication
            auth = await self._extract_auth_from_operation(operation, security_schemes, global_security)
            
            # Generate tags
            op_tags = operation.get("tags", [])
            all_tags = additional_tags + op_tags + ["openapi", "auto-generated"]
            
            # Create annotations
            annotations = {
                "title": operation.get("summary", tool_name),
                "openapi_path": path,
                "openapi_method": method,
                "openapi_operation_id": operation_id,
                "destructiveHint": method in ["DELETE", "POST", "PUT", "PATCH"],
                "idempotentHint": method in ["GET", "PUT", "DELETE"]
            }
            
            if operation.get("deprecated"):
                annotations["deprecated"] = True
            
            tool = ToolCreate(
                name=tool_name,
                url=full_url,
                description=description,
                integration_type="REST",
                request_type=method,
                input_schema=input_schema,
                annotations=annotations,
                auth=auth,
                gateway_id=gateway_id,
                tags=all_tags
            )
            
            return tool
            
        except Exception as e:
            logger.error(f"Failed to create tool from operation {method} {path}: {str(e)}")
            return None

    def _generate_operation_id(self, path: str, method: str) -> str:
        """Generate operation ID from path and method.

        Args:
            path: API path
            method: HTTP method

        Returns:
            Generated operation ID
        """
        # Remove path parameters and convert to camelCase
        clean_path = re.sub(r'\{[^}]+\}', '', path)
        path_parts = [part for part in clean_path.split('/') if part]
        
        # Convert to camelCase
        if path_parts:
            operation_id = method.lower() + ''.join(word.capitalize() for word in path_parts)
        else:
            operation_id = method.lower() + 'Root'
        
        return operation_id

    async def _generate_input_schema(self, operation: Dict[str, Any], path: str) -> Dict[str, Any]:
        """Generate input schema for the operation.

        Args:
            operation: OpenAPI operation
            path: API path

        Returns:
            JSON schema for tool input
        """
        schema = {
            "type": "object",
            "properties": {},
            "required": []
        }
        
        # Handle parameters
        parameters = operation.get("parameters", [])
        for param in parameters:
            if not isinstance(param, dict):
                continue
            
            param_name = param.get("name")
            if not param_name:
                continue
            
            param_schema = param.get("schema", {"type": "string"})
            param_description = param.get("description", "")
            param_in = param.get("in", "query")
            
            # Add parameter location to description
            if param_description:
                param_description += f" ({param_in} parameter)"
            else:
                param_description = f"{param_in} parameter"
            
            schema["properties"][param_name] = {
                **param_schema,
                "description": param_description
            }
            
            if param.get("required", False):
                schema["required"].append(param_name)
        
        # Handle request body
        request_body = operation.get("requestBody")
        if request_body:
            content = request_body.get("content", {})
            
            # Look for JSON content
            json_content = content.get("application/json") or content.get("application/json; charset=utf-8")
            if json_content:
                body_schema = json_content.get("schema")
                if body_schema:
                    # If it's an object, merge properties
                    if body_schema.get("type") == "object":
                        body_props = body_schema.get("properties", {})
                        schema["properties"].update(body_props)
                        
                        # Add required fields
                        body_required = body_schema.get("required", [])
                        schema["required"].extend(body_required)
                    else:
                        # Add as a single 'body' parameter
                        schema["properties"]["body"] = {
                            **body_schema,
                            "description": "Request body"
                        }
                        
                        if request_body.get("required", False):
                            schema["required"].append("body")
        
        # Extract path parameters
        path_params = re.findall(r'\{([^}]+)\}', path)
        for param_name in path_params:
            if param_name not in schema["properties"]:
                schema["properties"][param_name] = {
                    "type": "string",
                    "description": f"Path parameter: {param_name}"
                }
                schema["required"].append(param_name)
        
        return schema

    async def _extract_auth_from_operation(
        self, 
        operation: Dict[str, Any], 
        security_schemes: Dict[str, Any], 
        global_security: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Extract authentication configuration from operation.

        Args:
            operation: OpenAPI operation
            security_schemes: Available security schemes
            global_security: Global security requirements

        Returns:
            Authentication configuration or None
        """
        # Check operation-level security first
        security = operation.get("security", global_security)
        
        if not security:
            return None
        
        # Take the first security requirement
        for security_req in security:
            if not security_req:  # Empty security requirement means no auth
                continue
                
            # Get the first security scheme
            scheme_name = next(iter(security_req.keys()), None)
            if not scheme_name or scheme_name not in security_schemes:
                continue
            
            scheme = security_schemes[scheme_name]
            scheme_type = scheme.get("type", "").lower()
            
            if scheme_type == "http":
                http_scheme = scheme.get("scheme", "").lower()
                if http_scheme == "bearer":
                    return {
                        "auth_type": "bearer",
                        "auth_value": "REPLACE_WITH_BEARER_TOKEN"
                    }
                elif http_scheme == "basic":
                    return {
                        "auth_type": "basic", 
                        "username": "REPLACE_WITH_USERNAME",
                        "password": "REPLACE_WITH_PASSWORD"
                    }
            elif scheme_type == "apikey":
                header_name = scheme.get("name", "X-API-Key")
                return {
                    "auth_type": "authheaders",
                    "auth_header_key": header_name,
                    "auth_header_value": "REPLACE_WITH_API_KEY"
                }
        
        return None

    async def preview_tools(self, spec_content: Union[str, Dict[str, Any]], content_type: str = "json") -> List[Dict[str, Any]]:
        """Preview tools that would be generated from OpenAPI spec without creating them.

        Args:
            spec_content: OpenAPI specification content
            content_type: Content type - 'json' or 'yaml'

        Returns:
            List of tool preview objects

        Raises:
            OpenAPIParsingError: If parsing fails
            OpenAPIValidationError: If validation fails
        """
        spec = await self.parse_openapi_spec(spec_content, content_type)
        tools = await self.generate_tools_from_spec(spec)
        
        # Convert to preview format
        previews = []
        for tool in tools:
            preview = {
                "name": tool.name,
                "url": str(tool.url),
                "description": tool.description,
                "method": tool.request_type,
                "path": tool.annotations.get("openapi_path", ""),
                "operation_id": tool.annotations.get("openapi_operation_id", ""),
                "tags": tool.tags,
                "requires_auth": tool.auth is not None,
                "auth_type": tool.auth.auth_type if tool.auth else None,
                "parameter_count": len(tool.input_schema.get("properties", {})),
                "required_parameters": tool.input_schema.get("required", [])
            }
            previews.append(preview)
        
        return previews