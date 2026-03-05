# -*- coding: utf-8 -*-
"""Post-processing utilities for documentation crawl results.

Copyright 2025
SPDX-License-Identifier: Apache-2.0

Standalone functions for OpenAPI spec detection, auth page classification,
and base URL inference. Used by both DocumentationCrawlerService and
FirecrawlCrawlerService.
"""

# Standard
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

# Third-Party
from bs4 import BeautifulSoup
import httpx
import yaml

# First-Party
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


# Common paths where OpenAPI specs are often served
_COMMON_SPEC_PATHS = [
    "/openapi.json",
    "/openapi.yaml",
    "/swagger.json",
    "/swagger.yaml",
    "/v3/api-docs",
    "/api/openapi.json",
    "/api/swagger.json",
    "/api/v1/openapi.json",
    "/api/v2/openapi.json",
    "/api/v3/openapi.json",
    "/docs/openapi.json",
    "/api-docs",
    "/api-docs.json",
]

# Keywords in URL or title that suggest an authentication/authorization page
_AUTH_PAGE_KEYWORDS = [
    "auth",
    "authentication",
    "authorization",
    "oauth",
    "api-key",
    "api-keys",
    "apikey",
    "credentials",
    "security",
    "token",
    "tokens",
    "getting-started/authentication",
]


def _validate_url_not_internal(target_url: str) -> None:
    """Validate that a URL does not point to internal/private network addresses."""
    # First-Party
    from mcpgateway.utils.url_validation import validate_url_not_internal  # pylint: disable=import-outside-toplevel

    validate_url_not_internal(target_url)


def try_parse_spec(content: str) -> Optional[Dict[str, Any]]:
    """Try to parse content as OpenAPI spec (JSON or YAML).

    Args:
        content: Raw text content that may be an OpenAPI/Swagger spec.

    Returns:
        Parsed spec dict if valid, None otherwise.
    """
    # Try JSON
    try:
        data = json.loads(content)
        if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try YAML
    try:
        data = yaml.safe_load(content)
        if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
            return data
    except Exception:
        pass

    return None


