# -*- coding: utf-8 -*-
"""LLM Post-Processor Service Implementation.

Copyright 2025
SPDX-License-Identifier: Apache-2.0

The critical "correctness gate" between raw extraction and final tool creation.
Validates, deduplicates, corrects, and enriches all extracted data using a
multi-step LLM pipeline before tools are created.
"""

# Standard
from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Optional

# First-Party
from mcpgateway.services.llm_service import get_llm_service, LLMAPIError, LLMConfigurationError, LLMService
from mcpgateway.services.logging_service import LoggingService

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)

# Maximum tools per LLM batch to stay within context limits
_BATCH_SIZE = 20

# Maximum content chars to include in prompts for context
_MAX_CONTEXT_CHARS = 15000


@dataclass
class PostProcessedResult:
    """Result of the LLM post-processing pipeline."""

    tools: List[Dict[str, Any]] = field(default_factory=list)
    auth_config: Dict[str, Any] = field(default_factory=dict)
    validated_base_url: str = ""
    removed_tools: List[Dict[str, Any]] = field(default_factory=list)
    merged_tools: List[Dict[str, Any]] = field(default_factory=list)
    confidence_scores: Dict[str, float] = field(default_factory=dict)
    processing_notes: str = ""


class LLMPostProcessorService:
    """Multi-step LLM pipeline for validating, correcting, and enriching extracted tools."""

    def __init__(self, llm_service: Optional[LLMService] = None):
        """Initialize the post-processor.

        Args:
            llm_service: Optional LLMService instance. Uses global singleton if not provided.
        """
        self.llm_service = llm_service or get_llm_service()

    def _is_available(self) -> bool:
        """Check if LLM service is configured and available."""
        return self.llm_service.is_configured()

    async def postprocess_extracted_tools(
        self,
        raw_tools: List[Dict[str, Any]],
        crawl_result: Optional[Any] = None,
        doc_structure: Optional[Dict[str, Any]] = None,
        base_url: str = "",
        skip_endpoint_validation: bool = False,
    ) -> PostProcessedResult:
        """Run the full post-processing pipeline on extracted tools.

        Args:
            raw_tools: Raw tool definitions from extraction (LLM or regex).
            crawl_result: Optional CrawlResult from the crawler.
            doc_structure: Optional parsed documentation structure.
            base_url: Current inferred base URL.
            skip_endpoint_validation: If True, skip Step 1 (for OpenAPI specs).

        Returns:
            PostProcessedResult with validated, enriched tools.
        """
        result = PostProcessedResult(
            tools=list(raw_tools),
            validated_base_url=base_url,
        )

        if not raw_tools:
            result.processing_notes = "No tools to post-process."
            return result

        if not self._is_available():
            result.processing_notes = "LLM not available - returning raw tools without post-processing."
            return result

        # Prepare doc context (truncated for prompt size)
        doc_context = self._prepare_doc_context(crawl_result, doc_structure)
        notes = []

        # Step 1: Validate & correct endpoints (skip for OpenAPI specs)
        if not skip_endpoint_validation:
            try:
                validated, removed = await self._step1_validate_endpoints(result.tools, doc_context)
                result.tools = validated
                result.removed_tools = removed
                notes.append(f"Step 1: Validated {len(validated)} endpoints, removed {len(removed)} false positives.")
            except Exception as e:
                logger.warning(f"Post-processor Step 1 failed: {e}")
                notes.append(f"Step 1 (validate) failed: {e}")

        # Step 2: Deduplicate & reconcile
        try:
            deduped, merged = await self._step2_deduplicate(result.tools)
            result.tools = deduped
            result.merged_tools = merged
            notes.append(f"Step 2: {len(deduped)} unique tools after merging {len(merged)} duplicates.")
        except Exception as e:
            logger.warning(f"Post-processor Step 2 failed: {e}")
            notes.append(f"Step 2 (dedup) failed: {e}")

        # Step 3: Validate & enrich auth
        try:
            auth_config = await self._step3_validate_auth(
                result.tools,
                doc_context,
                crawl_result,
            )
            result.auth_config = auth_config
            notes.append(f"Step 3: Auth config validated with {len(auth_config.get('endpoint_overrides', {}))} endpoint overrides.")
        except Exception as e:
            logger.warning(f"Post-processor Step 3 failed: {e}")
            notes.append(f"Step 3 (auth) failed: {e}")

        # Step 4: Validate base URL
        try:
            validated_url = await self._step4_validate_base_url(
                base_url,
                result.tools,
                doc_context,
                crawl_result,
            )
            if validated_url:
                result.validated_base_url = validated_url
                notes.append(f"Step 4: Base URL validated as {validated_url}.")
            else:
                notes.append("Step 4: Base URL unchanged.")
        except Exception as e:
            logger.warning(f"Post-processor Step 4 failed: {e}")
            notes.append(f"Step 4 (base URL) failed: {e}")

        # Step 5: Enrich tool metadata
        try:
            enriched, scores = await self._step5_enrich_metadata(result.tools)
            result.tools = enriched
            result.confidence_scores = scores
            notes.append(f"Step 5: Enriched {len(enriched)} tools with improved metadata.")
        except Exception as e:
            logger.warning(f"Post-processor Step 5 failed: {e}")
            notes.append(f"Step 5 (enrich) failed: {e}")

        result.processing_notes = " | ".join(notes)
        logger.info(f"Post-processing complete: {len(result.tools)} tools, notes: {result.processing_notes}")
        return result

    # ------------------------------------------------------------------
    # Helper: prepare doc context for prompts
    # ------------------------------------------------------------------

    def _prepare_doc_context(self, crawl_result: Optional[Any], doc_structure: Optional[Dict[str, Any]]) -> str:
        """Prepare a truncated documentation context string for LLM prompts."""
        context = ""

        if crawl_result and hasattr(crawl_result, "aggregated_content"):
            context = crawl_result.aggregated_content
        elif doc_structure:
            context = doc_structure.get("raw_content", "")

        if len(context) > _MAX_CONTEXT_CHARS:
            context = context[:_MAX_CONTEXT_CHARS] + "\n... [truncated]"

        return context

    def _tools_to_summary(self, tools: List[Dict[str, Any]]) -> str:
        """Convert tools list to a compact JSON summary for prompts."""
        summaries = []
        for t in tools:
            summaries.append(
                {
                    "name": t.get("name", ""),
                    "method": t.get("method", ""),
                    "path": t.get("path", ""),
                    "description": (t.get("description", "") or "")[:100],
                    "auth_required": t.get("auth_required", False),
                }
            )
        return json.dumps(summaries, indent=2)

    # ------------------------------------------------------------------
    # Step 1: Validate & Correct Endpoints
    # ------------------------------------------------------------------

    async def _step1_validate_endpoints(self, tools: List[Dict[str, Any]], doc_context: str) -> tuple:
        """Validate endpoints against documentation. Remove false positives, fix mismatches.

        Returns:
            Tuple of (validated_tools, removed_tools).
        """
        validated = []
        removed = []

        # Process in batches
        for batch_start in range(0, len(tools), _BATCH_SIZE):
            batch = tools[batch_start : batch_start + _BATCH_SIZE]
            batch_json = json.dumps(batch, indent=2, default=str)

            prompt = f"""You are validating API endpoint definitions extracted from documentation.

DOCUMENTATION CONTEXT:
---
{doc_context[:8000]}
---

EXTRACTED ENDPOINTS TO VALIDATE:
{batch_json}

TASK: For each endpoint, determine:
1. Is this a real API endpoint from the documentation? (not a CSS route, internal navigation link, or documentation page URL)
2. Is the HTTP method correct for the operation described?
3. Are the path and parameters accurate?

Return a JSON object:
{{
  "validated": [
    {{...endpoint with any corrections applied...}}
  ],
  "removed": [
    {{"name": "...", "path": "...", "reason": "why this is a false positive"}}
  ]
}}

RULES:
- Be conservative: keep endpoints when uncertain
- Flag ambiguities in the endpoint's description rather than removing
- Fix obvious method/path mismatches (e.g., GET for a create operation should be POST)
- Remove clear false positives (CSS paths, doc navigation URLs, image URLs)
"""
            try:
                result = await self.llm_service.chat_completion_json(
                    messages=[
                        {"role": "system", "content": "You are an expert API analyst. Return only valid JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                )
                validated.extend(result.get("validated", batch))
                removed.extend(result.get("removed", []))
            except (LLMAPIError, LLMConfigurationError) as e:
                logger.warning(f"Step 1 batch failed, keeping raw: {e}")
                validated.extend(batch)

        return validated, removed

    # ------------------------------------------------------------------
    # Step 2: Deduplicate & Reconcile
    # ------------------------------------------------------------------

    async def _step2_deduplicate(self, tools: List[Dict[str, Any]]) -> tuple:
        """Deduplicate tools across pages. Merge duplicate definitions.

        Returns:
            Tuple of (deduped_tools, merged_info).
        """
        if len(tools) <= 5:
            # Too few to need LLM dedup
            return tools, []

        tools_summary = self._tools_to_summary(tools)

        prompt = f"""You are deduplicating API endpoint definitions that may have been extracted from multiple documentation pages.

ALL EXTRACTED ENDPOINTS:
{tools_summary}

TASK:
1. Identify duplicate endpoints (same resource + operation, possibly with slightly different names or descriptions)
2. For duplicates, keep the richest definition (best description, most parameters)
3. Identify endpoint groups (CRUD operations on the same resource)

Return a JSON object:
{{
  "unique_indices": [0, 1, 3, ...],
  "merged": [
    {{"kept_index": 1, "removed_index": 5, "reason": "same endpoint /users GET, merged parameters"}}
  ]
}}

Where unique_indices are 0-based indices of endpoints to keep. Only include indices of non-duplicate endpoints.
"""
        try:
            result = await self.llm_service.chat_completion_json(
                messages=[
                    {"role": "system", "content": "You are an expert API analyst. Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            unique_indices = result.get("unique_indices", list(range(len(tools))))
            merged = result.get("merged", [])

            # Validate indices are within range
            valid_indices = [i for i in unique_indices if 0 <= i < len(tools)]
            if not valid_indices:
                return tools, []

            deduped = [tools[i] for i in valid_indices]
            return deduped, merged

        except (LLMAPIError, LLMConfigurationError) as e:
            logger.warning(f"Step 2 dedup failed, keeping all: {e}")
            return tools, []

    # ------------------------------------------------------------------
    # Step 3: Validate & Enrich Auth Configuration
    # ------------------------------------------------------------------

    async def _step3_validate_auth(
        self,
        tools: List[Dict[str, Any]],
        doc_context: str,
        crawl_result: Optional[Any],
    ) -> Dict[str, Any]:
        """Validate and enrich authentication configuration.

        Returns:
            Dict with global_auth, endpoint_overrides, auth_details.
        """
        # Gather auth page content if available
        auth_content = ""
        if crawl_result and hasattr(crawl_result, "auth_pages"):
            for page in crawl_result.auth_pages[:3]:
                auth_content += f"\n--- Auth Page: {page.url} ---\n{page.content[:3000]}\n"

        tools_summary = self._tools_to_summary(tools)

        prompt = f"""You are analyzing API authentication requirements from documentation.

DOCUMENTATION CONTEXT (excerpts):
{doc_context[:5000]}

AUTHENTICATION-SPECIFIC PAGES:
{auth_content[:5000] if auth_content else "(no dedicated auth pages found)"}

EXTRACTED ENDPOINTS:
{tools_summary}

TASK: Determine the authentication configuration:
1. What is the global auth scheme for this API? (bearer, api_key, basic, oauth2, none)
2. Are there endpoints that differ from the global auth? (e.g., /health is public, /admin requires extra scopes)
3. What are the exact header names, token URLs, or scopes?

Return a JSON object:
{{
  "global_auth": {{
    "type": "bearer|api_key|basic|oauth2|none",
    "header_name": "Authorization",
    "header_value_prefix": "Bearer ",
    "details": "any additional details"
  }},
  "endpoint_overrides": {{
    "/health": {{"auth_required": false}},
    "/admin/*": {{"extra_scopes": ["admin"]}}
  }},
  "auth_details": {{
    "token_url": "",
    "authorization_url": "",
    "scopes": [],
    "api_key_header": "",
    "api_key_placement": "header|query"
  }}
}}

Only include fields you can determine from the documentation. Leave empty strings for unknown values.
"""
        try:
            result = await self.llm_service.chat_completion_json(
                messages=[
                    {"role": "system", "content": "You are an expert API security analyst. Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            return result
        except (LLMAPIError, LLMConfigurationError) as e:
            logger.warning(f"Step 3 auth validation failed: {e}")
            return {}

    # ------------------------------------------------------------------
    # Step 4: Validate Base URL
    # ------------------------------------------------------------------

    async def _step4_validate_base_url(
        self,
        current_base_url: str,
        tools: List[Dict[str, Any]],
        doc_context: str,
        crawl_result: Optional[Any],
    ) -> Optional[str]:
        """Validate and correct the base URL using LLM analysis.

        Returns:
            Validated base URL string, or None if unchanged.
        """
        candidates = []
        if crawl_result and hasattr(crawl_result, "base_url_candidates"):
            candidates = crawl_result.base_url_candidates

        sample_paths = [t.get("path", "") for t in tools[:10] if t.get("path")]

        prompt = f"""You are determining the correct API base URL from documentation.

CURRENT BASE URL: {current_base_url}
CANDIDATE BASE URLs: {json.dumps(candidates)}
SAMPLE ENDPOINT PATHS: {json.dumps(sample_paths)}

DOCUMENTATION EXCERPTS (look for curl examples, base URL declarations):
{doc_context[:5000]}

TASK: Determine the correct API base URL.
- The base URL is the scheme + host (+ optional path prefix) that, when combined with endpoint paths, produces valid API URLs.
- Often the documentation domain differs from the API domain (e.g., docs.stripe.com vs api.stripe.com).

Return a JSON object:
{{
  "base_url": "https://api.example.com/v1",
  "confidence": 0.9,
  "reasoning": "Found in curl examples throughout the documentation"
}}
"""
        try:
            result = await self.llm_service.chat_completion_json(
                messages=[
                    {"role": "system", "content": "You are an expert API analyst. Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            validated_url = result.get("base_url", "")
            confidence = result.get("confidence", 0)
            if validated_url and confidence >= 0.5:
                return validated_url.rstrip("/")
            return None
        except (LLMAPIError, LLMConfigurationError) as e:
            logger.warning(f"Step 4 base URL validation failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Step 5: Enrich Tool Metadata
    # ------------------------------------------------------------------

    async def _step5_enrich_metadata(self, tools: List[Dict[str, Any]]) -> tuple:
        """Enrich tool names, descriptions, tags, and hints.

        Returns:
            Tuple of (enriched_tools, confidence_scores).
        """
        enriched = []
        confidence_scores = {}

        for batch_start in range(0, len(tools), _BATCH_SIZE):
            batch = tools[batch_start : batch_start + _BATCH_SIZE]
            batch_json = json.dumps(batch, indent=2, default=str)

            prompt = f"""You are enriching API tool definitions with better metadata.

TOOL DEFINITIONS:
{batch_json}

TASK: For each tool, improve:
1. "name": Make it descriptive camelCase (e.g., "listAllUsers" not just "getUsers", "getUserById" not "getUser")
2. "description": Enhance to 2-3 clear sentences explaining what it does and when to use it
3. "tags": Add relevant resource/category tags (e.g., ["users", "identity"] for user endpoints)
4. "destructiveHint": true for DELETE, POST that creates side effects, PUT that overwrites
5. "idempotentHint": true for GET, PUT, DELETE; false for POST
6. "confidence": 0.0-1.0 score for how confident you are this is a real, correct endpoint

Return a JSON object:
{{
  "tools": [
    {{...enriched tool definition with all original fields plus improvements...}}
  ]
}}

RULES:
- Keep ALL original fields intact, only add/improve the ones listed above
- Don't remove or rename fields
- Be conservative with confidence scores
"""
            try:
                result = await self.llm_service.chat_completion_json(
                    messages=[
                        {"role": "system", "content": "You are an expert API analyst. Return only valid JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )
                enriched_batch = result.get("tools", batch)
                for tool in enriched_batch:
                    name = tool.get("name", "")
                    conf = tool.pop("confidence", 0.7)
                    if name:
                        confidence_scores[name] = conf
                enriched.extend(enriched_batch)
            except (LLMAPIError, LLMConfigurationError) as e:
                logger.warning(f"Step 5 enrichment batch failed: {e}")
                enriched.extend(batch)

        return enriched, confidence_scores
