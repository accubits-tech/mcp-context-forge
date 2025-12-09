# -*- coding: utf-8 -*-
"""OpenAPI Analysis Agent.

Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

This module implements an agent for analyzing OpenAPI specifications and
generating intelligent tool descriptions and configurations for the MCP Gateway.
Uses direct LLM API calls instead of CrewAI for better Python version compatibility.
"""

# Standard
import json
from typing import Any, Dict, List, Optional

# First-Party
from mcpgateway.services.llm_service import LLMService, LLMAPIError, LLMConfigurationError, get_llm_service
from mcpgateway.services.logging_service import LoggingService

# Initialize logging service
logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

# System prompts for different analysis tasks
OPENAPI_ANALYST_SYSTEM_PROMPT = """You are an expert API analyst with deep knowledge of OpenAPI/Swagger specifications,
API documentation formats (PDF, HTML, Markdown, plain text), and REST API design patterns.
You excel at understanding API endpoints from various documentation sources, parameters, authentication methods,
and generating clear, accurate descriptions for tools. You pay special attention to security considerations,
parameter validation, proper HTTP method usage, and can intelligently extract API information from
incomplete or unstructured documentation.

Always respond with valid JSON when asked to provide structured output."""


class OpenAPIAgent:
    """Agent for OpenAPI specification and API documentation analysis with tool generation.

    This agent uses direct LLM API calls to analyze OpenAPI specifications and API documentation,
    providing intelligent tool descriptions, security analysis, and configuration recommendations.
    """

    def __init__(self, llm_service: Optional[LLMService] = None):
        """Initialize the OpenAPI agent.

        Args:
            llm_service: Optional LLMService instance. If not provided, uses the global instance.
        """
        self.llm_service = llm_service or get_llm_service()

        if not self.llm_service.is_configured():
            logger.warning(
                "LLM service is not configured. Set LLM_API_KEY environment variable to enable AI-enhanced analysis."
            )

    def _is_ai_available(self) -> bool:
        """Check if AI-enhanced analysis is available."""
        return self.llm_service.is_configured()

    async def analyze_openapi_spec(
        self,
        spec: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Analyze OpenAPI specification and provide insights for tool generation.

        Args:
            spec: Parsed OpenAPI specification
            context: Additional context for analysis

        Returns:
            Analysis results with recommendations
        """
        if not self._is_ai_available():
            logger.warning("LLM not available, returning basic analysis")
            return self._generate_basic_analysis(spec, context)

        try:
            prompt = f"""Analyze the following OpenAPI specification and provide detailed insights:

OpenAPI Spec:
{json.dumps(spec, indent=2)[:30000]}

Context: {json.dumps(context or {}, indent=2)}

Please provide a comprehensive analysis including:

1. **API Overview**: Brief description of what this API does
2. **Endpoint Analysis**: For each endpoint, analyze:
   - Purpose and functionality
   - Security requirements
   - Parameter validation needs
   - Potential risks (e.g., destructive operations)
   - Recommended tool descriptions
3. **Authentication Analysis**:
   - What authentication methods are used
   - Security considerations
   - Configuration recommendations
4. **Parameter Mapping**:
   - Complex parameter relationships
   - Validation requirements
   - Default values and constraints
5. **Tool Generation Recommendations**:
   - Suggested tool names and descriptions
   - Grouping recommendations
   - Priority/importance ratings
   - Special handling requirements

Return your response as a valid JSON object with the above sections as keys."""

            messages = [
                {"role": "system", "content": OPENAPI_ANALYST_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]

            response = await self.llm_service.chat_completion_json(messages)
            logger.info("Completed OpenAPI specification analysis with AI enhancement")
            logger.debug(f"OpenAPI analysis response keys: {list(response.keys()) if isinstance(response, dict) else type(response)}")
            return response

        except (LLMAPIError, LLMConfigurationError) as e:
            logger.error(f"Failed to analyze OpenAPI specification with AI: {e}")
            return self._generate_basic_analysis(spec, context)
        except Exception as e:
            logger.error(f"Unexpected error in OpenAPI analysis: {e}")
            return self._generate_basic_analysis(spec, context)

    def _generate_basic_analysis(self, spec: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Generate basic analysis without LLM when AI is not available.

        Args:
            spec: OpenAPI specification
            context: Optional context

        Returns:
            Basic analysis structure
        """
        info = spec.get("info", {})
        paths = spec.get("paths", {})
        security_schemes = spec.get("components", {}).get("securitySchemes", {})
        global_security = spec.get("security", [])

        endpoint_count = len([
            1 for path_item in paths.values()
            for method in path_item.keys()
            if method.upper() in ["GET", "POST", "PUT", "DELETE", "PATCH"]
        ])

        return {
            "api_overview": f"{info.get('title', 'Unknown API')} with {endpoint_count} endpoints",
            "endpoint_analysis": {
                path: {
                    method: {
                        "summary": operation.get("summary", "No summary available"),
                        "description": operation.get("description", "No description available"),
                        "requires_auth": bool(operation.get("security") or global_security),
                        "method": method.upper()
                    }
                    for method, operation in path_item.items()
                    if method.upper() in ["GET", "POST", "PUT", "DELETE", "PATCH"]
                }
                for path, path_item in paths.items()
            },
            "authentication_analysis": {
                "schemes": list(security_schemes.keys()),
                "global_security": bool(global_security),
                "scheme_count": len(security_schemes)
            },
            "parameter_mapping": {},
            "tool_generation_recommendations": {
                "total_endpoints": endpoint_count,
                "authenticated_endpoints": len([
                    1 for path_item in paths.values()
                    for method, operation in path_item.items()
                    if method.upper() in ["GET", "POST", "PUT", "DELETE", "PATCH"]
                    and (operation.get("security") or global_security)
                ])
            },
            "metadata": {
                "generated_by": "basic_analysis",
                "ai_enhanced": False
            }
        }

    async def enhance_tool_descriptions(
        self,
        tools: List[Dict[str, Any]],
        analysis: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Enhance tool descriptions using AI analysis.

        Args:
            tools: List of tool configurations
            analysis: Analysis results from analyze_openapi_spec

        Returns:
            Enhanced tool configurations
        """
        if not self._is_ai_available():
            logger.warning("LLM not available, returning tools without enhancement")
            return tools

        try:
            prompt = f"""Based on the following OpenAPI analysis and tool configurations,
enhance the tool descriptions, names, and metadata:

Analysis:
{json.dumps(analysis, indent=2)[:15000]}

Tools to enhance:
{json.dumps(tools, indent=2)[:15000]}

For each tool, improve:
1. **Name**: Make it more descriptive and user-friendly
2. **Description**: Provide clear, comprehensive description of what the tool does
3. **Tags**: Add relevant, searchable tags
4. **Annotations**: Add helpful hints and metadata
5. **Parameter descriptions**: Improve parameter descriptions for clarity

Return the enhanced tools as a JSON array, maintaining the original structure but with improved content.
Focus on making the tools more discoverable and easier to understand for end users."""

            messages = [
                {"role": "system", "content": OPENAPI_ANALYST_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]

            response = await self.llm_service.chat_completion_json(messages)

            if isinstance(response, list):
                logger.info(f"Enhanced {len(response)} tool descriptions")
                return response
            elif isinstance(response, dict) and "tools" in response:
                logger.info(f"Enhanced {len(response['tools'])} tool descriptions")
                return response["tools"]
            else:
                logger.warning("AI did not return expected format, returning original tools")
                return tools

        except (LLMAPIError, LLMConfigurationError) as e:
            logger.error(f"Failed to enhance tool descriptions: {e}")
            return tools
        except Exception as e:
            logger.error(f"Unexpected error enhancing tools: {e}")
            return tools

    async def validate_security_configuration(
        self,
        spec: Dict[str, Any],
        tools: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Validate security configuration and provide recommendations.

        Args:
            spec: OpenAPI specification
            tools: Generated tool configurations

        Returns:
            Security validation results and recommendations
        """
        if not self._is_ai_available():
            logger.warning("LLM not available, returning basic security analysis")
            return self._generate_basic_security_analysis(spec, tools)

        try:
            prompt = f"""Analyze the security configuration of this OpenAPI specification and generated tools:

OpenAPI Spec Security:
{json.dumps(spec.get('components', {}).get('securitySchemes', {}), indent=2)}

Global Security:
{json.dumps(spec.get('security', []), indent=2)}

Generated Tools:
{json.dumps([{
    'name': t.get('name'),
    'method': t.get('method'),
    'path': t.get('path'),
    'auth_type': t.get('auth_type'),
    'requires_auth': t.get('requires_auth', False)
} for t in tools], indent=2)}

Please provide a security analysis including:

1. **Security Scheme Analysis**: Evaluate the security schemes used
2. **Authentication Mapping**: Verify correct auth mapping for each endpoint
3. **Security Risks**: Identify potential security risks
4. **Recommendations**: Provide security best practices and recommendations
5. **Tool-Specific Security**: Any special security considerations for each tool

Return as a valid JSON object."""

            messages = [
                {"role": "system", "content": OPENAPI_ANALYST_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]

            response = await self.llm_service.chat_completion_json(messages)
            logger.info("Completed security configuration validation")
            return response

        except (LLMAPIError, LLMConfigurationError) as e:
            logger.error(f"Failed to validate security configuration: {e}")
            return self._generate_basic_security_analysis(spec, tools)
        except Exception as e:
            logger.error(f"Unexpected error in security validation: {e}")
            return self._generate_basic_security_analysis(spec, tools)

    def _generate_basic_security_analysis(self, spec: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate basic security analysis without LLM when AI is not available.

        Args:
            spec: OpenAPI specification
            tools: Generated tool configurations

        Returns:
            Basic security analysis structure
        """
        security_schemes = spec.get("components", {}).get("securitySchemes", {})
        global_security = spec.get("security", [])

        # Count tools by auth type
        auth_summary: Dict[str, int] = {}
        for tool in tools:
            auth_type = tool.get("auth_type", "none")
            auth_summary[auth_type] = auth_summary.get(auth_type, 0) + 1

        return {
            "security_scheme_analysis": {
                "total_schemes": len(security_schemes),
                "scheme_types": {name: scheme.get("type") for name, scheme in security_schemes.items()},
                "global_security_required": bool(global_security)
            },
            "authentication_mapping": {
                "tools_by_auth_type": auth_summary,
                "total_tools": len(tools),
                "secured_tools": len([t for t in tools if t.get("requires_auth", False)])
            },
            "security_risks": ["Basic analysis: Manual security review recommended"],
            "recommendations": [
                "Review authentication configuration manually",
                "Verify SSL/TLS usage for production",
                "Test authorization for all endpoints"
            ],
            "tool_specific_security": {},
            "metadata": {
                "generated_by": "basic_security_analysis",
                "ai_enhanced": False
            }
        }

    async def generate_comprehensive_analysis(
        self,
        spec: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Generate a comprehensive analysis of the OpenAPI specification.

        Args:
            spec: Parsed OpenAPI specification
            context: Additional context

        Returns:
            Comprehensive analysis results
        """
        try:
            logger.info("Starting comprehensive OpenAPI analysis")

            # Run the main analysis
            analysis = await self.analyze_openapi_spec(spec, context)

            # Generate basic tool configurations for security analysis
            basic_tools = []
            paths = spec.get("paths", {})
            for path, path_item in paths.items():
                for method, operation in path_item.items():
                    if method.upper() in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                        basic_tools.append({
                            "name": operation.get("operationId", f"{method}_{path}"),
                            "method": method.upper(),
                            "path": path,
                            "requires_auth": bool(operation.get("security") or spec.get("security")),
                            "auth_type": "unknown"
                        })

            # Run security validation
            security_analysis = await self.validate_security_configuration(spec, basic_tools)

            # Combine results
            comprehensive_analysis = {
                **analysis,
                "security_analysis": security_analysis,
                "metadata": {
                    "openapi_version": spec.get("openapi", "unknown"),
                    "api_title": spec.get("info", {}).get("title", "Unknown API"),
                    "api_version": spec.get("info", {}).get("version", "unknown"),
                    "endpoint_count": len([
                        1 for path_item in paths.values()
                        for method in path_item.keys()
                        if method.upper() in ["GET", "POST", "PUT", "DELETE", "PATCH"]
                    ]),
                    "security_schemes_count": len(spec.get("components", {}).get("securitySchemes", {})),
                    "ai_enhanced": self._is_ai_available()
                }
            }

            logger.info("Completed comprehensive OpenAPI analysis")
            return comprehensive_analysis

        except Exception as e:
            logger.error(f"Failed to generate comprehensive analysis: {e}")
            return {
                "error": str(e),
                "api_overview": "Analysis failed",
                "endpoint_analysis": {},
                "authentication_analysis": {},
                "parameter_mapping": {},
                "tool_generation_recommendations": {},
                "security_analysis": {},
                "metadata": {"ai_enhanced": False}
            }

    async def analyze_api_documentation(
        self,
        doc_structure: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Analyze API documentation structure and provide insights for tool generation.

        Args:
            doc_structure: Parsed API documentation structure from parser service
            context: Additional context for analysis

        Returns:
            Analysis results with recommendations
        """
        if not self._is_ai_available():
            logger.warning("LLM not available, returning basic analysis")
            return self._generate_basic_doc_analysis(doc_structure, context)

        try:
            # Truncate raw content to avoid token limits
            raw_content = doc_structure.get('raw_content', '')
            if len(raw_content) > 25000:
                raw_content = raw_content[:25000] + "\n...[truncated]..."

            prompt = f"""Analyze the following API documentation structure and provide detailed insights for tool generation:

API Documentation:
Title: {doc_structure.get('title', 'Unknown')}
Source Format: {doc_structure.get('source_format', 'unknown')}
Content Length: {doc_structure.get('content_length', 0)} characters
Detected Endpoints: {len(doc_structure.get('potential_endpoints', []))}
Authentication Methods: {doc_structure.get('authentication_info', {}).get('methods', [])}

Regex-Detected Endpoints:
{json.dumps(doc_structure.get('potential_endpoints', [])[:20], indent=2)}

Raw Documentation Content:
{raw_content}

Context: {json.dumps(context or {}, indent=2)}

Please provide a comprehensive analysis including:

1. **API Overview**:
   - What does this API appear to do based on the documentation?
   - What is the likely purpose and domain of this API?
   - Quality assessment of the documentation

2. **Endpoint Extraction**:
   - Review the raw content to identify ALL API endpoints
   - Verify the regex-detected endpoints
   - **CRITICAL: Extract any endpoints missed by the regex parser**
   - For each valid endpoint found, provide:
     - HTTP Method (GET, POST, etc.)
     - Path (e.g., /users/{{id}})
     - Description of what it does
     - Parameters (names, types, location, required status)

3. **Authentication Analysis**:
   - What authentication methods are mentioned or implied?
   - Security considerations for the detected endpoints
   - Recommended authentication setup

4. **Tool Generation Recommendations**:
   - Suggested tool names (user-friendly, descriptive)
   - Grouping and tagging recommendations
   - Priority levels for implementation

Return your response as a valid JSON object with the following keys:
- "api_overview": string
- "extracted_endpoints": List of objects with the following fields:
  - "method": REQUIRED - HTTP method (GET, POST, PUT, DELETE, PATCH). Must be one of these values.
  - "path": REQUIRED - API path (e.g., "/users/{{id}}"). Must start with "/" or be a full URL.
  - "description": REQUIRED - Clear description of what the endpoint does. If not clear from docs, infer from path/method.
  - "name": OPTIONAL - Suggested operation name (e.g., "getUserById", "createUser"). Helps with tool naming.
  - "parameters": REQUIRED - List of parameter objects, each with "name", "type", "required", "description", "in" (query/path/body)
  - "confidence": REQUIRED - Integer 1-10 rating how confident you are this endpoint exists
- "authentication_analysis": object
- "tool_generation_recommendations": object
- "data_quality_assessment": object

IMPORTANT: Every endpoint in "extracted_endpoints" MUST have valid "method", "path", and "description" fields.
Do not include endpoints where you cannot determine the path or method.

Ensure "extracted_endpoints" contains BOTH the regex-found endpoints (if valid) AND any newly discovered ones from the raw text."""

            messages = [
                {"role": "system", "content": OPENAPI_ANALYST_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]

            response = await self.llm_service.chat_completion_json(messages)
            logger.info(f"Completed API documentation analysis for {len(doc_structure.get('potential_endpoints', []))} endpoints")
            return response

        except (LLMAPIError, LLMConfigurationError) as e:
            logger.error(f"Failed to analyze API documentation: {e}")
            return self._generate_basic_doc_analysis(doc_structure, context)
        except Exception as e:
            logger.error(f"Unexpected error in API documentation analysis: {e}")
            return self._generate_basic_doc_analysis(doc_structure, context)

    def _generate_basic_doc_analysis(self, doc_structure: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Generate basic analysis without LLM when AI is not available.

        Args:
            doc_structure: API documentation structure
            context: Optional context

        Returns:
            Basic analysis structure
        """
        endpoints = doc_structure.get('potential_endpoints', [])
        auth_info = doc_structure.get('authentication_info', {})
        source_format = doc_structure.get('source_format', 'unknown')

        # Basic endpoint analysis
        endpoint_analysis = {}
        for endpoint in endpoints:
            path = endpoint.get('path', '')
            method = endpoint.get('method', 'GET')

            endpoint_analysis[f"{method} {path}"] = {
                "method": method,
                "path": path,
                "confidence": 7 if method != 'GET' else 8,
                "inferred_purpose": self._infer_endpoint_purpose(method, path),
                "context_available": bool(endpoint.get('context', '').strip())
            }

        # Basic parameter extraction from detected parameters
        parameter_analysis = {}
        for param in doc_structure.get('parameters', []):
            param_name = param.get('name', '')
            if param_name:
                parameter_analysis[param_name] = {
                    "type": param.get('type', 'query'),
                    "description": param.get('description', f'Parameter: {param_name}'),
                    "confidence": 6
                }

        # Prepare extracted endpoints list for consistency
        extracted_endpoints = []
        for endpoint in endpoints:
            extracted_endpoints.append({
                "method": endpoint.get('method', 'GET'),
                "path": endpoint.get('path', ''),
                "description": self._infer_endpoint_purpose(endpoint.get('method', 'GET'), endpoint.get('path', '')),
                "parameters": [],
                "confidence": 7
            })

        return {
            "api_overview": f"{doc_structure.get('title', 'API Documentation')} - {len(endpoints)} endpoints detected from {source_format} format",
            "extracted_endpoints": extracted_endpoints,
            "endpoint_analysis": endpoint_analysis,
            "authentication_analysis": {
                "detected_methods": auth_info.get('methods', []),
                "confidence": 5 if auth_info.get('methods') else 3,
                "recommendations": ["Manual authentication review required"]
            },
            "parameter_extraction": parameter_analysis,
            "tool_generation_recommendations": {
                "total_endpoints": len(endpoints),
                "high_confidence_endpoints": len([e for e in endpoints if e.get('context')]),
                "recommended_for_generation": len([e for e in endpoints if len(e.get('path', '')) > 1])
            },
            "data_quality_assessment": {
                "confidence": "medium" if len(endpoints) > 0 else "low",
                "source_format": source_format,
                "content_length": doc_structure.get('content_length', 0),
                "issues": self._assess_basic_quality_issues(doc_structure),
                "ai_enhanced": False
            },
            "metadata": {
                "generated_by": "basic_doc_analysis",
                "ai_enhanced": False,
                "source_format": source_format
            }
        }

    def _infer_endpoint_purpose(self, method: str, path: str) -> str:
        """Infer the purpose of an endpoint based on method and path.

        Args:
            method: HTTP method
            path: API path

        Returns:
            Inferred purpose description
        """
        method_purposes = {
            'GET': 'Retrieve',
            'POST': 'Create or process',
            'PUT': 'Update or replace',
            'DELETE': 'Delete',
            'PATCH': 'Partially update'
        }

        action = method_purposes.get(method.upper(), 'Interact with')

        # Extract resource from path
        path_parts = [part for part in path.split('/') if part and not part.startswith('{')]
        if path_parts:
            resource = path_parts[-1].replace('_', ' ').replace('-', ' ')
            return f"{action} {resource}"

        return f"{action} resource"

    def _assess_basic_quality_issues(self, doc_structure: Dict[str, Any]) -> List[str]:
        """Assess basic quality issues with the documentation.

        Args:
            doc_structure: Parsed documentation structure

        Returns:
            List of quality issues
        """
        issues = []

        endpoints = doc_structure.get('potential_endpoints', [])
        if not endpoints:
            issues.append("No API endpoints detected")

        # Check for endpoints with minimal context
        low_context_count = len([e for e in endpoints if len(e.get('context', '').strip()) < 50])
        if low_context_count > len(endpoints) / 2:
            issues.append("Many endpoints lack sufficient context for accurate analysis")

        # Check for authentication information
        if not doc_structure.get('authentication_info', {}).get('methods'):
            issues.append("No authentication information detected")

        # Check content length
        content_length = doc_structure.get('content_length', 0)
        if content_length < 1000:
            issues.append("Documentation appears to be very short - may be incomplete")

        # Check for parameter information
        if not doc_structure.get('parameters'):
            issues.append("No parameter information detected")

        return issues

    async def enhance_tools_from_documentation(
        self,
        tools: List[Dict[str, Any]],
        analysis: Dict[str, Any],
        doc_structure: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Enhance tool configurations using AI analysis of documentation.

        Args:
            tools: List of basic tool configurations
            analysis: Analysis results from analyze_api_documentation
            doc_structure: Original documentation structure

        Returns:
            Enhanced tool configurations
        """
        if not self._is_ai_available():
            logger.warning("LLM not available, returning tools with basic enhancement")
            return self._enhance_tools_basic(tools, analysis, doc_structure)

        try:
            prompt = f"""Based on the API documentation analysis, enhance these tool configurations:

Original Analysis:
{json.dumps(analysis, indent=2)[:15000]}

Tools to Enhance:
{json.dumps(tools, indent=2)[:15000]}

Original Documentation Context:
{json.dumps({"title": doc_structure.get("title"), "source_format": doc_structure.get("source_format"), "auth_info": doc_structure.get("authentication_info")}, indent=2)}

For each tool, improve:

1. **Name Enhancement**:
   - Make names more descriptive and user-friendly
   - Follow camelCase convention
   - Avoid generic names like "getRootPath"

2. **Description Enhancement**:
   - Write clear, comprehensive descriptions
   - Explain what the endpoint does and returns
   - Include important usage notes or warnings

3. **Parameter Enhancement**:
   - Add detailed parameter descriptions
   - Suggest appropriate types and constraints
   - Indicate required vs optional parameters clearly

4. **Tag Enhancement**:
   - Add relevant, searchable tags
   - Include functional tags (e.g., "user-management", "data-retrieval")
   - Add security-related tags where appropriate

5. **Authentication Setup**:
   - Configure appropriate authentication based on analysis
   - Set placeholder values that are clearly marked for replacement

6. **Annotations Enhancement**:
   - Add helpful metadata and hints
   - Include confidence ratings from analysis
   - Add warnings for destructive operations

Return the enhanced tools as a JSON array maintaining the original structure.
Focus on making tools more discoverable, understandable, and safe to use."""

            messages = [
                {"role": "system", "content": OPENAPI_ANALYST_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]

            response = await self.llm_service.chat_completion_json(messages)
            logger.debug(f"Tool enhancement response type: {type(response).__name__}, keys: {list(response.keys()) if isinstance(response, dict) else 'N/A'}")

            if isinstance(response, list):
                logger.info(f"Enhanced {len(response)} tool descriptions using API documentation analysis")
                return response
            elif isinstance(response, dict) and "tools" in response:
                logger.info(f"Enhanced {len(response['tools'])} tool descriptions")
                return response["tools"]
            elif isinstance(response, dict) and "enhanced_tools" in response:
                # Handle alternative key name
                logger.info(f"Enhanced {len(response['enhanced_tools'])} tool descriptions (from enhanced_tools key)")
                return response["enhanced_tools"]
            else:
                logger.warning(f"AI did not return expected format (type={type(response).__name__}, keys={list(response.keys()) if isinstance(response, dict) else 'N/A'}), returning basic enhanced tools")
                return self._enhance_tools_basic(tools, analysis, doc_structure)

        except (LLMAPIError, LLMConfigurationError) as e:
            logger.error(f"Failed to enhance tools from documentation: {e}")
            return self._enhance_tools_basic(tools, analysis, doc_structure)
        except Exception as e:
            logger.error(f"Unexpected error enhancing tools: {e}")
            return self._enhance_tools_basic(tools, analysis, doc_structure)

    def _enhance_tools_basic(
        self,
        tools: List[Dict[str, Any]],
        analysis: Dict[str, Any],
        doc_structure: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Basic tool enhancement without AI when agent is not available.

        Args:
            tools: Original tools
            analysis: Analysis results
            doc_structure: Documentation structure

        Returns:
            Basically enhanced tools
        """
        enhanced_tools = []
        endpoint_analysis = analysis.get('endpoint_analysis', {})

        for tool in tools:
            enhanced_tool = tool.copy()

            # Try to find matching endpoint analysis
            method = tool.get('request_type', 'GET')
            path = tool.get('annotations', {}).get('api_doc_path', '')
            endpoint_key = f"{method} {path}"

            endpoint_info = endpoint_analysis.get(endpoint_key, {})

            # Enhance description if we have analysis
            if endpoint_info.get('inferred_purpose'):
                enhanced_tool['description'] = f"{endpoint_info['inferred_purpose']} - {enhanced_tool.get('description', '')}"

            # Add confidence annotation
            confidence = endpoint_info.get('confidence', 5)
            if 'annotations' not in enhanced_tool:
                enhanced_tool['annotations'] = {}
            enhanced_tool['annotations']['confidence_rating'] = confidence
            enhanced_tool['annotations']['ai_enhanced'] = False
            enhanced_tool['annotations']['enhancement_source'] = 'basic_analysis'

            # Add source format information
            enhanced_tool['annotations']['documentation_format'] = doc_structure.get('source_format', 'unknown')

            enhanced_tools.append(enhanced_tool)

        return enhanced_tools

    async def extract_tools_from_raw_content(
        self,
        raw_content: str,
        base_url: str,
        source_info: Optional[Dict[str, Any]] = None,
        chunk_size: int = 20000
    ) -> List[Dict[str, Any]]:
        """Extract tool definitions directly from raw API documentation content.

        This method passes the raw documentation content directly to the LLM,
        which analyzes it and returns complete tool definitions. For large documents,
        it automatically chunks the content and makes multiple LLM calls.

        Args:
            raw_content: Raw API documentation content (text, HTML, markdown, etc.)
            base_url: Base URL for the API endpoints
            source_info: Optional metadata about the source (filename, url, format)
            chunk_size: Maximum characters per LLM call (default 20000)

        Returns:
            List of tool definition dictionaries ready for ToolCreate
        """
        if not self._is_ai_available():
            logger.warning("LLM not available for tool extraction")
            return []

        source_info = source_info or {}
        all_tools = []

        # Split content into chunks if too large
        content_chunks = self._chunk_content(raw_content, chunk_size)
        total_chunks = len(content_chunks)

        logger.info(f"Extracting tools from {len(raw_content)} chars of content ({total_chunks} chunk(s))")

        for chunk_idx, chunk in enumerate(content_chunks):
            try:
                chunk_tools = await self._extract_tools_from_chunk(
                    chunk=chunk,
                    base_url=base_url,
                    source_info=source_info,
                    chunk_idx=chunk_idx,
                    total_chunks=total_chunks
                )
                all_tools.extend(chunk_tools)
                logger.info(f"Extracted {len(chunk_tools)} tools from chunk {chunk_idx + 1}/{total_chunks}")

            except Exception as e:
                logger.error(f"Failed to extract tools from chunk {chunk_idx + 1}: {e}")
                continue

        # Deduplicate tools by name
        seen_names = set()
        unique_tools = []
        for tool in all_tools:
            name = tool.get('name', '')
            if name and name not in seen_names:
                seen_names.add(name)
                unique_tools.append(tool)

        logger.info(f"Total unique tools extracted: {len(unique_tools)}")
        return unique_tools

    def _chunk_content(self, content: str, chunk_size: int) -> List[str]:
        """Split content into chunks, trying to break at natural boundaries.

        Args:
            content: Raw content to chunk
            chunk_size: Maximum size per chunk

        Returns:
            List of content chunks
        """
        if len(content) <= chunk_size:
            return [content]

        chunks = []
        current_pos = 0

        while current_pos < len(content):
            # Find the end position for this chunk
            end_pos = min(current_pos + chunk_size, len(content))

            # If not at the end, try to break at a natural boundary
            if end_pos < len(content):
                # Look for paragraph breaks, section headers, or newlines
                for delimiter in ['\n\n', '\n#', '\n##', '\n###', '\n---', '\n***', '\n']:
                    # Search backwards from end_pos for a delimiter
                    search_start = max(current_pos + chunk_size // 2, current_pos)
                    last_delimiter = content.rfind(delimiter, search_start, end_pos)
                    if last_delimiter > current_pos:
                        end_pos = last_delimiter + len(delimiter)
                        break

            chunk = content[current_pos:end_pos].strip()
            if chunk:
                chunks.append(chunk)

            current_pos = end_pos

        return chunks

    async def _extract_tools_from_chunk(
        self,
        chunk: str,
        base_url: str,
        source_info: Dict[str, Any],
        chunk_idx: int,
        total_chunks: int
    ) -> List[Dict[str, Any]]:
        """Extract tools from a single content chunk.

        Args:
            chunk: Content chunk to analyze
            base_url: Base URL for API endpoints
            source_info: Source metadata
            chunk_idx: Index of this chunk
            total_chunks: Total number of chunks

        Returns:
            List of tool definitions from this chunk
        """
        chunk_context = f"(Part {chunk_idx + 1} of {total_chunks})" if total_chunks > 1 else ""

        prompt = f"""You are analyzing API documentation to extract tool definitions for an API gateway.
{chunk_context}

BASE URL for this API: {base_url}
Source: {source_info.get('filename') or source_info.get('url') or 'API Documentation'}
Format: {source_info.get('format', 'unknown')}

API DOCUMENTATION CONTENT:
---
{chunk}
---

TASK: Extract ALL API endpoints from this documentation and return them as tool definitions.

For EACH endpoint found, provide a complete tool definition with:
1. "name": A unique, descriptive tool name in camelCase (e.g., "getUserById", "createOrder", "listProducts")
2. "method": HTTP method - must be one of: GET, POST, PUT, DELETE, PATCH
3. "path": The API path (e.g., "/users/{{id}}", "/orders"). Use {{param}} for path parameters.
4. "full_url": Complete URL combining base_url + path (e.g., "{base_url}/users/{{id}}")
5. "description": Clear description of what this endpoint does (2-3 sentences)
6. "parameters": Array of parameter objects, each with:
   - "name": Parameter name
   - "type": Data type (string, integer, boolean, object, array)
   - "in": Where the parameter goes (path, query, body, header)
   - "required": true/false
   - "description": What this parameter is for
7. "request_body_schema": For POST/PUT/PATCH, describe the expected request body structure
8. "response_description": Brief description of what the endpoint returns
9. "tags": Array of relevant tags (e.g., ["users", "authentication"])
10. "auth_required": true/false - whether authentication is needed

IMPORTANT RULES:
- Extract EVERY endpoint you can identify from the documentation
- If information is missing, make reasonable inferences based on:
  - The endpoint path (e.g., /users suggests user-related operations)
  - The HTTP method (GET=read, POST=create, PUT=update, DELETE=remove)
  - Common API patterns
- Use the exact base_url provided: {base_url}
- Generate meaningful, unique names for each endpoint
- Include ALL parameters mentioned, even if descriptions are vague

Return a JSON object with this structure:
{{
  "tools": [
    {{
      "name": "toolName",
      "method": "GET",
      "path": "/endpoint/path",
      "full_url": "{base_url}/endpoint/path",
      "description": "Description of what this does",
      "parameters": [...],
      "tags": ["tag1", "tag2"],
      "auth_required": false
    }}
  ],
  "extraction_notes": "Any notes about ambiguous or unclear endpoints"
}}

If no endpoints are found in this content, return: {{"tools": [], "extraction_notes": "No API endpoints found in this section"}}
"""

        try:
            messages = [
                {"role": "system", "content": OPENAPI_ANALYST_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]

            response = await self.llm_service.chat_completion_json(messages, max_tokens=8000)

            tools = response.get('tools', [])

            # Validate and normalize each tool
            validated_tools = []
            for tool in tools:
                validated = self._validate_and_normalize_tool(tool, base_url)
                if validated:
                    validated_tools.append(validated)

            if response.get('extraction_notes'):
                logger.info(f"LLM extraction notes: {response['extraction_notes']}")

            return validated_tools

        except (LLMAPIError, LLMConfigurationError) as e:
            logger.error(f"LLM API error extracting tools: {e}")
            return []

    def _validate_and_normalize_tool(self, tool: Dict[str, Any], base_url: str) -> Optional[Dict[str, Any]]:
        """Validate and normalize a tool definition from LLM.

        Args:
            tool: Raw tool definition from LLM
            base_url: Base URL for the API

        Returns:
            Normalized tool definition or None if invalid
        """
        # Check required fields
        name = tool.get('name', '').strip()
        method = (tool.get('method', '') or '').upper().strip()
        path = tool.get('path', '').strip()

        if not name or len(name) < 2:
            logger.warning(f"Skipping tool with invalid name: {name}")
            return None

        if method not in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH']:
            logger.warning(f"Skipping tool {name} with invalid method: {method}")
            return None

        if not path:
            logger.warning(f"Skipping tool {name} with no path")
            return None

        # Normalize path
        if not path.startswith('/') and not path.startswith('http'):
            path = '/' + path

        # Generate full URL
        if path.startswith('http'):
            full_url = path
        else:
            full_url = tool.get('full_url') or f"{base_url.rstrip('/')}{path}"

        # Validate URL
        if not full_url.startswith(('http://', 'https://')):
            full_url = f"{base_url.rstrip('/')}{path}"

        # Normalize name (ensure valid identifier)
        import re
        normalized_name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
        if normalized_name[0].isdigit():
            normalized_name = 'api_' + normalized_name

        # Helper to ensure description is always a string
        def ensure_string_description(desc: Any, default: str = "") -> str:
            if desc is None:
                return default
            if isinstance(desc, str):
                return desc
            # Convert dict/list/other to string representation
            if isinstance(desc, dict):
                return json.dumps(desc, indent=2)[:500]  # Truncate long schemas
            return str(desc)[:500]

        # Helper to normalize JSON Schema type
        def normalize_json_type(type_val: Any) -> str:
            if isinstance(type_val, str):
                type_lower = type_val.lower()
                # Handle "string or object" type patterns from LLM
                if ' or ' in type_lower:
                    type_lower = type_lower.split(' or ')[0].strip()
                if type_lower in ['integer', 'int', 'number']:
                    return 'integer'
                elif type_lower in ['boolean', 'bool']:
                    return 'boolean'
                elif type_lower in ['object', 'dict', 'map']:
                    return 'object'
                elif type_lower in ['array', 'list']:
                    return 'array'
                else:
                    return 'string'
            elif isinstance(type_val, list):
                # Handle array type like ['string', 'object'] - use first valid type
                for t in type_val:
                    if isinstance(t, str) and t in ['string', 'integer', 'boolean', 'object', 'array', 'number']:
                        return 'integer' if t == 'number' else t
                return 'string'
            return 'string'

        # Build input schema from parameters
        input_schema = {
            "type": "object",
            "properties": {},
            "required": []
        }

        for param in tool.get('parameters', []):
            param_name = param.get('name', '')
            if not param_name:
                continue

            param_type = normalize_json_type(param.get('type', 'string'))
            param_desc = ensure_string_description(param.get('description'), f"Parameter: {param_name}")

            input_schema['properties'][param_name] = {
                "type": param_type,
                "description": param_desc
            }

            if param.get('required', False):
                input_schema['required'].append(param_name)

        # Add body parameter for POST/PUT/PATCH if not present
        if method in ['POST', 'PUT', 'PATCH'] and 'body' not in input_schema['properties']:
            # Ensure request_body_schema is converted to a string description
            body_schema = tool.get('request_body_schema')
            body_desc = ensure_string_description(body_schema, "Request body (JSON object)")

            input_schema['properties']['body'] = {
                "type": "object",
                "description": body_desc
            }

        # Ensure tool description is a string
        tool_description = ensure_string_description(tool.get('description'), f"{method} {path}")
        response_desc = ensure_string_description(tool.get('response_description'), '')

        return {
            "name": normalized_name,
            "method": method,
            "path": path,
            "url": full_url,
            "description": tool_description,
            "input_schema": input_schema,
            "tags": tool.get('tags', []) if isinstance(tool.get('tags'), list) else [],
            "auth_required": bool(tool.get('auth_required', False)),
            "annotations": {
                "title": name,
                "api_doc_method": method,
                "api_doc_path": path,
                "generated_from": "llm_direct_extraction",
                "response_description": response_desc
            }
        }
