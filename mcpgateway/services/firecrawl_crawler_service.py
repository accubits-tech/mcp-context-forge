# -*- coding: utf-8 -*-
"""Firecrawl-based Documentation Crawler Service.

Copyright 2025
SPDX-License-Identifier: Apache-2.0

Uses the Firecrawl SDK for web crawling/scraping, with post-processing
for OpenAPI spec detection, auth page classification, and base URL inference.
"""

# Standard
import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

# Third-Party
import httpx

# First-Party
from mcpgateway.config import settings
from mcpgateway.services.doc_crawler_service import CrawledPage, CrawlResult, _NON_DOC_SEGMENTS
from mcpgateway.services.doc_post_processing import (
    classify_page_as_auth,
    detect_openapi_spec,
    infer_base_urls,
    try_parse_spec,
)
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


def _validate_url_not_internal(target_url: str) -> None:
    """Validate that a URL does not point to internal/private network addresses."""
    # First-Party
    from mcpgateway.utils.url_validation import validate_url_not_internal  # pylint: disable=import-outside-toplevel

    validate_url_not_internal(target_url)


class FirecrawlCrawlerService:
    """Documentation crawler using Firecrawl SDK with domain-specific post-processing."""

    def __init__(self) -> None:
        """Initialize the Firecrawl crawler service."""
        self.max_pages: int = getattr(settings, "doc_crawler_max_pages", 30)
        self.max_depth: int = getattr(settings, "doc_crawler_max_depth", 3)
        self.max_total_content: int = 50 * 1024 * 1024  # 50MB
        self._user_agent = "MCP-Gateway Documentation Crawler 1.0"
        self._firecrawl_api_url: str = getattr(settings, "firecrawl_api_url", "https://api.firecrawl.dev")
        self._firecrawl_api_key: str = getattr(settings, "firecrawl_api_key", "")

    def _get_client(self) -> Any:
        """Create an AsyncFirecrawl client instance.

        Returns:
            AsyncFirecrawl client configured for hosted or self-hosted Firecrawl.
        """
        from firecrawl import AsyncFirecrawl  # pylint: disable=import-outside-toplevel

        kwargs: Dict[str, Any] = {"api_url": self._firecrawl_api_url}
        if self._firecrawl_api_key:
            kwargs["api_key"] = self._firecrawl_api_key
        return AsyncFirecrawl(**kwargs)

    def _build_exclude_paths(self) -> List[str]:
        """Convert _NON_DOC_SEGMENTS to Firecrawl excludePaths regex patterns.

        Returns:
            List of regex patterns for Firecrawl's excludePaths parameter.
        """
        return [f".*/{segment}/.*" for segment in _NON_DOC_SEGMENTS]

    async def crawl_documentation(self, url: str, enable_crawling: bool = True) -> CrawlResult:
        """Primary entry point: crawl or scrape URL via Firecrawl, then post-process.

        Args:
            url: Starting documentation URL.
            enable_crawling: If True, multi-page crawl. If False, single-page scrape.

        Returns:
            CrawlResult with pages, aggregated content, discovered spec, etc.
        """
        result = CrawlResult()
        start_time = time.monotonic()

        try:
            # SSRF validation before sending to Firecrawl
            _validate_url_not_internal(url)

            if enable_crawling:
                pages = await self._crawl_pages(url)
            else:
                pages = await self._scrape_single_page(url)

            # Post-processing: OpenAPI spec detection
            spec_url, spec_dict = await self._detect_spec_from_pages(pages, url)
            if spec_url:
                result.discovered_openapi_spec_url = spec_url
            if spec_dict:
                result.discovered_openapi_spec = spec_dict

            # Post-processing: Auth page classification
            for page in pages:
                page.is_auth_page = classify_page_as_auth(page.url, page.title, page.content)
                if page.is_auth_page:
                    result.auth_pages.append(page)

            # Post-processing: Base URL inference
            pages_content = [(p.url, p.content) for p in pages]
            result.base_url_candidates = infer_base_urls(pages_content, url)

            # Aggregate content
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
                "crawler": "firecrawl",
            }

        except Exception as e:
            logger.error(f"Firecrawl documentation crawl failed for {url}: {e}")
            result.crawl_stats = {"error": str(e), "crawler": "firecrawl"}

        return result

    # ------------------------------------------------------------------
    # Firecrawl crawl (multi-page)
    # ------------------------------------------------------------------

    async def _crawl_pages(self, url: str) -> List[CrawledPage]:
        """Crawl multiple pages using Firecrawl's crawl endpoint.

        Args:
            url: Starting URL.

        Returns:
            List of CrawledPage objects.
        """
        client = self._get_client()

        crawl_params = {
            "url": url,
            "limit": self.max_pages,
            "maxDiscoveryDepth": self.max_depth,
            "excludePaths": self._build_exclude_paths(),
            "scrapeOptions": {
                "formats": ["markdown", "rawHtml", "links"],
                "onlyMainContent": True,
            },
        }

        logger.info(f"Starting Firecrawl crawl of {url} (limit={self.max_pages}, depth={self.max_depth})")

        # Use async crawl with polling
        crawl_result = await client.crawl_url(url, params=crawl_params, poll_interval=2)

        pages: List[CrawledPage] = []

        # crawl_result.data contains the list of crawled documents
        data_items = getattr(crawl_result, "data", None) or []
        for item in data_items:
            page = self._firecrawl_item_to_page(item)
            if page:
                pages.append(page)

        logger.info(f"Firecrawl crawled {len(pages)} pages from {url}")
        return pages

    # ------------------------------------------------------------------
    # Firecrawl scrape (single page)
    # ------------------------------------------------------------------

    async def _scrape_single_page(self, url: str) -> List[CrawledPage]:
        """Scrape a single page using Firecrawl's scrape endpoint.

        Args:
            url: URL to scrape.

        Returns:
            List with a single CrawledPage, or empty if failed.
        """
        client = self._get_client()

        scrape_params = {
            "formats": ["markdown", "rawHtml", "links"],
            "onlyMainContent": True,
        }

        logger.info(f"Firecrawl scraping single page: {url}")
        scrape_result = await client.scrape_url(url, params=scrape_params)

        page = self._firecrawl_item_to_page(scrape_result)
        return [page] if page else []

    # ------------------------------------------------------------------
    # Response mapping
    # ------------------------------------------------------------------

    def _firecrawl_item_to_page(self, item: Any) -> Optional[CrawledPage]:
        """Convert a Firecrawl response item to a CrawledPage.

        Args:
            item: Firecrawl scrape/crawl result item (dict or object).

        Returns:
            CrawledPage or None if item is empty.
        """
        if item is None:
            return None

        # Handle both dict and object access patterns
        if isinstance(item, dict):
            markdown = item.get("markdown", "") or ""
            raw_html = item.get("rawHtml", "") or ""
            links = item.get("links", []) or []
            metadata = item.get("metadata", {}) or {}
        else:
            markdown = getattr(item, "markdown", "") or ""
            raw_html = getattr(item, "rawHtml", "") or getattr(item, "raw_html", "") or ""
            links = getattr(item, "links", []) or []
            metadata = getattr(item, "metadata", {}) or {}

        if isinstance(metadata, dict):
            source_url = metadata.get("sourceURL", "") or metadata.get("url", "") or ""
            title = metadata.get("title", "") or ""
            content_type = metadata.get("content-type", "text/html") or "text/html"
        else:
            source_url = getattr(metadata, "sourceURL", "") or getattr(metadata, "url", "") or ""
            title = getattr(metadata, "title", "") or ""
            content_type = getattr(metadata, "content_type", "text/html") or "text/html"

        if not markdown and not raw_html:
            return None

        # Use markdown as the cleaned content (Firecrawl already does content extraction)
        content = markdown or raw_html

        return CrawledPage(
            url=source_url,
            content=content,
            raw_html=raw_html,
            content_type=content_type,
            links=links if isinstance(links, list) else [],
            title=title,
        )

    # ------------------------------------------------------------------
    # OpenAPI spec detection from crawled pages
    # ------------------------------------------------------------------

    async def _detect_spec_from_pages(self, pages: List[CrawledPage], original_url: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Run OpenAPI spec detection on crawled pages.

        Checks raw HTML of each page for spec references, and probes common paths.

        Args:
            pages: List of crawled pages.
            original_url: The original URL that was crawled.

        Returns:
            Tuple of (spec_url_or_None, parsed_spec_dict_or_None).
        """
        if not pages:
            return None, None

        # Check the first page (most likely to be the main doc page or spec itself)
        first_page = pages[0]

        async def _fetch_spec_url(spec_url: str) -> Tuple[str, str]:
            """Fetch a spec URL using httpx directly."""
            _validate_url_not_internal(spec_url)
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(spec_url, headers={"User-Agent": self._user_agent})
                resp.raise_for_status()
                return str(resp.url), resp.text

        spec_url, spec_dict = await detect_openapi_spec(
            first_page.url or original_url,
            first_page.content_type,
            first_page.raw_html or first_page.content,
            _fetch_spec_url,
            user_agent=self._user_agent,
        )
        if spec_url:
            return spec_url, spec_dict

        # Check remaining pages for inline specs
        for page in pages[1:]:
            if page.raw_html:
                spec = try_parse_spec(page.raw_html)
                if spec:
                    return page.url, spec

        return None, None
