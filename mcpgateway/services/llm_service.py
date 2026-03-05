# -*- coding: utf-8 -*-
"""LLM Service Implementation.

Copyright 2025
SPDX-License-Identifier: Apache-2.0

This module provides a lightweight service for making direct HTTP calls to
Anthropic's Messages API for LLM completions.
"""

# Standard
import json
import logging
import re
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
    """Direct HTTP client for Anthropic's Messages API.

    This service provides a simple interface for making chat completion requests
    to Anthropic's Messages API.

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
    ) -> str:
        """Make a chat completion request to the Anthropic Messages API.

        Args:
            messages: List of message dictionaries with 'role' and 'content' keys.
                      System messages are extracted and sent as the top-level 'system' field.
            model: Model to use (overrides default).
            temperature: Temperature for this request (overrides default).
            max_tokens: Max tokens for this request (overrides default).

        Returns:
            The assistant's response content as a string.

        Raises:
            LLMConfigurationError: If the service is not properly configured.
            LLMAPIError: If the API request fails.
        """
        if not self.is_configured():
            raise LLMConfigurationError("LLM service is not configured. Set LLM_API_KEY and LLM_API_BASE_URL environment variables.")

        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        resolved_model = model or self.model
        resolved_max_tokens = max_tokens or self.max_tokens
        resolved_temperature = temperature if temperature is not None else self.temperature

        # Extract system messages into top-level system field (Anthropic API requirement)
        # If the last message is role=assistant, it serves as a prefill — Anthropic
        # continues from where the prefill left off, so we prepend it to the response.
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

        payload: Dict[str, Any] = {
            "model": resolved_model,
            "messages": non_system_messages,
            "max_tokens": resolved_max_tokens,
            "temperature": resolved_temperature,
        }

        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        # Log the request payload (excluding full message content for brevity)
        log_payload = {k: v for k, v in payload.items() if k not in ("messages", "system")}
        log_payload["message_count"] = len(payload["messages"])
        log_payload["has_system"] = "system" in payload
        log_payload["total_prompt_chars"] = sum(len(m.get("content", "")) for m in messages)
        logger.info(f"LLM request: {json.dumps(log_payload)}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()

                data = response.json()
                content = assistant_prefill + data["content"][0]["text"]
                usage = data.get("usage", {})
                stop_reason = data.get("stop_reason", "unknown")

                logger.info(
                    f"LLM response: stop_reason={stop_reason}, "
                    f"input_tokens={usage.get('input_tokens', '?')}, "
                    f"output_tokens={usage.get('output_tokens', '?')}, "
                    f"content_length={len(content) if content else 0}"
                )

                if not content:
                    logger.error(f"LLM returned empty content. Full response: {json.dumps(data)[:2000]}")
                    raise LLMAPIError(f"LLM returned empty content (stop_reason={stop_reason})")

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

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        """Extract a JSON object from text that may contain markdown fences or be truncated.

        Handles these cases in order:
        1. Direct JSON parse
        2. JSON wrapped in ```json ... ``` or ``` ... ``` fences
        3. First { to last } substring
        4. Truncated JSON (stop_reason=max_tokens) — close open arrays/objects

        Args:
            text: Raw text potentially containing JSON.

        Returns:
            Parsed JSON dictionary.

        Raises:
            json.JSONDecodeError: If no valid JSON could be extracted.
        """
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
            # Remove any trailing incomplete string value (unmatched quote)
            candidate = re.sub(r',\s*"[^"]*$', "", candidate)
            candidate = re.sub(r",\s*$", "", candidate)
            # Count open/close brackets
            open_braces = candidate.count("{") - candidate.count("}")
            open_brackets = candidate.count("[") - candidate.count("]")
            if open_braces > 0 or open_brackets > 0:
                candidate += "]" * open_brackets + "}" * open_braces
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass

        raise json.JSONDecodeError("No valid JSON found", text, 0)

    async def chat_completion_json(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Make a chat completion request expecting a JSON response.

        This method relies on prompt engineering to get JSON output from the API
        and parses the response. It robustly handles markdown-wrapped and
        truncated JSON responses.

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
        response_text = await self.chat_completion(
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
