# -*- coding: utf-8 -*-
"""LLM Service Implementation.

Copyright 2025
SPDX-License-Identifier: Apache-2.0

This module provides a lightweight service for making direct HTTP calls to OpenAI-compatible
LLM APIs. It replaces the CrewAI dependency with simple HTTP requests for better Python
version compatibility.
"""

# Standard
import json
import logging
from typing import Any, Dict, List, Optional

# Third-Party
import httpx

# First-Party
from mcpgateway.config import settings

logger = logging.getLogger(__name__)


class LLMServiceError(Exception):
    """Base exception for LLM service errors."""


class LLMConfigurationError(LLMServiceError):
    """Raised when LLM is not properly configured."""


class LLMAPIError(LLMServiceError):
    """Raised when LLM API call fails."""


class LLMService:
    """Direct HTTP client for OpenAI-compatible LLM APIs.

    This service provides a simple interface for making chat completion requests
    to OpenAI-compatible APIs without requiring the CrewAI dependency.

    Examples:
        >>> service = LLMService()
        >>> if service.is_configured():
        ...     response = await service.chat_completion([
        ...         {"role": "system", "content": "You are a helpful assistant."},
        ...         {"role": "user", "content": "Hello!"}
        ...     ])
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ):
        """Initialize the LLM service.

        Args:
            base_url: Base URL for the LLM API. Defaults to settings.llm_api_base_url.
            api_key: API key for authentication. Defaults to settings.llm_api_key.
            model: Model name to use. Defaults to settings.llm_model.
            timeout: Request timeout in seconds. Defaults to settings.llm_timeout.
            max_tokens: Maximum tokens for responses. Defaults to settings.llm_max_tokens.
            temperature: Temperature for responses. Defaults to settings.llm_temperature.
        """
        self.base_url = (base_url or settings.llm_api_base_url).rstrip("/")
        self.api_key = api_key or settings.llm_api_key.get_secret_value()
        self.model = model or settings.llm_model
        self.timeout = timeout or settings.llm_timeout
        self.max_tokens = max_tokens or settings.llm_max_tokens
        self.temperature = temperature if temperature is not None else settings.llm_temperature

    def is_configured(self) -> bool:
        """Check if the LLM service is properly configured.

        Returns:
            True if API key is set and base URL is valid, False otherwise.
        """
        return bool(self.api_key and self.base_url)

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Make a chat completion request to the LLM API.

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys.
            model: Model to use (overrides default).
            temperature: Temperature for this request (overrides default).
            max_tokens: Max tokens for this request (overrides default).
            response_format: Optional response format specification (e.g., {"type": "json_object"}).

        Returns:
            The assistant's response content as a string.

        Raises:
            LLMConfigurationError: If the service is not properly configured.
            LLMAPIError: If the API request fails.
        """
        if not self.is_configured():
            raise LLMConfigurationError("LLM service is not configured. Set LLM_API_KEY and LLM_API_BASE_URL environment variables.")

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }

        if response_format:
            payload["response_format"] = response_format

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                logger.debug(f"LLM request to {url} with model={payload['model']}")
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()

                data = response.json()
                content = data["choices"][0]["message"]["content"]
                logger.debug(f"LLM response (first 500 chars): {content[:500]}")
                return content

        except httpx.TimeoutException as e:
            logger.error(f"LLM API request timed out after {self.timeout}s: {e}")
            raise LLMAPIError(f"LLM API request timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM API returned error status {e.response.status_code}: {e.response.text}")
            raise LLMAPIError(f"LLM API error ({e.response.status_code}): {e.response.text}") from e
        except httpx.RequestError as e:
            logger.error(f"LLM API request failed: {e}")
            raise LLMAPIError(f"LLM API request failed: {e}") from e
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"Failed to parse LLM API response: {e}")
            raise LLMAPIError(f"Failed to parse LLM API response: {e}") from e

    async def chat_completion_json(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Make a chat completion request expecting a JSON response.

        This method requests JSON output format from the API and parses the response.

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys.
            model: Model to use (overrides default).
            temperature: Temperature for this request (overrides default).
            max_tokens: Max tokens for this request (overrides default).

        Returns:
            Parsed JSON response as a dictionary.

        Raises:
            LLMConfigurationError: If the service is not properly configured.
            LLMAPIError: If the API request fails or response is not valid JSON.
        """
        # Request JSON response format
        response_text = await self.chat_completion(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )

        try:
            result = json.loads(response_text)
            logger.debug(f"LLM JSON response keys: {list(result.keys()) if isinstance(result, dict) else f'array[{len(result)}]'}")
            return result
        except json.JSONDecodeError as e:
            # Try to extract JSON from the response if it's wrapped in markdown
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
_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """Get the global LLM service instance.

    Returns:
        The global LLMService instance.
    """
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
