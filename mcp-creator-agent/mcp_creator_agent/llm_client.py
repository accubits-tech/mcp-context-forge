"""LLM Client for MCP Creator Agent.

This module provides a synchronous HTTP client for making direct calls to
Anthropic's Messages API.
"""

import json
import logging
import re
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
    """Synchronous HTTP client for Anthropic's Messages API.

    This client provides a simple interface for making chat completion requests
    to Anthropic's Messages API without requiring the CrewAI dependency.
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
    ) -> str:
        """Make a chat completion request to the Anthropic Messages API.

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys.
                      System messages are extracted and sent as the top-level 'system' field.
                      A trailing assistant message serves as a prefill and is prepended to the response.
            model: Model to use (overrides config).
            temperature: Temperature for this request (overrides config).
            max_tokens: Max tokens for this request (overrides config).

        Returns:
            The assistant's response content as a string.

        Raises:
            LLMConfigurationError: If the client is not properly configured.
            LLMAPIError: If the API request fails.
        """
        if not self.is_configured():
            raise LLMConfigurationError("LLM client is not configured. Set MCP_CREATOR_LLM_API_KEY environment variable.")

        url = f"{self.config.api_base_url.rstrip('/')}/v1/messages"
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        # Extract system messages into top-level system field (Anthropic API requirement)
        system_parts = []
        non_system_messages = []
        assistant_prefill = ""
        for msg in messages:
            if msg.get("role") == "system":
                system_parts.append(msg.get("content", ""))
            else:
                non_system_messages.append(msg)

        if non_system_messages and non_system_messages[-1].get("role") == "assistant":
            assistant_prefill = non_system_messages[-1].get("content", "")

        payload: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": non_system_messages,
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
        }

        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.config.timeout,
            )
            response.raise_for_status()

            data = response.json()
            return assistant_prefill + data["content"][0]["text"]

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

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """Extract a JSON object from text that may contain markdown fences or be truncated."""
        stripped = text.strip()

        # 1. Direct parse
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

        # 2. Extract from markdown code fences
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 3. Find the outermost { ... } span
        first_brace = stripped.find("{")
        if first_brace >= 0:
            last_brace = stripped.rfind("}")
            if last_brace > first_brace:
                try:
                    return json.loads(stripped[first_brace : last_brace + 1])
                except json.JSONDecodeError:
                    pass

            # 4. Truncated JSON — try to close open brackets/braces
            candidate = stripped[first_brace:]
            candidate = re.sub(r',\s*"[^"]*$', "", candidate)
            candidate = re.sub(r",\s*$", "", candidate)
            open_braces = candidate.count("{") - candidate.count("}")
            open_brackets = candidate.count("[") - candidate.count("]")
            if open_braces > 0 or open_brackets > 0:
                candidate += "]" * open_brackets + "}" * open_braces
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass

        raise json.JSONDecodeError("No valid JSON found", text, 0)

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
        )

        try:
            result = self._extract_json(response_text)
            logger.debug(f"LLM JSON response keys: {list(result.keys()) if isinstance(result, dict) else f'array[{len(result)}]'}")
            return result
        except json.JSONDecodeError as e:
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
