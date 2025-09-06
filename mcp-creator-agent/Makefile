.PHONY: help install install-dev test lint clean demo run-test

# Default target
help:
	@echo "MCP Creator Agent - Available commands:"
	@echo ""
	@echo "Installation:"
	@echo "  install        Install production dependencies"
	@echo "  install-dev    Install development dependencies"
	@echo ""
	@echo "Development:"
	@echo "  test           Run basic tests"
	@echo "  lint           Run linting checks"
	@echo "  demo           Run the demo script"
	@echo "  clean          Clean up generated files"
	@echo ""
	@echo "Usage:"
	@echo "  run-test       Run the simple test script"
	@echo "  help           Show this help message"

# Install production dependencies
install:
	pip install -r requirements.txt

# Install development dependencies
install-dev:
	pip install -e ".[dev]"

# Run basic tests
test:
	python test_simple.py

# Run linting
lint:
	ruff check .
	black --check .
	isort --check-only .

# Format code
format:
	black .
	isort .

# Run demo
demo:
	python demo.py

# Clean generated files
clean:
	rm -f *.pyc
	rm -f generated_function.py
	rm -f demo_*.py
	rm -rf __pycache__
	rm -rf mcp_creator_agent/__pycache__
	rm -rf .pytest_cache
	rm -rf .coverage
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# Run simple test
run-test:
	python test_simple.py

# Setup environment
setup-env:
	@if [ ! -f .env ]; then \
		echo "Creating .env file from template..."; \
		cp env.example .env; \
		echo "Please edit .env with your configuration"; \
	else \
		echo ".env file already exists"; \
	fi

# Check environment
check-env:
	@if [ -f .env ]; then \
		echo "Environment file exists"; \
		if grep -q "your-openai-api-key-here" .env; then \
			echo "⚠️  Please update OPENAI_API_KEY in .env"; \
		else \
			echo "✅ Environment appears to be configured"; \
		fi; \
	else \
		echo "❌ No .env file found. Run 'make setup-env' to create one"; \
	fi

# Install pre-commit hooks
install-hooks:
	pre-commit install

# Run pre-commit on all files
pre-commit:
	pre-commit run --all-files

# Show project info
info:
	@echo "MCP Creator Agent"
	@echo "================="
	@echo "Python version: $(shell python --version)"
	@echo "Pip version: $(shell pip --version)"
	@echo "Project location: $(shell pwd)"
	@echo ""
	@echo "Available commands: make help"
