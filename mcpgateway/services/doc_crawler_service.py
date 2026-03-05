# -*- coding: utf-8 -*-
"""Documentation Crawler Service Implementation.

Copyright 2025
SPDX-License-Identifier: Apache-2.0

Multi-page documentation crawler with BFS traversal, OpenAPI spec auto-detection,
safe redirect following (SSRF-checked per hop), auth page classification,
base URL inference, robots.txt respect, and politeness controls.
"""

# Standard
import asyncio
from dataclasses import dataclass, field
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

# Third-Party
from bs4 import BeautifulSoup
import httpx

# First-Party
from mcpgateway.config import settings
from mcpgateway.services.doc_post_processing import (
    classify_page_as_auth,
    detect_openapi_spec,
    infer_base_urls,
)
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


def _validate_url_not_internal(target_url: str) -> None:
    """Validate that a URL does not point to internal/private network addresses."""
    # First-Party
    from mcpgateway.utils.url_validation import validate_url_not_internal  # pylint: disable=import-outside-toplevel

    validate_url_not_internal(target_url)


@dataclass
class CrawledPage:
    """Holds data for a single crawled page."""

    url: str
    content: str  # cleaned text content
    raw_html: str
    content_type: str
    is_auth_page: bool = False
    links: List[str] = field(default_factory=list)
    title: str = ""


@dataclass
class CrawlResult:
    """Aggregated result from a multi-page documentation crawl."""

    pages: List[CrawledPage] = field(default_factory=list)
    aggregated_content: str = ""
    discovered_openapi_spec_url: Optional[str] = None
    discovered_openapi_spec: Optional[Dict[str, Any]] = None
    auth_pages: List[CrawledPage] = field(default_factory=list)
    base_url_candidates: List[str] = field(default_factory=list)
    crawl_stats: Dict[str, Any] = field(default_factory=dict)


# URL path segments that indicate non-documentation pages
_NON_DOC_SEGMENTS = frozenset(
    [
        "login",
        "signup",
        "register",
        "pricing",
        "blog",
        "careers",
        "about",
        "contact",
        "terms",
        "privacy",
        "legal",
        "status",
        "community",
        "forum",
        "changelog",
        "release-notes",
    ]
)

# File extensions to skip when crawling links
_SKIP_EXTENSIONS = frozenset(
    [
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".css",
        ".js",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".zip",
        ".tar",
        ".gz",
        ".pdf",
        ".mp4",
        ".mp3",
    ]
)


