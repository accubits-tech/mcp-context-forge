# OpenAPI Integration with CrewAI Agent

This document describes the OpenAPI specification parser and CrewAI agent integration that has been added to the MCP Gateway.

## Overview

The integration provides automatic tool generation from OpenAPI specifications using:
- **OpenAPI Service**: Parses OpenAPI specs and generates MCP Gateway tools
- **CrewAI Agent**: Uses AI to enhance tool descriptions and validate configurations
- **REST endpoints**: Upload files or process URLs containing OpenAPI specifications

## Features

### 🔧 Core Functionality
- **Multi-format Support**: JSON and YAML OpenAPI specifications
- **Version Support**: OpenAPI 3.0.x and 3.1.x
- **Validation**: Comprehensive OpenAPI specification validation
- **Authentication Mapping**: Automatic extraction of security schemes
- **Parameter Conversion**: Maps OpenAPI parameters to MCP tool input schemas

### 🤖 AI Enhancement
- **CrewAI Agent**: Powered by the provided LLM endpoint (`http://20.66.97.208/v1/chat/completions`)
- **Intelligent Analysis**: Analyzes API endpoints for security risks and best practices
- **Enhanced Descriptions**: Generates better tool names and descriptions
- **Security Validation**: Reviews authentication configurations

### 📡 API Endpoints

#### Upload OpenAPI File
```
POST /tools/openapi/upload
Content-Type: multipart/form-data

Parameters:
- file: OpenAPI file (JSON/YAML)
- base_url: Override base URL (optional)
- gateway_id: Gateway ID to associate tools (optional)  
- tags: Comma-separated additional tags (optional)
- preview_only: Only preview tools (default: false)
- enhance_with_ai: Use CrewAI for enhancement (default: true)
```

#### Process OpenAPI from URL
```
POST /tools/openapi/url
Content-Type: application/json

{
  "url": "https://api.example.com/openapi.json",
  "base_url": "https://api.example.com",
  "gateway_id": "gateway-1",
  "tags": "api,external",
  "preview_only": false,
  "enhance_with_ai": true
}
```

## Implementation Files

### Core Components
- `mcpgateway/services/openapi_service.py` - OpenAPI parsing and tool generation
- `mcpgateway/agents/openapi_agent.py` - CrewAI agent for AI enhancement
- `mcpgateway/main.py` - REST API endpoints (lines 1722-2079)

### Dependencies Added
```toml
# pyproject.toml
"crewai>=0.80.0",
"openapi-spec-validator>=0.7.0", 
"pyyaml>=6.0.2",
```

### Testing & Examples
- `test_openapi_integration.py` - Integration test script
- `examples/openapi_usage_examples.py` - Usage examples and client code

## Usage Examples

### 1. Preview Tools from URL
```python
import requests

response = requests.post("http://localhost:4444/tools/openapi/url", json={
    "url": "https://petstore3.swagger.io/api/v3/openapi.json",
    "preview_only": True,
    "enhance_with_ai": False
})

print(f"Would create {response.json()['tool_count']} tools")
```

### 2. Upload File and Create Tools
```python
import requests

with open("my-api.json", "rb") as f:
    response = requests.post(
        "http://localhost:4444/tools/openapi/upload",
        files={"file": f},
        data={
            "enhance_with_ai": "true",
            "tags": "my-api,production"
        }
    )

print(f"Created {response.json()['tools_created']} tools")
```

## Response Format

Both endpoints return a JSON response with:

```json
{
  "status": "success",
  "message": "Processed OpenAPI specification from file.json",
  "api_info": {
    "title": "Pet Store API",
    "version": "1.0.0", 
    "openapi_version": "3.0.0"
  },
  "tools_created": 12,
  "tools_failed": 0,
  "created_tools": [...],
  "failed_tools": [],
  "ai_enhanced": true
}
```

## Tool Generation Logic

### 1. OpenAPI Parsing
- Validates OpenAPI specification format and version
- Extracts API metadata (title, version, servers)
- Parses paths, operations, parameters, and security schemes

### 2. Tool Creation
For each OpenAPI operation:
- **Name**: Uses `operationId` or generates from path/method
- **URL**: Combines server URL with operation path
- **Method**: Maps to HTTP method (GET, POST, PUT, DELETE, PATCH)
- **Parameters**: Converts OpenAPI parameters to input schema
- **Authentication**: Maps security schemes to MCP auth types
- **Metadata**: Adds OpenAPI-specific annotations

### 3. AI Enhancement (Optional)
The CrewAI agent:
- Analyzes the complete API specification
- Reviews each endpoint for security and usability
- Generates enhanced descriptions and names
- Validates authentication configurations
- Provides recommendations and warnings

## Security Features

### Authentication Mapping
- **API Key**: Maps to `authheaders` with custom header
- **Bearer Token**: Maps to `bearer` auth type  
- **Basic Auth**: Maps to `basic` auth type
- **OAuth 2.0**: Placeholder values for manual configuration

### Security Validation
- Identifies potentially destructive operations
- Flags operations requiring authentication
- Validates security scheme configurations
- Provides security recommendations

## Error Handling

### Validation Errors
- Invalid OpenAPI format (400 Bad Request)
- Unsupported OpenAPI version (400 Bad Request)
- Missing required fields (400 Bad Request)

### Processing Errors  
- Network errors when fetching from URL (400 Bad Request)
- Tool creation failures (partial success with failed_tools list)
- AI enhancement failures (logs warning, continues with basic tools)

## Configuration

### CrewAI Agent Configuration
The agent is configured to use:
- **Model**: `gpt-oss-20b`
- **Endpoint**: `http://20.66.97.208/v1/chat/completions`
- **Role**: OpenAPI Specification Analyst
- **Capabilities**: Analysis, enhancement, security validation

### Service Integration
- Uses existing `ToolService.register_tool()` for tool creation
- Integrates with MCP Gateway authentication and authorization
- Supports all existing tool management features (tags, metadata, etc.)

## Installation & Deployment

1. **Dependencies**: Install new dependencies from `pyproject.toml`
2. **Services**: The services are automatically initialized in `main.py`
3. **Endpoints**: New routes are added to the existing `/tools/` router
4. **Testing**: Run integration tests to verify functionality

## Future Enhancements

Potential improvements:
- **Webhook Support**: Auto-update tools when OpenAPI specs change
- **Bulk Operations**: Process multiple OpenAPI specifications simultaneously  
- **Custom Templates**: User-defined templates for tool generation
- **Integration Testing**: Test generated tools against actual APIs
- **Schema Evolution**: Handle OpenAPI specification updates intelligently

---

The OpenAPI integration is now ready for use with the MCP Gateway. Users can upload OpenAPI specifications or provide URLs, and the system will automatically generate appropriate tools with optional AI enhancement for better usability.