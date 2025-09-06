#!/usr/bin/env python3
"""Demo script for the MCP Creator Agent."""

import os
import sys
from dotenv import load_dotenv

# Add the current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp_creator_agent.agent import FunctionCreatorAgent
from mcp_creator_agent.models import FunctionCreationRequest

# Load environment variables
load_dotenv()


def demo_simple_function():
    """Demo creating a simple function."""
    print("🚀 Demo: Creating a simple function from API documentation")
    print("=" * 60)
    
    # Example API documentation
    api_doc = """
    Create a Python function that calculates the factorial of a number.
    
    The function should:
    - Accept a positive integer as input
    - Return the factorial of that number
    - Handle edge cases (0, 1, negative numbers)
    - Include proper error handling
    - Be well-documented
    """
    
    # Create request
    request = FunctionCreationRequest(
        api_documentation=api_doc,
        function_name="calculate_factorial",
        description="Calculate the factorial of a given number",
        requirements=[],
        test_examples=[
            {"input": 5, "expected_output": 120},
            {"input": 0, "expected_output": 1},
            {"input": 1, "expected_output": 1}
        ],
        additional_context=None
    )
    
    try:
        # Create agent
        print("🤖 Initializing Function Creator Agent...")
        agent = FunctionCreatorAgent(verbose=True)
        
        # Create function
        print("⚙️  Creating Python function...")
        response = agent.create_function(request)
        
        # Display results
        print("\n✅ Function created successfully!")
        print(f"📝 Function Name: {response.function_name}")
        print(f"📋 Description: {response.description}")
        print(f"📦 Requirements: {', '.join(response.requirements) if response.requirements else 'None'}")
        print(f"💻 Usage Example:\n{response.usage_example}")
        
        if response.test_code:
            print(f"\n🧪 Test Code:\n{response.test_code}")
        
        if response.execution_result:
            print(f"\n🎯 Execution Result:\n{response.execution_result}")
        
        # Save function to file
        output_file = "demo_factorial_function.py"
        with open(output_file, 'w') as f:
            f.write(response.function_code)
        print(f"\n💾 Function saved to: {output_file}")
        
    except Exception as e:
        print(f"❌ Error: {e}")


def demo_api_function():
    """Demo creating a function for API interaction."""
    print("\n🌐 Demo: Creating an API interaction function")
    print("=" * 60)
    
    # Example API documentation
    api_doc = """
    Create a Python function that fetches weather data from a REST API.
    
    The function should:
    - Accept city name and API key as parameters
    - Make HTTP GET request to weather API
    - Handle HTTP errors gracefully
    - Parse JSON response
    - Return structured weather data
    - Include proper error handling and logging
    - Use requests library for HTTP calls
    """
    
    # Create request
    request = FunctionCreationRequest(
        api_documentation=api_doc,
        function_name="get_weather_data",
        description="Fetch weather data for a given city from weather API",
        requirements=["requests"],
        test_examples=[
            {"input": {"city": "London", "api_key": "demo_key"}, "expected_output": "weather_data_dict"}
        ],
        additional_context=None
    )
    
    try:
        # Create agent
        print("🤖 Initializing Function Creator Agent...")
        agent = FunctionCreatorAgent(verbose=True)
        
        # Create function
        print("⚙️  Creating API function...")
        response = agent.create_function(request)
        
        # Display results
        print("\n✅ API function created successfully!")
        print(f"📝 Function Name: {response.function_name}")
        print(f"📋 Description: {response.description}")
        print(f"📦 Requirements: {', '.join(response.requirements)}")
        print(f"💻 Usage Example:\n{response.usage_example}")
        
        # Save function to file
        output_file = "demo_weather_api_function.py"
        with open(output_file, 'w') as f:
            f.write(response.function_code)
        print(f"\n💾 Function saved to: {output_file}")
        
    except Exception as e:
        print(f"❌ Error: {e}")


def main():
    """Run the demo."""
    print("🎯 MCP Creator Agent - CrewAI Demo")
    print("=" * 60)
    
    # Check if OpenAI API key is set
    if not os.getenv("OPENAI_API_KEY"):
        print("⚠️  Warning: OPENAI_API_KEY environment variable not set")
        print("   Please set your OpenAI API key to run the demo")
        print("   export OPENAI_API_KEY='your-api-key-here'")
        return
    
    try:
        # Run demos
        demo_simple_function()
        demo_api_function()
        
        print("\n🎉 Demo completed successfully!")
        print("📁 Check the generated .py files for the created functions")
        
    except KeyboardInterrupt:
        print("\n⏹️  Demo interrupted by user")
    except Exception as e:
        print(f"\n❌ Demo failed: {e}")


if __name__ == "__main__":
    main()
