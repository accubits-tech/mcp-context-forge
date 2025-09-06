#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Examples of using the OpenAPI integration with MCP Gateway.

Copyright 2025
SPDX-License-Identifier: Apache-2.0

This file provides examples of how to use the new OpenAPI endpoints
to automatically generate tools from OpenAPI specifications.
"""

import json
import requests
from typing import Dict, Any


class MCPGatewayClient:
    """Simple client for interacting with MCP Gateway OpenAPI endpoints."""
    
    def __init__(self, base_url: str = "http://localhost:4444", auth_token: str = None):
        """Initialize the client.
        
        Args:
            base_url: Base URL of the MCP Gateway
            auth_token: JWT authentication token
        """
        self.base_url = base_url.rstrip('/')
        self.headers = {
            "Content-Type": "application/json"
        }
        if auth_token:
            self.headers["Authorization"] = f"Bearer {auth_token}"
    
    def upload_openapi_file(
        self, 
        file_path: str, 
        preview_only: bool = False,
        enhance_with_ai: bool = True,
        base_url: str = None,
        gateway_id: str = None,
        tags: str = None
    ) -> Dict[str, Any]:
        """Upload and process an OpenAPI specification file.
        
        Args:
            file_path: Path to the OpenAPI file (JSON or YAML)
            preview_only: If True, only preview tools without creating them
            enhance_with_ai: If True, use CrewAI to enhance descriptions
            base_url: Override base URL from specification
            gateway_id: Gateway ID to associate tools with
            tags: Comma-separated additional tags
            
        Returns:
            Response from the server
        """
        url = f"{self.base_url}/tools/openapi/upload"
        
        # Prepare form data
        data = {
            "preview_only": str(preview_only).lower(),
            "enhance_with_ai": str(enhance_with_ai).lower()
        }
        
        if base_url:
            data["base_url"] = base_url
        if gateway_id:
            data["gateway_id"] = gateway_id
        if tags:
            data["tags"] = tags
        
        # Upload file
        with open(file_path, 'rb') as f:
            files = {'file': (file_path, f, 'application/json')}
            response = requests.post(url, data=data, files=files, headers=self.headers)
        
        return response.json()
    
    def process_openapi_url(
        self,
        spec_url: str,
        preview_only: bool = False,
        enhance_with_ai: bool = True,
        base_url: str = None,
        gateway_id: str = None,
        tags: str = None
    ) -> Dict[str, Any]:
        """Process an OpenAPI specification from URL.
        
        Args:
            spec_url: URL to the OpenAPI specification
            preview_only: If True, only preview tools without creating them
            enhance_with_ai: If True, use CrewAI to enhance descriptions
            base_url: Override base URL from specification
            gateway_id: Gateway ID to associate tools with
            tags: Comma-separated additional tags
            
        Returns:
            Response from the server
        """
        url = f"{self.base_url}/tools/openapi/url"
        
        payload = {
            "url": spec_url,
            "preview_only": preview_only,
            "enhance_with_ai": enhance_with_ai
        }
        
        if base_url:
            payload["base_url"] = base_url
        if gateway_id:
            payload["gateway_id"] = gateway_id
        if tags:
            payload["tags"] = tags
        
        response = requests.post(url, json=payload, headers=self.headers)
        return response.json()
    
    def list_tools(self, tags: str = None, include_inactive: bool = False) -> Dict[str, Any]:
        """List tools in the gateway.
        
        Args:
            tags: Comma-separated list of tags to filter by
            include_inactive: Whether to include inactive tools
            
        Returns:
            List of tools
        """
        url = f"{self.base_url}/tools/"
        params = {
            "include_inactive": str(include_inactive).lower()
        }
        if tags:
            params["tags"] = tags
        
        response = requests.get(url, params=params, headers=self.headers)
        return response.json()


def example_1_preview_from_url():
    """Example 1: Preview tools from a public OpenAPI specification URL."""
    print("📝 Example 1: Preview tools from OpenAPI URL")
    
    client = MCPGatewayClient()
    
    # Use a public OpenAPI spec (JSONPlaceholder API)
    spec_url = "https://jsonplaceholder.typicode.com/openapi.json"
    
    try:
        result = client.process_openapi_url(
            spec_url=spec_url,
            preview_only=True,  # Only preview, don't create tools
            enhance_with_ai=False,  # Skip AI enhancement for faster response
            tags="example,jsonplaceholder"
        )
        
        print(f"  📊 API: {result.get('api_info', {}).get('title', 'Unknown')}")
        print(f"  🔧 Tools that would be created: {result.get('tool_count', 0)}")
        
        for tool in result.get('tools', [])[:3]:  # Show first 3 tools
            print(f"    - {tool['name']}: {tool['method']} {tool['path']}")
        
        print("  ✅ Preview completed successfully")
        
    except Exception as e:
        print(f"  ❌ Error: {str(e)}")


def example_2_upload_file_with_ai():
    """Example 2: Upload OpenAPI file and create tools with AI enhancement."""
    print("\n🤖 Example 2: Upload file with AI enhancement")
    
    # First, create a sample OpenAPI file
    sample_spec = {
        "openapi": "3.0.0",
        "info": {
            "title": "Blog API",
            "version": "1.0.0",
            "description": "A simple blog management API"
        },
        "servers": [{"url": "https://blog.example.com/api"}],
        "paths": {
            "/posts": {
                "get": {
                    "operationId": "listPosts",
                    "summary": "List blog posts",
                    "responses": {"200": {"description": "Success"}}
                },
                "post": {
                    "operationId": "createPost",
                    "summary": "Create new blog post",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "content": {"type": "string"},
                                        "author": {"type": "string"}
                                    },
                                    "required": ["title", "content"]
                                }
                            }
                        }
                    },
                    "responses": {"201": {"description": "Created"}}
                }
            }
        }
    }
    
    # Save to temporary file
    import tempfile
    import os
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(sample_spec, f, indent=2)
        temp_file = f.name
    
    try:
        client = MCPGatewayClient()
        
        result = client.upload_openapi_file(
            file_path=temp_file,
            preview_only=False,  # Actually create the tools
            enhance_with_ai=True,  # Use AI to improve descriptions
            tags="example,blog,demo"
        )
        
        print(f"  📊 API: {result.get('api_info', {}).get('title', 'Unknown')}")
        print(f"  ✅ Tools created: {result.get('tools_created', 0)}")
        print(f"  ❌ Tools failed: {result.get('tools_failed', 0)}")
        print(f"  🤖 AI enhanced: {result.get('ai_enhanced', False)}")
        
        # Show created tools
        for tool in result.get('created_tools', []):
            print(f"    - {tool['name']}: {tool['description'][:50]}...")
        
    except Exception as e:
        print(f"  ❌ Error: {str(e)}")
    
    finally:
        # Clean up temp file
        os.unlink(temp_file)


def example_3_filter_generated_tools():
    """Example 3: Filter and work with generated tools."""
    print("\n🔍 Example 3: Filter generated tools")
    
    client = MCPGatewayClient()
    
    try:
        # List all tools with OpenAPI tag
        result = client.list_tools(tags="openapi", include_inactive=False)
        
        print(f"  🔧 Found {len(result)} OpenAPI-generated tools")
        
        # Group by integration type
        openapi_tools = [tool for tool in result if 'openapi' in tool.get('tags', [])]
        ai_enhanced_tools = [tool for tool in openapi_tools if 'auto-generated' in tool.get('tags', [])]
        
        print(f"  📊 OpenAPI tools: {len(openapi_tools)}")
        print(f"  🤖 AI enhanced tools: {len(ai_enhanced_tools)}")
        
        # Show tools by HTTP method
        methods = {}
        for tool in openapi_tools:
            method = tool.get('request_type', 'UNKNOWN')
            methods[method] = methods.get(method, 0) + 1
        
        print("  📈 Tools by HTTP method:")
        for method, count in methods.items():
            print(f"    - {method}: {count}")
        
    except Exception as e:
        print(f"  ❌ Error: {str(e)}")


def example_4_batch_processing():
    """Example 4: Process multiple OpenAPI specifications."""
    print("\n⚡ Example 4: Batch processing multiple APIs")
    
    client = MCPGatewayClient()
    
    # List of public OpenAPI specifications to process
    apis = [
        {
            "name": "JSONPlaceholder",
            "url": "https://jsonplaceholder.typicode.com/openapi.json",
            "tags": "demo,jsonplaceholder"
        },
        {
            "name": "HTTPBin",
            "url": "https://httpbin.org/spec.json", 
            "tags": "demo,httpbin"
        }
    ]
    
    results = []
    
    for api in apis:
        try:
            print(f"  📝 Processing {api['name']}...")
            
            result = client.process_openapi_url(
                spec_url=api['url'],
                preview_only=True,  # Preview only for demo
                enhance_with_ai=False,  # Skip AI for faster processing
                tags=api['tags']
            )
            
            results.append({
                "name": api['name'],
                "success": True,
                "tool_count": result.get('tool_count', 0),
                "api_title": result.get('api_info', {}).get('title', 'Unknown')
            })
            
            print(f"    ✅ {api['name']}: {result.get('tool_count', 0)} tools")
            
        except Exception as e:
            results.append({
                "name": api['name'],
                "success": False,
                "error": str(e)
            })
            print(f"    ❌ {api['name']}: {str(e)}")
    
    # Summary
    successful = sum(1 for r in results if r['success'])
    total_tools = sum(r.get('tool_count', 0) for r in results if r['success'])
    
    print(f"  📊 Summary: {successful}/{len(apis)} APIs processed successfully")
    print(f"  🔧 Total tools that would be created: {total_tools}")


def main():
    """Run all examples."""
    print("🚀 MCP Gateway OpenAPI Integration Examples\n")
    print("🔗 Make sure your MCP Gateway is running on http://localhost:4444")
    print("🔑 Update auth_token in MCPGatewayClient if authentication is required\n")
    
    # Run examples
    example_1_preview_from_url()
    example_2_upload_file_with_ai()
    example_3_filter_generated_tools()
    example_4_batch_processing()
    
    print("\n🎉 All examples completed!")
    print("\n📚 Additional Usage Tips:")
    print("  - Use preview_only=true to see what tools would be created")
    print("  - Use enhance_with_ai=true for better tool descriptions")
    print("  - Add custom tags to organize generated tools")
    print("  - Specify gateway_id to associate tools with specific gateways")
    print("  - Override base_url if the OpenAPI servers are incorrect")


if __name__ == "__main__":
    main()