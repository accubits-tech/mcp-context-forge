# -*- coding: utf-8 -*-
"""Authentication Extraction Service Implementation.

Copyright 2025
SPDX-License-Identifier: Apache-2.0

Deep authentication extraction from API documentation content.
Extracts custom headers, OAuth details, API key placement, and confidence scoring.
Replaces the shallow regex keyword matching in api_doc_parser_service.
"""

# Standard
import re
from typing import Any, Dict, List, Optional

# First-Party
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


class AuthExtractionService:
    """Deep authentication extraction from API documentation pages."""

    def extract_auth_from_pages(self, pages: Optional[List] = None, all_content: str = "") -> Dict[str, Any]:
        """Extract comprehensive authentication information from documentation.

        Args:
            pages: Optional list of CrawledPage objects (for page-level analysis).
            all_content: Full aggregated text content.

        Returns:
            Dict with methods, details, custom_headers, oauth_config, api_key_config, confidence.
        """
        result: Dict[str, Any] = {
            "methods": [],
            "details": {},
            "custom_headers": [],
            "oauth_config": {},
            "api_key_config": {},
            "confidence": 0,
        }

        if not all_content and pages:
            all_content = "\n\n".join(getattr(p, "content", "") for p in pages)

        if not all_content:
            return result

        content_lower = all_content.lower()

        # Detect auth methods with tighter patterns
        detected_methods = self._detect_auth_methods(content_lower)
        result["methods"] = list(detected_methods.keys())
        result["details"] = detected_methods

        # Extract custom headers from curl examples and header declarations
        custom_headers = self._extract_custom_headers(all_content)
        result["custom_headers"] = custom_headers

        # Extract OAuth details
        oauth_config = self._extract_oauth_details(all_content)
        if oauth_config:
            result["oauth_config"] = oauth_config
            if "oauth" not in result["methods"]:
                result["methods"].append("oauth")

        # Extract API key details
        api_key_config = self._extract_api_key_details(all_content)
        if api_key_config:
            result["api_key_config"] = api_key_config
            if "api_key" not in result["methods"]:
                result["methods"].append("api_key")

        # Compute confidence score (0-10)
        result["confidence"] = self._compute_confidence(result)

        logger.info(f"Auth extraction: methods={result['methods']}, confidence={result['confidence']}, custom_headers={len(custom_headers)}")
        return result

    def _detect_auth_methods(self, content_lower: str) -> Dict[str, Any]:
        """Detect authentication methods from content with match counts."""
        auth_patterns = {
            "bearer": r"\b(bearer\s+token|authorization:\s*bearer|jwt\s+(token|auth)|bearer\s+auth)\b",
            "api_key": r"\b(api[\s_-]*key|x-api-key|apikey)\b",
            "basic": r"\b(basic\s+auth(?:entication)?|http\s+basic|basic\s+credentials)\b",
            "oauth": r"\b(oauth\s*2?\.?0?|o-?auth\s+(?:flow|token|client|grant|scope))\b",
        }

        detected = {}
        for auth_type, pattern in auth_patterns.items():
            matches = re.findall(pattern, content_lower)
            if matches:
                detected[auth_type] = {"match_count": len(matches)}

        return detected

    def _extract_custom_headers(self, content: str) -> List[Dict[str, str]]:
        """Extract custom authentication header names from content.

        Looks for:
        - Headers in curl -H examples
        - Header declarations in documentation tables/lists
        - X- prefixed headers in code blocks
        """
        headers: List[Dict[str, str]] = []
        seen: set = set()

        # Pattern 1: curl -H "Header-Name: value" patterns
        curl_header_pattern = r"""curl\s[^"']*-H\s+["']([A-Z][A-Za-z0-9-]+)\s*:\s*([^"']+)["']"""
        for match in re.finditer(curl_header_pattern, content):
            header_name = match.group(1).strip()
            header_value_hint = match.group(2).strip()
            if header_name.lower() not in ("content-type", "accept", "user-agent", "cache-control") and header_name not in seen:
                seen.add(header_name)
                headers.append({"name": header_name, "value_hint": header_value_hint, "source": "curl_example"})

        # Pattern 2: Authorization header variants
        auth_header_pattern = r"""(?:["']|^|\s)(Authorization)\s*:\s*(\w+)\s"""
        for match in re.finditer(auth_header_pattern, content, re.MULTILINE):
            header_name = match.group(1)
            scheme = match.group(2)
            key = f"{header_name}:{scheme}"
            if key not in seen:
                seen.add(key)
                headers.append({"name": header_name, "value_hint": f"{scheme} <token>", "source": "header_declaration"})

        # Pattern 3: X- prefixed custom headers in code/docs
        x_header_pattern = r"\b(X-[A-Z][A-Za-z0-9-]+(?:-[A-Za-z0-9]+)*)\b"
        for match in re.finditer(x_header_pattern, content):
            header_name = match.group(1)
            if header_name not in seen and len(header_name) <= 50:
                seen.add(header_name)
                headers.append({"name": header_name, "value_hint": "", "source": "x_header"})

        # Pattern 4: Header name in table rows (e.g., "| Header | Description |")
        table_header_pattern = r"\|\s*([A-Z][A-Za-z0-9-]+)\s*\|[^|]*(?:auth|token|key|credential|secret)[^|]*\|"
        for match in re.finditer(table_header_pattern, content, re.IGNORECASE):
            header_name = match.group(1).strip()
            if header_name not in seen and len(header_name) <= 50:
                seen.add(header_name)
                headers.append({"name": header_name, "value_hint": "", "source": "doc_table"})

        return headers

    def _extract_oauth_details(self, content: str) -> Dict[str, Any]:
        """Extract OAuth configuration details from content."""
        oauth_config: Dict[str, Any] = {}

        # Token URL patterns
        token_url_patterns = [
            r"(?:token\s*(?:url|endpoint|uri))\s*[:=]\s*[\"']?(https?://[^\s\"']+)",
            r"(?:POST|post)\s+(https?://[^\s\"']+/(?:oauth|token|auth)[^\s\"']*)",
            r"(https?://[^\s\"']+/oauth2?/token)",
        ]
        for pattern in token_url_patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                oauth_config["token_url"] = match.group(1).rstrip("/")
                break

        # Authorization URL patterns
        auth_url_patterns = [
            r"(?:authorization\s*(?:url|endpoint|uri)|authorize\s*(?:url|endpoint))\s*[:=]\s*[\"']?(https?://[^\s\"']+)",
            r"(https?://[^\s\"']+/oauth2?/authorize)",
        ]
        for pattern in auth_url_patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                oauth_config["authorization_url"] = match.group(1).rstrip("/")
                break

        # Scopes
        scope_patterns = [
            r"(?:scopes?|permissions?)\s*[:=]\s*[\"']?([a-z0-9_.:,\s]+)[\"']?",
            r"scope\s*[:=]\s*[\"']([^\"']+)[\"']",
        ]
        for pattern in scope_patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                raw_scopes = match.group(1)
                scopes = [s.strip() for s in re.split(r"[,\s]+", raw_scopes) if s.strip() and len(s.strip()) > 1]
                if scopes:
                    oauth_config["scopes"] = scopes
                    break

        # Grant types
        grant_type_pattern = r"(?:grant[_\s]*type|flow)\s*[:=]?\s*[\"']?(authorization[_\s]*code|client[_\s]*credentials|implicit|password|device[_\s]*code)[\"']?"
        match = re.search(grant_type_pattern, content, re.IGNORECASE)
        if match:
            oauth_config["grant_type"] = match.group(1).strip().replace(" ", "_")

        return oauth_config

    def _extract_api_key_details(self, content: str) -> Dict[str, Any]:
        """Extract API key configuration details from content."""
        api_key_config: Dict[str, Any] = {}

        # Header name for API key
        header_patterns = [
            r"(?:header|send|include|pass)\s+(?:the\s+)?(?:api\s*key|token)\s+(?:in|as|using)\s+(?:the\s+)?[\"']?([A-Z][A-Za-z0-9-]+)[\"']?",
            r"[\"']?(X-API-Key|api-key|X-Auth-Token|X-Access-Token|Authorization)[\"']?\s*:\s*[\"']?(?:your[_\s]*)?(?:api[_\s]*)?key",
        ]
        for pattern in header_patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                api_key_config["header_name"] = match.group(1)
                api_key_config["placement"] = "header"
                break

        # Check for query parameter placement
        query_key_pattern = r"(?:query\s*(?:parameter|param|string))\s*[:=]?\s*[\"']?(?:api[_\s]*key|apikey|key|token)[\"']?"
        if re.search(query_key_pattern, content, re.IGNORECASE):
            if "placement" not in api_key_config:
                api_key_config["placement"] = "query"

        # API key prefix (e.g., "Bearer ", "Token ", "Api-Key ")
        prefix_pattern = r"(?:prefix|prepend|format)\s*[:=]?\s*[\"']([A-Za-z]+ )[\"']"
        match = re.search(prefix_pattern, content, re.IGNORECASE)
        if match:
            api_key_config["prefix"] = match.group(1)

        return api_key_config

    def _compute_confidence(self, result: Dict[str, Any]) -> int:
        """Compute confidence score (0-10) for the auth extraction."""
        score = 0

        # Base score from number of methods detected
        num_methods = len(result["methods"])
        if num_methods >= 1:
            score += 3
        if num_methods >= 2:
            score += 1

        # Bonus for custom headers found
        if result["custom_headers"]:
            score += 2

        # Bonus for specific OAuth config
        oauth = result.get("oauth_config", {})
        if oauth.get("token_url"):
            score += 2
        if oauth.get("scopes"):
            score += 1

        # Bonus for specific API key config
        api_key = result.get("api_key_config", {})
        if api_key.get("header_name"):
            score += 1
        if api_key.get("placement"):
            score += 1

        return min(score, 10)
