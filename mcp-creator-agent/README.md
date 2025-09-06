# MCP Creator Agent

A powerful CrewAI agent that creates production-ready Python functions based on API documentation provided by users. This agent uses a multi-agent approach to generate, review, test, and document Python functions automatically.

## 🚀 Features

- **🤖 Multi-Agent Workflow**: Uses CrewAI with specialized agents for code generation, review, and documentation
- **🔧 Dynamic Function Creation**: Generates Python functions from natural language API descriptions
- **🧪 Built-in Testing**: Includes test generation and execution using e2b-code-interpreter
- **📚 Comprehensive Documentation**: Auto-generates usage examples and integration guidance
- **⚡ Production Ready**: Creates functions with proper error handling, docstrings, and PEP 8 compliance
- **🛠️ Multiple Tools**: Python interpreter, code validation, and package installation tools

## 🏗️ Architecture

The agent consists of three specialized CrewAI agents:

1. **Code Generator Agent**: Creates Python functions based on API documentation
2. **Code Reviewer Agent**: Validates, tests, and improves the generated code
3. **Documentation Specialist Agent**: Creates comprehensive documentation and examples

### Tools Available

- **Python Interpreter**: Execute Python code in a sandboxed environment
- **Code Validator**: Validate Python syntax without execution
- **Package Installer**: Install required packages in the sandbox

## 📋 Requirements

- Python 3.10 or higher
- OpenAI API key (or other supported LLM provider)
- Internet connection for LLM API calls

## 🚀 Quick Start

### 1. Installation

```bash
cd mcp-creator-agent

# Install dependencies
pip install -r requirements.txt

# Or install in development mode
pip install -e .
```

### 2. Environment Setup

```bash
# Set your OpenAI API key
export OPENAI_API_KEY="your-api-key-here"

# Optional: Set other environment variables
export OPENAI_BASE_URL="https://api.openai.com/v1"  # If using custom endpoint
```

### 3. Basic Usage

#### Command Line Interface

```bash
# Create a function from API documentation
python -m mcp_creator_agent.main \
    --api-doc "Create a function that calculates factorial of a number" \
    --function-name "calculate_factorial" \
    --output "my_function.py"

# Or from a file
python -m mcp_creator_agent.main \
    --api-doc-file "api_spec.txt" \
    --function-name "api_client" \
    --requirements requests pandas \
    --output "api_client.py"
```

#### Programmatic Usage

```python
from mcp_creator_agent.agent import FunctionCreatorAgent
from mcp_creator_agent.models import FunctionCreationRequest

# Create agent
agent = FunctionCreatorAgent(verbose=True)

# Create request
request = FunctionCreationRequest(
    api_documentation="Create a function that fetches data from a REST API",
    function_name="fetch_api_data",
    description="Fetch data from REST API with error handling",
    requirements=["requests"]
)

# Generate function
response = agent.create_function(request)

# Save to file
with open("generated_function.py", "w") as f:
    f.write(response.function_code)

print(f"Function created: {response.function_name}")
```

### 4. Run Demo

```bash
# Run the interactive demo
python demo.py
```

## 📖 Examples

### Example 1: Simple Mathematical Function

**Input:**
```bash
python -m mcp_creator_agent.main \
    --api-doc "Create a function that calculates the nth Fibonacci number" \
    --function-name "fibonacci" \
    --description "Calculate Fibonacci numbers with memoization"
```

**Generated Output:**
```python
def fibonacci(n: int, memo: dict = None) -> int:
    """
    Calculate the nth Fibonacci number using memoization.
    
    Args:
        n: The position in the Fibonacci sequence (0-indexed)
        memo: Dictionary for memoization (internal use)
    
    Returns:
        The nth Fibonacci number
    
    Raises:
        ValueError: If n is negative
    """
    if memo is None:
        memo = {}
    
    if n < 0:
        raise ValueError("Input must be non-negative")
    
    if n in memo:
        return memo[n]
    
    if n <= 1:
        memo[n] = n
        return n
    
    memo[n] = fibonacci(n - 1, memo) + fibonacci(n - 2, memo)
    return memo[n]
```

### Example 2: API Client Function

**Input:**
```bash
python -m mcp_creator_agent.main \
    --api-doc "Create a function that makes HTTP GET requests to a REST API and handles errors" \
    --function-name "api_get" \
    --requirements requests \
    --description "Make HTTP GET requests with comprehensive error handling"
```

