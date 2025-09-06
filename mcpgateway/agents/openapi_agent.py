# -*- coding: utf-8 -*-
"""CrewAI OpenAPI Analysis Agent.

Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

This module implements a CrewAI agent for analyzing OpenAPI specifications and
generating intelligent tool descriptions and configurations for the MCP Gateway.
"""

# Standard
import json
from typing import Any, Dict, List, Optional

# Third-Party
from crewai import Agent, Crew, Process, Task
from crewai.llm import LLM

# First-Party
from mcpgateway.services.logging_service import LoggingService

# Initialize logging service
logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


class OpenAPIAgent:
    """CrewAI agent for OpenAPI specification analysis and tool generation."""

    def __init__(self, llm_endpoint: str = "http://20.66.97.208/v1/chat/completions"):
        """Initialize the OpenAPI agent.

        Args:
            llm_endpoint: LLM endpoint for the agent
        """
        self.llm_endpoint = llm_endpoint
        try:
            # Try to configure the LLM with proper provider format
            self.llm = LLM(
                model="openai/gpt-oss-20b",  # Specify provider format
                base_url=llm_endpoint.replace("/v1/chat/completions", ""),
                api_key="dummy"  # The endpoint doesn't require authentication
            )
        except Exception as e:
            logger.warning(f"Failed to initialize CrewAI LLM: {str(e)}")
            # Fallback to a mock LLM or disable AI features
            self.llm = None
        
        # Create the agent only if LLM is available
        if self.llm:
            self.agent = Agent(
                role="OpenAPI Specification Analyst",
                goal="Analyze OpenAPI specifications and generate comprehensive, accurate tool descriptions and metadata for API endpoints",
                backstory="""You are an expert API analyst with deep knowledge of OpenAPI/Swagger specifications.
                You excel at understanding API endpoints, parameters, authentication methods, and generating
                clear, accurate descriptions for tools. You pay special attention to security considerations,
                parameter validation, and proper HTTP method usage.""",
                verbose=True,
                allow_delegation=False,
                llm=self.llm
            )
        else:
            logger.warning("CrewAI LLM not available, AI enhancements will be disabled")
            self.agent = None

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
        # Check if agent is available
        if not self.agent:
            logger.warning("CrewAI agent not available, returning basic analysis")
            return self._generate_basic_analysis(spec, context)
            
        try:
            # Create analysis task
            task = Task(
                description=f"""Analyze the following OpenAPI specification and provide detailed insights:

OpenAPI Spec:
{json.dumps(spec, indent=2)}

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

Format your response as a structured JSON object with the above sections.
""",
                agent=self.agent,
                expected_output="A structured JSON analysis of the OpenAPI specification with detailed recommendations for tool generation"
            )

            # Create crew and execute
            crew = Crew(
                agents=[self.agent],
                tasks=[task],
                process=Process.sequential,
                verbose=True
            )

            result = crew.kickoff()
            
            # Parse the result
            try:
                if hasattr(result, 'raw'):
                    analysis = json.loads(result.raw)
                else:
                    analysis = json.loads(str(result))
            except json.JSONDecodeError:
                # If the result isn't valid JSON, create a basic structure
                logger.warning("Agent response was not valid JSON, creating basic analysis")
                analysis = {
                    "api_overview": str(result),
                    "endpoint_analysis": {},
                    "authentication_analysis": {},
                    "parameter_mapping": {},
                    "tool_generation_recommendations": {}
                }

            logger.info("Completed OpenAPI specification analysis")
            return analysis

        except Exception as e:
            logger.error(f"Failed to analyze OpenAPI specification: {str(e)}")
            # Return a basic analysis structure
            return {
                "api_overview": f"API with {len(spec.get('paths', {}))} endpoints",
                "endpoint_analysis": {},
                "authentication_analysis": {},
                "parameter_mapping": {},
                "tool_generation_recommendations": {},
                "error": str(e)
            }

    def _generate_basic_analysis(self, spec: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Generate basic analysis without LLM when agent is not available.

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
        """Enhance tool descriptions using agent analysis.

        Args:
            tools: List of tool configurations
            analysis: Analysis results from analyze_openapi_spec

        Returns:
            Enhanced tool configurations
        """
        # Check if agent is available
        if not self.agent:
            logger.warning("CrewAI agent not available, returning tools without enhancement")
            return tools
            
        try:
            # Create enhancement task
            task = Task(
                description=f"""Based on the following OpenAPI analysis and tool configurations,
enhance the tool descriptions, names, and metadata:

Analysis:
{json.dumps(analysis, indent=2)}

Tools to enhance:
{json.dumps(tools, indent=2)}

For each tool, improve:
1. **Name**: Make it more descriptive and user-friendly
2. **Description**: Provide clear, comprehensive description of what the tool does
3. **Tags**: Add relevant, searchable tags
4. **Annotations**: Add helpful hints and metadata
5. **Parameter descriptions**: Improve parameter descriptions for clarity

Return the enhanced tools as a JSON array, maintaining the original structure but with improved content.
Focus on making the tools more discoverable and easier to understand for end users.
""",
                agent=self.agent,
                expected_output="A JSON array of enhanced tool configurations with improved descriptions and metadata"
            )

            # Create crew and execute
            crew = Crew(
                agents=[self.agent],
                tasks=[task],
                process=Process.sequential,
                verbose=True
            )

            result = crew.kickoff()

            # Parse the result
            try:
                if hasattr(result, 'raw'):
                    enhanced_tools = json.loads(result.raw)
                else:
                    enhanced_tools = json.loads(str(result))

                if isinstance(enhanced_tools, list):
                    logger.info(f"Enhanced {len(enhanced_tools)} tool descriptions")
                    return enhanced_tools
                else:
                    logger.warning("Agent did not return a list, returning original tools")
                    return tools

            except json.JSONDecodeError:
                logger.warning("Agent response was not valid JSON, returning original tools")
                return tools

        except Exception as e:
            logger.error(f"Failed to enhance tool descriptions: {str(e)}")
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
        # Check if agent is available
        if not self.agent:
            logger.warning("CrewAI agent not available, returning basic security analysis")
            return self._generate_basic_security_analysis(spec, tools)
            
        try:
            # Create security validation task
            task = Task(
                description=f"""Analyze the security configuration of this OpenAPI specification and generated tools:

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

Return as a structured JSON object.
""",
                agent=self.agent,
                expected_output="A structured JSON security analysis with recommendations"
            )

            # Create crew and execute
            crew = Crew(
                agents=[self.agent],
                tasks=[task],
                process=Process.sequential,
                verbose=True
            )

            result = crew.kickoff()

            # Parse the result
            try:
                if hasattr(result, 'raw'):
                    security_analysis = json.loads(result.raw)
                else:
                    security_analysis = json.loads(str(result))
            except json.JSONDecodeError:
                logger.warning("Agent response was not valid JSON, creating basic security analysis")
                security_analysis = {
                    "security_scheme_analysis": str(result),
                    "authentication_mapping": "Valid",
                    "security_risks": [],
                    "recommendations": [],
                    "tool_specific_security": {}
                }

            logger.info("Completed security configuration validation")
            return security_analysis

        except Exception as e:
            logger.error(f"Failed to validate security configuration: {str(e)}")
            return {
                "security_scheme_analysis": "Analysis failed",
                "authentication_mapping": "Unknown",
                "security_risks": [f"Analysis error: {str(e)}"],
                "recommendations": ["Manual security review required"],
                "tool_specific_security": {},
                "error": str(e)
            }

    def _generate_basic_security_analysis(self, spec: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate basic security analysis without LLM when agent is not available.

        Args:
            spec: OpenAPI specification
            tools: Generated tool configurations

        Returns:
            Basic security analysis structure
        """
        security_schemes = spec.get("components", {}).get("securitySchemes", {})
        global_security = spec.get("security", [])
        
        # Count tools by auth type
        auth_summary = {}
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
                    "security_schemes_count": len(spec.get("components", {}).get("securitySchemes", {}))
                }
            }
            
            logger.info("Completed comprehensive OpenAPI analysis")
            return comprehensive_analysis
            
        except Exception as e:
            logger.error(f"Failed to generate comprehensive analysis: {str(e)}")
            return {
                "error": str(e),
                "api_overview": "Analysis failed",
                "endpoint_analysis": {},
                "authentication_analysis": {},
                "parameter_mapping": {},
                "tool_generation_recommendations": {},
                "security_analysis": {},
                "metadata": {}
            }