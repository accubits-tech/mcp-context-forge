"""Main entry point for the MCP Creator Agent."""

import argparse
import logging
import sys

from .agent import FunctionCreatorAgent
from .models import FunctionCreationRequest

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_api_doc_from_file(file_path: str) -> str:
    """Load API documentation from a file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error reading file {file_path}: {e}")
        sys.exit(1)


def save_function_to_file(function_code: str, output_path: str) -> None:
    """Save the generated function to a file."""
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(function_code)
        logger.info(f"Function saved to: {output_path}")
    except Exception as e:
        logger.error(f"Error saving function to {output_path}: {e}")
        sys.exit(1)


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="MCP Creator Agent - Create Python functions from API documentation"
    )
    
    parser.add_argument(
        "--api-doc",
        type=str,
        help="API documentation text or path to file containing API documentation"
    )
    
    parser.add_argument(
        "--api-doc-file",
        type=str,
        help="Path to file containing API documentation"
    )
    
    parser.add_argument(
        "--function-name",
        type=str,
        help="Custom name for the generated function"
    )
    
    parser.add_argument(
        "--description",
        type=str,
        help="Description of what the function should do"
    )
    
    parser.add_argument(
        "--requirements",
        type=str,
        nargs="+",
        help="List of required Python packages"
    )
    
    parser.add_argument(
        "--output",
        type=str,
        default="generated_function.py",
        help="Output file path for the generated function (default: generated_function.py)"
    )
    
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Maximum number of iterations for the crew (default: 3)"
    )
    
    parser.add_argument(
        "--llm-model",
        type=str,
        default="gpt-4o",
        help="LLM model to use (default: gpt-4o)"
    )
    
    args = parser.parse_args()
    
    # Validate input
    if not args.api_doc and not args.api_doc_file:
        logger.error("Either --api-doc or --api-doc-file must be provided")
        sys.exit(1)
    
    # Load API documentation
    if args.api_doc_file:
        api_doc = load_api_doc_from_file(args.api_doc_file)
    else:
        api_doc = args.api_doc
    
    # Create request
    request = FunctionCreationRequest(
        api_documentation=api_doc,
        function_name=args.function_name,
        description=args.description,
        requirements=args.requirements or [],
        additional_context=None
    )
    
    try:
        # Create agent
        logger.info("Initializing Function Creator Agent...")
        agent = FunctionCreatorAgent(
            verbose=args.verbose,
            max_iterations=args.max_iterations
        )
        
        # Create function
        logger.info("Creating Python function from API documentation...")
        response = agent.create_function(request)
        
        # Save function to file
        save_function_to_file(response.function_code, args.output)
        
        # Print summary
        print("\n" + "="*50)
        print("FUNCTION CREATION COMPLETED SUCCESSFULLY!")
        print("="*50)
        print(f"Function Name: {response.function_name}")
        print(f"Description: {response.description}")
        print(f"Requirements: {', '.join(response.requirements) if response.requirements else 'None'}")
        print(f"Output File: {args.output}")
        print(f"Usage Example:\n{response.usage_example}")
        
        if response.test_code:
            print(f"\nTest Code:\n{response.test_code}")
        
        if response.execution_result:
            print(f"\nExecution Result:\n{response.execution_result}")
        
        print("="*50)
        
    except Exception as e:
        logger.error(f"Failed to create function: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