**Generated Output:**
```python
import requests
from typing import Dict, Any, Optional
import logging

def api_get(
    url: str, 
    headers: Optional[Dict[str, str]] = None, 
    timeout: int = 30
) -> Dict[str, Any]:
    """
    Make an HTTP GET request to a REST API with comprehensive error handling.
    
    Args:
        url: The API endpoint URL
        headers: Optional HTTP headers
        timeout: Request timeout in seconds
    
    Returns:
        Dictionary containing response data and metadata
    
    Raises:
        requests.RequestException: For HTTP/network errors
        ValueError: For invalid input parameters
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")
    
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    
    try:
        response = requests.get(
            url, 
            headers=headers or {}, 
            timeout=timeout
        )
        response.raise_for_status()
        
        return {
            "status_code": response.status_code,
            "data": response.json() if response.content else None,
            "headers": dict(response.headers),
            "url": response.url
        }
        
    except requests.exceptions.Timeout:
        raise requests.RequestException(f"Request timed out after {timeout} seconds")
    except requests.exceptions.RequestException as e:
        raise requests.RequestException(f"Request failed: {str(e)}")
    except ValueError as e:
        raise ValueError(f"Invalid response format: {str(e)}")
```

## ⚙️ Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_API_KEY` | OpenAI API key (required) | None |
| `OPENAI_BASE_URL` | Custom OpenAI endpoint | `https://api.openai.com/v1` |
| `OPENAI_ORGANIZATION` | OpenAI organization ID | None |

### Agent Configuration

```python
from mcp_creator_agent.agent import FunctionCreatorAgent

# Custom configuration
agent = FunctionCreatorAgent(
    verbose=True,           # Enable detailed logging
    max_iterations=5,       # Maximum crew iterations
    llm=LLM(model="gpt-4") # Custom LLM configuration
)
```

## 🧪 Testing

```bash
# Run tests
pytest tests/

# Run with coverage
pytest --cov=mcp_creator_agent tests/

# Run specific test
pytest tests/test_agent.py::test_function_creation
```

## 🔧 Development

### Project Structure

```
mcp-creator-agent/
├── mcp_creator_agent/          # Main package
│   ├── __init__.py            # Package initialization
│   ├── agent.py               # Main CrewAI agent
│   ├── models.py              # Pydantic models
│   ├── tools.py               # CrewAI tools
│   └── main.py                # CLI entry point
├── tests/                     # Test suite
├── demo.py                    # Demo script
├── requirements.txt           # Dependencies
├── pyproject.toml            # Project configuration
└── README.md                 # This file
```

### Adding New Tools

```python
from crewai.tools import tool

@tool("Custom Tool")
def custom_tool(param: str) -> str:
    """Description of what this tool does."""
    # Tool implementation
    return "Tool result"
```

### Adding New Agent Types

```python
def _create_agents(self) -> List[Agent]:
    # ... existing agents ...
    
    new_agent = Agent(
        role='New Agent Role',
        goal='What this agent should accomplish',
        backstory='Agent background and expertise',
        tools=self.tools,
        llm=self.llm,
        verbose=self.verbose
    )
    
    return [..., new_agent]
```

## 🚀 Deployment

### Docker

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "-m", "mcp_creator_agent.main", "--help"]
```

### Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcp-creator-agent
spec:
  replicas: 3
  selector:
    matchLabels:
      app: mcp-creator-agent
  template:
    metadata:
      labels:
        app: mcp-creator-agent
    spec:
      containers:
      - name: agent
        image: mcp-creator-agent:latest
        env:
        - name: OPENAI_API_KEY
          valueFrom:
            secretKeyRef:
              name: openai-secret
              key: api-key
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass
6. Submit a pull request

## 📄 License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## 🆘 Support

- **Issues**: Report bugs and feature requests on GitHub
- **Documentation**: Check the [docs/](docs/) directory
- **Examples**: See [examples/](examples/) for more usage examples

## 🔗 Related Projects

- [CrewAI](https://github.com/joaomdmoura/crewAI) - Framework for orchestrating role-playing autonomous AI agents
- [e2b-code-interpreter](https://github.com/e2b-dev/e2b) - Sandboxed Python code execution
- [MCP Context Forge](https://github.com/your-org/mcp-context-forge) - Main project repository
