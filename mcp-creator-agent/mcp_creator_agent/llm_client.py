"""LLM Client for MCP Creator Agent.

This module provides a synchronous HTTP client for making direct calls to
OpenAI-compatible LLM APIs, replacing the CrewAI dependency.
"""

import json
import logging
from typing import Any

import requests

from .config import LLMConfig, get_config

logger = logging.getLogger(__name__)


class LLMClientError(Exception):
    """Base exception for LLM client errors."""


class LLMConfigurationError(LLMClientError):
    """Raised when LLM is not properly configured."""


class LLMAPIError(LLMClientError):
    """Raised when LLM API call fails."""


class LLMClient:
    """Synchronous HTTP client for OpenAI-compatible LLM APIs.

    This client provides a simple interface for making chat completion requests
    to OpenAI-compatible APIs without requiring the CrewAI dependency.
    """

    def __init__(self, config: LLMConfig | None = None):
        """Initialize the LLM client.

        Args:
            config: Optional LLMConfig instance. If not provided, uses global config.
        """
        self.config = config or get_config()

    def is_configured(self) -> bool:
        """Check if the LLM client is properly configured."""
        return self.config.is_configured()

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Make a chat completion request to the LLM API.

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys.
            model: Model to use (overrides config).
            temperature: Temperature for this request (overrides config).
            max_tokens: Max tokens for this request (overrides config).
            response_format: Optional response format specification.

        Returns:
            The assistant's response content as a string.

        Raises:
            LLMConfigurationError: If the client is not properly configured.
            LLMAPIError: If the API request fails.
        """
        if not self.is_configured():
            raise LLMConfigurationError(
                "LLM client is not configured. Set MCP_CREATOR_LLM_API_KEY environment variable."
            )

        url = f"{self.config.api_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
        }

        if response_format:
            payload["response_format"] = response_format

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.config.timeout,
            )
            response.raise_for_status()

            data = response.json()
            return data["choices"][0]["message"]["content"]

        except requests.exceptions.Timeout as e:
            logger.error(f"LLM API request timed out after {self.config.timeout}s: {e}")
            raise LLMAPIError(f"LLM API request timed out: {e}") from e
        except requests.exceptions.HTTPError as e:
            logger.error(f"LLM API returned error status: {e.response.status_code}: {e.response.text}")
            raise LLMAPIError(f"LLM API error ({e.response.status_code}): {e.response.text}") from e
        except requests.exceptions.RequestException as e:
            logger.error(f"LLM API request failed: {e}")
            raise LLMAPIError(f"LLM API request failed: {e}") from e
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"Failed to parse LLM API response: {e}")
            raise LLMAPIError(f"Failed to parse LLM API response: {e}") from e

    def chat_completion_json(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Make a chat completion request expecting a JSON response.

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys.
            model: Model to use (overrides config).
            temperature: Temperature for this request (overrides config).
            max_tokens: Max tokens for this request (overrides config).

        Returns:
            Parsed JSON response as a dictionary.

        Raises:
            LLMConfigurationError: If the client is not properly configured.
            LLMAPIError: If the API request fails or response is not valid JSON.
        """
        response_text = self.chat_completion(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )

        try:
            return json.loads(response_text)
        except json.JSONDecodeError as e:
            # Try to extract JSON from markdown code blocks
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                if json_end > json_start:
                    try:
                        return json.loads(response_text[json_start:json_end].strip())
                    except json.JSONDecodeError:
                        pass
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                if json_end > json_start:
                    try:
                        return json.loads(response_text[json_start:json_end].strip())
                    except json.JSONDecodeError:
                        pass

            logger.error(f"LLM response is not valid JSON: {response_text[:500]}")
            raise LLMAPIError(f"LLM response is not valid JSON: {e}") from e


# Global singleton instance
_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Get the global LLM client instance."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