class DocumentationCrawlerService:
    """Multi-page documentation crawler with OpenAPI auto-detection."""

    def __init__(self):
        """Initialize the crawler service with configuration from settings."""
        self.max_pages: int = getattr(settings, "doc_crawler_max_pages", 30)
        self.max_depth: int = getattr(settings, "doc_crawler_max_depth", 3)
        self.delay: float = getattr(settings, "doc_crawler_delay", 0.5)
        self.max_concurrent: int = getattr(settings, "doc_crawler_max_concurrent", 5)
        self.max_redirects: int = getattr(settings, "doc_crawler_max_redirects", 5)
        self.enable_js_rendering: bool = getattr(settings, "doc_crawler_enable_js_rendering", False)
        self.respect_robots_txt: bool = getattr(settings, "doc_crawler_respect_robots_txt", True)
        self.max_content_per_page: int = 5 * 1024 * 1024  # 5MB
        self.max_total_content: int = 50 * 1024 * 1024  # 50MB
        self.request_timeout: int = 30
        self._user_agent = "MCP-Gateway Documentation Crawler 1.0"
        self._robots_cache: Dict[str, Optional[RobotFileParser]] = {}

    async def crawl_documentation(self, url: str, enable_crawling: bool = True) -> CrawlResult:
        """Primary entry point: fetch, detect spec, optionally crawl, return aggregated result.

        Args:
            url: Starting documentation URL.
            enable_crawling: If True, perform multi-page BFS crawl. If False, fetch single page only.

        Returns:
            CrawlResult with pages, aggregated content, discovered spec, etc.
        """
        result = CrawlResult()
        start_time = time.monotonic()

        try:
            # Step 1: Fetch the starting URL with safe redirect following
            final_url, response = await self._fetch_with_redirects(url)

            # Step 2: Check if the response IS an OpenAPI spec or links to one
            async def _fetch_spec_url(spec_url: str) -> Tuple[str, str]:
                """Fetch a spec URL and return (final_url, text)."""
                fu, resp = await self._fetch_with_redirects(spec_url)
                return fu, resp.text

            content_type = response.headers.get("content-type", "").lower()
            spec_url, spec_dict = await detect_openapi_spec(
                final_url, content_type, response.text, _fetch_spec_url, user_agent=self._user_agent
            )
            if spec_url:
                result.discovered_openapi_spec_url = spec_url
            if spec_dict:
                result.discovered_openapi_spec = spec_dict

            # Step 3: Crawl pages (BFS) if enabled, otherwise single page
            if enable_crawling:
                pages = await self._crawl_pages(final_url, response)
            else:
                page = self._response_to_page(final_url, response)
                pages = [page] if page else []

            # Step 4: Classify auth pages
            for page in pages:
                page.is_auth_page = classify_page_as_auth(page.url, page.title, page.content)
                if page.is_auth_page:
                    result.auth_pages.append(page)

            # Step 5: Infer base URLs
            pages_content = [(p.url, p.content) for p in pages]
            result.base_url_candidates = infer_base_urls(pages_content, url)

            # Step 6: Aggregate content
            result.pages = pages
            total_content = []
            total_size = 0
            for page in pages:
                if total_size + len(page.content) > self.max_total_content:
                    break
                total_content.append(f"--- Page: {page.url} ---\n{page.content}")
                total_size += len(page.content)
            result.aggregated_content = "\n\n".join(total_content)

            elapsed = time.monotonic() - start_time
            result.crawl_stats = {
                "pages_crawled": len(pages),
                "auth_pages_found": len(result.auth_pages),
                "openapi_spec_detected": result.discovered_openapi_spec_url is not None,
                "base_url_candidates": result.base_url_candidates,
                "elapsed_seconds": round(elapsed, 2),
                "total_content_chars": len(result.aggregated_content),
            }

        except Exception as e:
            logger.error(f"Documentation crawl failed for {url}: {e}")
            result.crawl_stats = {"error": str(e)}

        return result

    # ------------------------------------------------------------------
    # Fetching with safe redirect following
    # ------------------------------------------------------------------

    async def _fetch_with_redirects(self, url: str) -> Tuple[str, httpx.Response]:
        """Follow redirects with SSRF validation on each hop.

        Args:
            url: URL to fetch.

        Returns:
            Tuple of (final_url, response).

        Raises:
            ValueError: If URL is internal or too many redirects.
            httpx.HTTPError: On HTTP errors.
        """
        _validate_url_not_internal(url)

        current_url = url
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json,application/yaml,*/*",
        }

        async with httpx.AsyncClient(timeout=self.request_timeout, follow_redirects=False) as client:
            for hop in range(self.max_redirects + 1):
                response = await client.get(current_url, headers=headers)

                if response.is_redirect or response.has_redirect_location:
                    location = response.headers.get("location", "")
                    if not location:
                        raise ValueError(f"Redirect with no Location header from {current_url}")
                    next_url = urljoin(current_url, location)
                    _validate_url_not_internal(next_url)
                    logger.debug(f"Following redirect hop {hop + 1}: {current_url} -> {next_url}")
                    current_url = next_url
                    continue

                response.raise_for_status()
                return current_url, response

        raise ValueError(f"Too many redirects ({self.max_redirects}) starting from {url}")

    # ------------------------------------------------------------------
    # Multi-page BFS crawl
    # ------------------------------------------------------------------

    async def _crawl_pages(self, start_url: str, start_response: httpx.Response) -> List[CrawledPage]:
        """BFS crawl documentation pages within the same domain.

        Args:
            start_url: Starting URL (after redirects).
            start_response: HTTP response for the starting page.

        Returns:
            List of CrawledPage objects.
        """
        parsed_start = urlparse(start_url)
        allowed_domain = parsed_start.netloc

        # Check robots.txt
        if self.respect_robots_txt:
            allowed = await self._check_robots_txt(start_url)
            if not allowed:
                logger.warning(f"robots.txt disallows crawling {start_url}")
                page = self._response_to_page(start_url, start_response)
                return [page] if page else []

        # Initialize BFS
        visited: Set[str] = set()
        pages: List[CrawledPage] = []
        # Queue: (url, depth)
        queue: asyncio.Queue = asyncio.Queue()

        # Process start page
        start_page = self._response_to_page(start_url, start_response)
        if start_page:
            pages.append(start_page)
            visited.add(self._normalize_url(start_url))

            # Extract links from start page
            links = self._extract_documentation_links(start_page.raw_html, start_url, allowed_domain)
            start_page.links = links
            for link in links:
                norm = self._normalize_url(link)
                if norm not in visited:
                    await queue.put((link, 1))
                    visited.add(norm)

        # BFS with concurrency limit
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def fetch_page(page_url: str, depth: int) -> Optional[CrawledPage]:
            """Fetch a single page with rate limiting."""
            async with semaphore:
                if self.delay > 0:
                    await asyncio.sleep(self.delay)
                try:
                    _validate_url_not_internal(page_url)
                    final_url, resp = await self._fetch_with_redirects(page_url)
                    page = self._response_to_page(final_url, resp)
                    if page and depth < self.max_depth:
                        page.links = self._extract_documentation_links(page.raw_html, final_url, allowed_domain)
                    return page
                except Exception as e:
                    logger.debug(f"Failed to crawl {page_url}: {e}")
                    return None

        while not queue.empty() and len(pages) < self.max_pages:
            # Batch fetch up to max_concurrent pages
            batch = []
            while not queue.empty() and len(batch) < self.max_concurrent and len(pages) + len(batch) < self.max_pages:
                page_url, depth = await queue.get()
                batch.append((page_url, depth))

            if not batch:
                break

            tasks = [fetch_page(u, d) for u, d in batch]
            results = await asyncio.gather(*tasks)

            for (page_url, depth), page in zip(batch, results):
                if page is None:
                    continue

                # Check content size limit
                if len(page.content) > self.max_content_per_page:
                    page.content = page.content[: self.max_content_per_page]

                pages.append(page)

                # Enqueue new links if within depth
                if depth < self.max_depth:
                    for link in page.links:
                        norm = self._normalize_url(link)
                        if norm not in visited:
                            visited.add(norm)
                            await queue.put((link, depth + 1))

        logger.info(f"Crawled {len(pages)} pages from {start_url}")
        return pages

    # ------------------------------------------------------------------
    # Page conversion helpers
    # ------------------------------------------------------------------

    def _response_to_page(self, url: str, response: httpx.Response) -> Optional[CrawledPage]:
        """Convert an HTTP response to a CrawledPage."""
        content_type = response.headers.get("content-type", "").lower()
        raw_html = response.text

        if "html" in content_type or (not content_type and raw_html.strip().startswith("<")):
            soup = BeautifulSoup(raw_html, "html.parser")

            title = ""
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)

            # Remove non-content elements
            for el in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                el.decompose()

            # Try main content areas first
            text = ""
            for selector in ["main", "article", ".content", ".main-content", ".documentation", ".api-docs", "#content"]:
                elements = soup.select(selector)
                if elements:
                    text = "\n\n".join(el.get_text(separator="\n", strip=True) for el in elements)
                    if text.strip():
                        break

            if not text.strip():
                body = soup.find("body")
                text = body.get_text(separator="\n", strip=True) if body else soup.get_text(separator="\n", strip=True)

            return CrawledPage(url=url, content=text, raw_html=raw_html, content_type=content_type, title=title)

        # Non-HTML (plain text, etc.)
        return CrawledPage(url=url, content=raw_html, raw_html=raw_html, content_type=content_type, title="")

    def _normalize_url(self, url: str) -> str:
        """Normalize URL for dedup (strip fragment, trailing slash)."""
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    # ------------------------------------------------------------------
    # Link extraction
    # ------------------------------------------------------------------

    def _extract_documentation_links(self, html: str, base_url: str, allowed_domain: str) -> List[str]:
        """Extract documentation navigation links from HTML.

        Looks in nav, sidebar, TOC, and api-reference sections.
        Filters out static assets, non-doc pages, and external domains.
        """
        soup = BeautifulSoup(html, "html.parser")
        links: List[str] = []
        seen: Set[str] = set()

        # Priority selectors for documentation navigation
        selectors = [
            "nav a",
            "aside a",
            ".sidebar a",
            ".toc a",
            ".table-of-contents a",
            ".api-reference a",
            ".docs-nav a",
            ".doc-sidebar a",
            ".menu a",
            '[role="navigation"] a',
        ]

        candidate_elements = []
        for selector in selectors:
            candidate_elements.extend(soup.select(selector))

        # If no nav elements found, fall back to all links in body
        if not candidate_elements:
            body = soup.find("body")
            if body:
                candidate_elements = body.find_all("a", href=True)

        for a_tag in candidate_elements:
            href = a_tag.get("href", "")
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue

            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)

            # Domain scoping
            if parsed.netloc != allowed_domain:
                continue

            # Skip static assets
            path_lower = parsed.path.lower()
            if any(path_lower.endswith(ext) for ext in _SKIP_EXTENSIONS):
                continue

            # Skip non-doc pages
            path_segments = set(parsed.path.strip("/").split("/"))
            if path_segments & _NON_DOC_SEGMENTS:
                continue

            normalized = self._normalize_url(full_url)
            if normalized not in seen:
                seen.add(normalized)
                links.append(full_url)

        return links

    # ------------------------------------------------------------------
    # robots.txt
    # ------------------------------------------------------------------

    async def _check_robots_txt(self, url: str) -> bool:
        """Check if crawling the URL is allowed by robots.txt."""
        parsed = urlparse(url)
        robots_base = f"{parsed.scheme}://{parsed.netloc}"

        if robots_base in self._robots_cache:
            rp = self._robots_cache[robots_base]
            if rp is None:
                return True  # No robots.txt = allowed
            return rp.can_fetch(self._user_agent, url)

        robots_url = f"{robots_base}/robots.txt"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(robots_url)
                if resp.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(resp.text.splitlines())
                    self._robots_cache[robots_base] = rp
                    return rp.can_fetch(self._user_agent, url)
                self._robots_cache[robots_base] = None
                return True
        except Exception:
            self._robots_cache[robots_base] = None
            return True

    # ------------------------------------------------------------------
    # JS rendering integration (optional)
    # ------------------------------------------------------------------

    async def _maybe_render_js(self, url: str, raw_html: str) -> Optional[str]:
        """If JS rendering is enabled and needed, render the page with Playwright.

        Returns rendered HTML or None if not needed/available.
        """
        if not self.enable_js_rendering:
            return None

        try:
            # First-Party
            from mcpgateway.services.js_renderer_service import JSRendererService  # pylint: disable=import-outside-toplevel

            renderer = JSRendererService()
            if renderer.detect_js_rendering_needed(raw_html):
                logger.info(f"JS rendering needed for {url}")
                rendered = await renderer.render_page(url)
                return rendered
        except ImportError:
            logger.debug("JS renderer not available")
        except Exception as e:
            logger.warning(f"JS rendering failed for {url}: {e}")

        return None
