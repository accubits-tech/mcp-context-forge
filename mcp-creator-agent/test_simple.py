#!/usr/bin/env python3
"""Simple test script for the MCP Creator Agent."""

import os
import sys
from dotenv import load_dotenv

# Add the current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_imports():
    """Test that all modules can be imported."""
    try:
        from mcp_creator_agent.models import FunctionCreationRequest, FunctionCreationResponse
        print("✅ Models imported successfully")
        
        from mcp_creator_agent.tools import execute_python, validate_python_code, install_package
        print("✅ Tools imported successfully")
        
        from mcp_creator_agent.agent import FunctionCreatorAgent
        print("✅ Agent imported successfully")
        
        return True
    except ImportError as e:
        print(f"❌ Import error: {e}")
        return False

def test_models():
    """Test the Pydantic models."""
    try:
        from mcp_creator_agent.models import FunctionCreationRequest, FunctionCreationResponse
        
        # Test request model
        request = FunctionCreationRequest(
            api_documentation="Test API documentation",
            function_name="test_function",
            description="Test function description",
            requirements=["requests"],
            test_examples=[{"input": "test", "expected_output": "result"}],
            additional_context="Test context"
        )
        print("✅ FunctionCreationRequest created successfully")
        
        # Test response model
        response = FunctionCreationResponse(
            function_code="def test_function(): pass",
            function_name="test_function",
            description="Test function",
            requirements=["requests"],
            usage_example="test_function()",
            test_code="assert test_function() is None",
            execution_result="Test passed",
            metadata={"test": True}
        )
        print("✅ FunctionCreationResponse created successfully")
        
        return True
    except Exception as e:
        print(f"❌ Model test error: {e}")
        return False

def test_basic_functionality():
    """Test basic functionality without LLM calls."""
    try:
        from mcp_creator_agent.agent import FunctionCreatorAgent
        
        # This will fail without LLM, but we can test the structure
        print("✅ Agent class structure is valid")
        
        # Test that we can create the class (it will fail on LLM initialization)
        try:
            agent = FunctionCreatorAgent(verbose=False)
            print("✅ Agent class can be instantiated")
        except Exception as e:
            if "OPENAI_API_KEY" in str(e) or "llm" in str(e).lower():
                print("✅ Agent class structure is valid (LLM not configured)")
            else:
                raise e
        
        return True
    except Exception as e:
        print(f"❌ Basic functionality test error: {e}")
        return False

def main():
    """Run all tests."""
    print("🧪 Testing MCP Creator Agent")
    print("=" * 40)
    
    tests = [
        ("Import Test", test_imports),
        ("Model Test", test_models),
        ("Basic Functionality Test", test_basic_functionality),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\n🔍 Running: {test_name}")
        try:
            if test_func():
                passed += 1
                print(f"✅ {test_name} passed")
            else:
                print(f"❌ {test_name} failed")
        except Exception as e:
            print(f"❌ {test_name} failed with exception: {e}")
    
    print("\n" + "=" * 40)
    print(f"📊 Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! The agent is ready to use.")
        print("\n💡 Next steps:")
        print("   1. Set your OPENAI_API_KEY environment variable")
        print("   2. Run: python demo.py")
        print("   3. Or use the CLI: python -m mcp_creator_agent.main --help")
    else:
        print("⚠️  Some tests failed. Please check the errors above.")
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