def extract_spec_url_from_html(html: str, base_url: str) -> Optional[str]:
    """Extract OpenAPI/Swagger spec URL from HTML content.

    Looks for:
    - SwaggerUIBundle({url: "..."}) in script tags
    - <redoc spec-url="..."> elements
    - <link> tags with openapi/swagger in href

    Args:
        html: Raw HTML content.
        base_url: Base URL for resolving relative URLs.

    Returns:
        Absolute spec URL if found, None otherwise.
    """
    # SwaggerUIBundle url pattern
    swagger_ui_match = re.search(r'SwaggerUIBundle\s*\(\s*\{[^}]*url\s*:\s*["\']([^"\']+)["\']', html)
    if swagger_ui_match:
        return urljoin(base_url, swagger_ui_match.group(1))

    # Redoc spec-url attribute
    redoc_match = re.search(r'<redoc[^>]+spec-url\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    if redoc_match:
        return urljoin(base_url, redoc_match.group(1))

    # spec-url in any element (some custom doc renderers)
    spec_url_match = re.search(r'spec-url\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    if spec_url_match:
        return urljoin(base_url, spec_url_match.group(1))

    # Link tags with openapi/swagger references
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("link", href=True):
        href = link["href"].lower()
        if any(kw in href for kw in ["openapi", "swagger", "api-docs"]):
            return urljoin(base_url, link["href"])

    # Script tags with spec URL references
    for script in soup.find_all("script"):
        if script.string:
            # Look for spec URL assignment patterns
            url_match = re.search(r'(?:spec|swagger|openapi)(?:Url|_url|URL)\s*[:=]\s*["\']([^"\']+)["\']', script.string)
            if url_match:
                return urljoin(base_url, url_match.group(1))

    return None


async def probe_common_spec_paths(doc_url: str, user_agent: str = "MCP-Gateway Documentation Crawler 1.0") -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """HEAD/GET common spec paths to find an OpenAPI spec.

    Args:
        doc_url: Documentation URL to derive base from.
        user_agent: User-Agent header for requests.

    Returns:
        Tuple of (spec_url_or_None, parsed_spec_dict_or_None).
    """
    parsed = urlparse(doc_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for path in _COMMON_SPEC_PATHS:
            probe_url = base + path
            try:
                _validate_url_not_internal(probe_url)
                resp = await client.get(probe_url, headers={"User-Agent": user_agent, "Accept": "application/json,application/yaml"})
                if resp.status_code == 200:
                    spec = try_parse_spec(resp.text)
                    if spec:
                        logger.info(f"Found OpenAPI spec at probed path: {probe_url}")
                        return probe_url, spec
            except Exception:
                continue

    return None, None


async def detect_openapi_spec(
    url: str,
    content_type: str,
    content_text: str,
    fetch_url_fn: Any,
    user_agent: str = "MCP-Gateway Documentation Crawler 1.0",
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Check if content IS an OpenAPI spec or embeds/links to one.

    Args:
        url: The URL the content was fetched from.
        content_type: Content-Type header value.
        content_text: Raw response text.
        fetch_url_fn: Async callable(url) -> (final_url, response_text) for following spec URLs.
        user_agent: User-Agent header for probing requests.

    Returns:
        Tuple of (spec_url_or_None, parsed_spec_dict_or_None).
    """
    # Check if response itself is a spec (JSON or YAML)
    if "json" in content_type or "yaml" in content_type:
        spec = try_parse_spec(content_text)
        if spec:
            return url, spec

    # If HTML, look for embedded spec references
    if "html" in content_type:
        spec_url = extract_spec_url_from_html(content_text, url)
        if spec_url:
            try:
                spec_final_url, spec_text = await fetch_url_fn(spec_url)
                spec = try_parse_spec(spec_text)
                if spec:
                    return spec_final_url, spec
            except Exception as e:
                logger.warning(f"Failed to fetch detected spec URL {spec_url}: {e}")

    # Probe common spec paths
    spec_url, spec = await probe_common_spec_paths(url, user_agent=user_agent)
    if spec_url:
        return spec_url, spec

    return None, None


def classify_page_as_auth(url: str, title: str, content: str) -> bool:
    """Detect if a page is about authentication/authorization.

    Args:
        url: Page URL.
        title: Page title.
        content: Page text content.

    Returns:
        True if the page is about auth.
    """
    url_lower = url.lower()
    title_lower = title.lower()

    # Check URL path
    for keyword in _AUTH_PAGE_KEYWORDS:
        if keyword in url_lower:
            return True

    # Check title
    for keyword in _AUTH_PAGE_KEYWORDS:
        if keyword in title_lower:
            return True

    # Check content keyword density (at least 3 auth mentions in first 2000 chars)
    content_sample = content[:2000].lower()
    auth_mentions = sum(1 for kw in ["authentication", "authorization", "api key", "bearer token", "oauth", "credentials", "access token"] if kw in content_sample)
    return auth_mentions >= 3


def infer_base_urls(pages_content: List[Tuple[str, str]], doc_url: str) -> List[str]:
    """Extract API base URL candidates from crawled content.

    Checks for:
    - Explicit "Base URL:" declarations
    - curl example domains
    - Most frequent API domain in code examples
    - api.{root_domain} derivation

    Args:
        pages_content: List of (url, text_content) tuples from crawled pages.
        doc_url: Original documentation URL.

    Returns:
        Up to 5 base URL candidates sorted by score.
    """
    candidates: Dict[str, int] = {}

    all_content = "\n".join(content for _, content in pages_content)

    # Pattern 1: Explicit base URL declarations
    base_url_patterns = [
        r"(?:base\s*url|api\s*(?:base|root)\s*url|endpoint)\s*[:=]\s*(https?://[^\s,\"'`]+)",
        r"(?:Base URL|BASE_URL|baseUrl|baseURL)\s*[:=]\s*[\"']?(https?://[^\s,\"'`]+)",
    ]
    for pattern in base_url_patterns:
        for match in re.finditer(pattern, all_content, re.IGNORECASE):
            matched_url = match.group(1).rstrip("/")
            candidates[matched_url] = candidates.get(matched_url, 0) + 10  # high weight

    # Pattern 2: curl example domains
    curl_pattern = r"curl\s+(?:-[^\s]+\s+)*[\"']?(https?://[^\s\"']+)"
    for match in re.finditer(curl_pattern, all_content):
        full_url = match.group(1)
        parsed = urlparse(full_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if parsed.netloc != urlparse(doc_url).netloc:  # different from doc domain
            candidates[base] = candidates.get(base, 0) + 5

    # Pattern 3: Code example URLs (any https:// in content that differs from doc domain)
    code_url_pattern = r"(https?://(?:api|rest|gateway|service)\.[^\s\"'`\])<>]+)"
    for match in re.finditer(code_url_pattern, all_content, re.IGNORECASE):
        parsed = urlparse(match.group(1))
        base = f"{parsed.scheme}://{parsed.netloc}"
        candidates[base] = candidates.get(base, 0) + 3

    # Pattern 4: Derive api.{root_domain} from doc URL
    parsed_doc = urlparse(doc_url)
    doc_domain = parsed_doc.netloc
    domain_parts = doc_domain.split(".")
    if len(domain_parts) >= 2:
        root_domain = ".".join(domain_parts[-2:])
        api_candidate = f"{parsed_doc.scheme}://api.{root_domain}"
        if api_candidate not in candidates:
            candidates[api_candidate] = 1

    # Sort by score descending
    sorted_candidates = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
    return [u for u, _ in sorted_candidates[:5]]
