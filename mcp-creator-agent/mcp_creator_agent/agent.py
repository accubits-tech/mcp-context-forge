"""Main CrewAI agent for creating Python functions from API documentation."""

import logging
from typing import Optional, List
from crewai import Agent, Task, Crew, LLM
from crewai.tools import BaseTool

from .models import FunctionCreationRequest, FunctionCreationResponse
from .tools import execute_python, validate_python_code, install_package

logger = logging.getLogger(__name__)


class FunctionCreatorAgent:
    """CrewAI agent that creates Python functions from API documentation."""
    
    def __init__(
        self,
        llm: Optional[LLM] = None,
        verbose: bool = True,
        max_iterations: int = 3
    ):
        """
        Initialize the FunctionCreatorAgent.
        
        Args:
            llm: LLM instance to use (defaults to OpenAI GPT-4)
            verbose: Whether to enable verbose logging
            max_iterations: Maximum number of iterations for the crew
        """
        self.llm = llm or LLM(model="gpt-4o")
        self.verbose = verbose
        self.max_iterations = max_iterations
        
        # Initialize tools
        self.tools = [
            execute_python,
            validate_python_code,
            install_package
        ]
        
        # Create the crew
        self.crew = self._create_crew()
    
    def _create_agents(self) -> List[Agent]:
        """Create the specialized agents for function creation."""
        
        # Code Generator Agent
        code_generator = Agent(
            role='Python Code Generator',
            goal='Generate high-quality, production-ready Python functions based on API documentation',
            backstory="""You are an expert Python developer with deep knowledge of APIs, 
            HTTP clients, data processing, and best practices. You excel at creating clean, 
            well-documented, and efficient Python functions that follow PEP 8 standards.""",
            tools=self.tools,
            llm=self.llm,
            verbose=self.verbose
        )
        
        # Code Reviewer Agent
        code_reviewer = Agent(
            role='Code Reviewer and Tester',
            goal='Review, validate, and test generated Python functions to ensure quality and correctness',
            backstory="""You are a senior Python developer and code reviewer with expertise 
            in testing, debugging, and code quality. You ensure functions are robust, 
            handle errors gracefully, and meet the specified requirements.""",
            tools=self.tools,
            llm=self.llm,
            verbose=self.verbose
        )
        
        # Documentation Specialist Agent
        doc_specialist = Agent(
            role='Documentation and Integration Specialist',
            goal='Create comprehensive documentation, usage examples, and integration guidance',
            backstory="""You are a technical writer and integration specialist who excels 
            at creating clear documentation, usage examples, and helping users understand 
            how to integrate and use the generated functions effectively.""",
            tools=self.tools,
            llm=self.llm,
            verbose=self.verbose
        )
        
        return [code_generator, code_reviewer, doc_specialist]
    
    def _create_tasks(self, request: FunctionCreationRequest) -> List[Task]:
        """Create the tasks for function creation workflow."""
        
        agents = self._create_agents()
        code_generator, code_reviewer, doc_specialist = agents
        
        # Task 1: Generate the function
        generate_task = Task(
            description=f"""Analyze the following API documentation and create a Python function:
            
            API Documentation:
            {request.api_documentation}
            
            Function Name: {request.function_name or 'auto-generated'}
            Description: {request.description or 'auto-generated'}
            Requirements: {', '.join(request.requirements) if request.requirements else 'auto-detected'}
            Additional Context: {request.additional_context or 'none'}
            
            Create a complete, working Python function that:
            1. Implements the API functionality described
            2. Includes proper error handling
            3. Has comprehensive docstrings
            4. Follows PEP 8 standards
            5. Is ready for production use
            
            Return the complete function code as a string.""",
            agent=code_generator,
            expected_output="Complete Python function code as a string"
        )
        
        # Task 2: Review and test the function
        review_task = Task(
            description=f"""Review and test the generated Python function:
            
            Function Code:
            {{function_code}}
            
            Test Examples: {request.test_examples if request.test_examples else 'Create basic tests'}
            
            Tasks:
            1. Validate the code syntax
            2. Test the function with provided examples
            3. Identify and fix any issues
            4. Ensure error handling is robust
            5. Verify the function meets all requirements
            
            Return the improved function code and test results.""",
            agent=code_reviewer,
            expected_output="Improved function code and test results",
            context=[generate_task]
        )
        
        # Task 3: Create documentation and examples
        doc_task = Task(
            description=f"""Create comprehensive documentation for the final function:
            
            Final Function Code:
            {{function_code}}
            
            Create:
            1. Clear usage examples
            2. Installation requirements
            3. Integration guidance
            4. Troubleshooting tips
            
            Return a complete response with the function, documentation, and examples.""",
            agent=doc_specialist,
            expected_output="Complete function with documentation and examples",
            context=[review_task]
        )
        
        return [generate_task, review_task, doc_task]
    
    def _create_crew(self) -> Crew:
        """Create the CrewAI crew."""
        return Crew(
            agents=self._create_agents(),
            tasks=[],  # Tasks will be created dynamically
            verbose=self.verbose,
            max_iterations=self.max_iterations
        )
    
    def create_function(self, request: FunctionCreationRequest) -> FunctionCreationResponse:
        """
        Create a Python function based on the provided API documentation.
        
        Args:
            request: FunctionCreationRequest containing API documentation and requirements
            
        Returns:
            FunctionCreationResponse containing the created function and metadata
        """
        try:
            # Create tasks for this specific request
            tasks = self._create_tasks(request)
            
            # Update crew with new tasks
            self.crew.tasks = tasks
            
            # Execute the crew
            logger.info(f"Starting function creation for: {request.function_name or 'auto-generated'}")
            result = self.crew.kickoff()
            
            # Parse the result and create response
            # The result will contain the final function code and documentation
            response = self._parse_crew_result(result, request)
            
            logger.info(f"Successfully created function: {response.function_name}")
            return response
            
        except Exception as e:
            logger.error(f"Error creating function: {e}")
            raise RuntimeError(f"Failed to create function: {str(e)}")
    
    def _parse_crew_result(self, result: str, request: FunctionCreationRequest) -> FunctionCreationResponse:
        """
        Parse the crew execution result and create a structured response.
        
        Args:
            result: Raw result from crew execution
            request: Original function creation request
            
        Returns:
            Structured FunctionCreationResponse
        """
        # This is a simplified parser - in practice, you might want more sophisticated parsing
        # or have the agents return structured data
        
        # Extract function name
        function_name = request.function_name or "generated_function"
        
        # For now, assume the result contains the function code
        # In a real implementation, you'd parse this more carefully
        function_code = result
        
        # Create basic response
        response = FunctionCreationResponse(
            function_code=function_code,
            function_name=function_name,
            description=request.description or f"Function generated from API documentation",
            requirements=request.requirements or [],
            usage_example=f"# Usage example for {function_name}\nresult = {function_name}(your_params)",
            metadata={
                "source": "crewai_agent",
                "api_documentation_length": len(request.api_documentation),
                "test_examples_provided": len(request.test_examples) if request.test_examples else 0
            }
        )
        
        return response
