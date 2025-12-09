"""Configuration for MCP Creator Agent.

Environment variables:
- MCP_CREATOR_LLM_API_BASE_URL: Base URL for OpenAI-compatible LLM API
- MCP_CREATOR_LLM_API_KEY: API key for LLM service
- MCP_CREATOR_LLM_MODEL: Model name to use
- MCP_CREATOR_LLM_TIMEOUT: Request timeout in seconds
- MCP_CREATOR_LLM_TEMPERATURE: Temperature for responses
"""

import os
from dataclasses import dataclass


@dataclass
class LLMConfig:
    """LLM configuration settings."""

    api_base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o"
    timeout: int = 120
    temperature: float = 0.7
    max_tokens: int = 4096

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """Load configuration from environment variables."""
        return cls(
            api_base_url=os.getenv("MCP_CREATOR_LLM_API_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.getenv("MCP_CREATOR_LLM_API_KEY", ""),
            model=os.getenv("MCP_CREATOR_LLM_MODEL", "gpt-4o"),
            timeout=int(os.getenv("MCP_CREATOR_LLM_TIMEOUT", "120")),
            temperature=float(os.getenv("MCP_CREATOR_LLM_TEMPERATURE", "0.7")),
            max_tokens=int(os.getenv("MCP_CREATOR_LLM_MAX_TOKENS", "4096")),
        )

    def is_configured(self) -> bool:
        """Check if the LLM is properly configured."""
        return bool(self.api_key and self.api_base_url)


# Global config instance
_config: LLMConfig | None = None


def get_config() -> LLMConfig:
    """Get the global LLM configuration."""
    global _config
    if _config is None:
        _config = LLMConfig.from_env()
    return _config
