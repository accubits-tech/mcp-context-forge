# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/tool_generation_job_service.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tool Generation Background Job Service.
Manages async background jobs for OpenAPI/API-doc tool generation.
"""

# Standard
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import uuid

# Third-Party
import yaml

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import SessionLocal, ToolGenerationJob, utc_now
from mcpgateway.schemas import AuthenticationValues, ToolCreate
from mcpgateway.utils.metadata_capture import MetadataCapture
from mcpgateway.utils.services_auth import encode_auth

logger = logging.getLogger(__name__)


class _MockRequest:
    """Minimal request substitute for MetadataCapture in background tasks."""

    headers: dict = {}
    client = type("Client", (), {"host": "background_job"})()


class ToolGenerationJobService:
    """Service for managing tool generation background jobs."""

    def __init__(self):
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._tasks: Dict[str, asyncio.Task] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """Initialize the service at app startup."""
        self._semaphore = asyncio.Semaphore(settings.tool_gen_max_concurrent_jobs)
        self._mark_stale_jobs_failed()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(f"ToolGenerationJobService initialized (max_concurrent={settings.tool_gen_max_concurrent_jobs})")

    def _mark_stale_jobs_failed(self):
        """Mark any running/pending jobs from a previous server lifecycle as failed."""
        try:
            with SessionLocal() as session:
                stale = session.query(ToolGenerationJob).filter(ToolGenerationJob.status.in_(["running", "pending"])).all()
                for job in stale:
                    job.status = "failed"
                    job.error = "Server restarted while job was in progress"
                    job.completed_at = utc_now()
                if stale:
                    session.commit()
                    logger.info(f"Marked {len(stale)} stale jobs as failed on startup")
        except Exception as e:
            logger.warning(f"Could not check for stale jobs (table may not exist yet): {e}")

    async def _cleanup_loop(self):
        """Periodically clean up old completed/failed/cancelled jobs."""
        while True:
            await asyncio.sleep(3600)
            try:
                cutoff = utc_now() - timedelta(hours=settings.tool_gen_job_ttl_hours)
                with SessionLocal() as session:
                    deleted = (
                        session.query(ToolGenerationJob)
                        .filter(
                            ToolGenerationJob.status.in_(["completed", "failed", "cancelled"]),
                            ToolGenerationJob.completed_at < cutoff,
                        )
                        .delete(synchronize_session=False)
                    )
                    session.commit()
                    if deleted:
                        logger.info(f"Cleaned up {deleted} old job records")
            except Exception as e:
                logger.error(f"Job cleanup error: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit_job(
        self,
        job_type: str,
        params: Dict[str, Any],
        user: str,
        auth_override: Optional[AuthenticationValues],
        file_content: Optional[bytes] = None,
        filename: Optional[str] = None,
    ) -> str:
        """Create a job record and dispatch the background runner."""
        job_id = uuid.uuid4().hex

        with SessionLocal() as session:
            job = ToolGenerationJob(
                id=job_id,
                job_type=job_type,
                status="pending",
                progress=0,
                progress_message="Queued",
                params=params,
                created_by=user if isinstance(user, str) else MetadataCapture.extract_username(user),
                created_at=utc_now(),
            )
            session.add(job)
            session.commit()

        # Select the runner
        runners = {
            "openapi_upload": self._run_openapi_upload,
            "openapi_url": self._run_openapi_url,
            "apidoc_upload": self._run_apidoc_upload,
            "apidoc_url": self._run_apidoc_url,
        }
        runner = runners[job_type]

        # Dispatch
        if file_content is not None:
            task = asyncio.create_task(runner(job_id, params, auth_override, user, file_content, filename))
        else:
            task = asyncio.create_task(runner(job_id, params, auth_override, user))

        task.add_done_callback(lambda fut: self._on_task_done(job_id, fut))
        self._tasks[job_id] = task

        logger.info(f"Submitted job {job_id} (type={job_type})")
        return job_id

    async def get_job(self, job_id: str, user: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get job status. Optionally validates user ownership."""
        with SessionLocal() as session:
            job = session.get(ToolGenerationJob, job_id)
            if not job:
                return None
            if user is not None:
                user_str = user if isinstance(user, str) else MetadataCapture.extract_username(user)
                if user_str != "anonymous" and job.created_by != user_str:
                    return None
            return self._job_to_dict(job)

    async def list_jobs(self, user: str, limit: int = 20, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
        """List jobs for a user, newest first."""
        user_str = user if isinstance(user, str) else MetadataCapture.extract_username(user)
        with SessionLocal() as session:
            query = session.query(ToolGenerationJob)
            if user_str != "anonymous":
                query = query.filter(ToolGenerationJob.created_by == user_str)
            total = query.count()
            jobs = query.order_by(ToolGenerationJob.created_at.desc()).offset(offset).limit(limit).all()
            return [self._job_to_dict(j) for j in jobs], total

    async def cancel_job(self, job_id: str, user: str) -> bool:
        """Request cancellation of a job."""
        with SessionLocal() as session:
            job = session.get(ToolGenerationJob, job_id)
            if not job:
                return False
            user_str = user if isinstance(user, str) else MetadataCapture.extract_username(user)
            if user_str != "anonymous" and job.created_by != user_str:
                return False
            if job.status in ("completed", "failed", "cancelled"):
                return False
            job.status = "cancelled"
            job.completed_at = utc_now()
            session.commit()
        logger.info(f"Job {job_id} cancelled by {user_str}")
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _job_to_dict(job: ToolGenerationJob) -> Dict[str, Any]:
        return {
            "job_id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "progress": job.progress,
            "progress_message": job.progress_message,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "result": job.result,
            "error": job.error,
        }

    def _on_task_done(self, job_id: str, future: asyncio.Task) -> None:
        """Safety net for unhandled exceptions in runners."""
        self._tasks.pop(job_id, None)
        exc = future.exception()
        if exc is not None:
            logger.error(f"Job {job_id} unhandled exception: {exc}")
            try:
                with SessionLocal() as session:
                    job = session.get(ToolGenerationJob, job_id)
                    if job and job.status == "running":
                        job.status = "failed"
                        job.error = f"Unhandled error: {type(exc).__name__}: {str(exc)}"
                        job.completed_at = utc_now()
                        session.commit()
            except Exception as db_err:
                logger.error(f"Failed to mark job {job_id} as failed: {db_err}")

    def _update_progress(self, session, job_id: str, progress: int, message: str):
        job = session.get(ToolGenerationJob, job_id)
        if job:
            job.progress = progress
            job.progress_message = message
            session.commit()

    def _is_cancelled(self, session, job_id: str) -> bool:
        session.expire_all()
        job = session.get(ToolGenerationJob, job_id)
        return job is not None and job.status == "cancelled"

    def _build_auth_summary(self, created_tools: List[Dict], auth_overridden: bool) -> Dict[str, Any]:
        auth_types_seen = set()
        tools_requiring_auth = 0
        tools_configured = 0
        for t in created_tools:
            if t.get("auth_required"):
                tools_requiring_auth += 1
                if t.get("auth_type"):
                    auth_types_seen.add(t["auth_type"])
                if t.get("auth_configured"):
                    tools_configured += 1
        return {
            "tools_requiring_auth": tools_requiring_auth,
            "tools_configured": tools_configured,
            "auth_types": list(auth_types_seen),
            "auth_overridden": auth_overridden,
        }

    async def _register_tools_in_db(
        self,
        session,
        tools: List,
        user: str,
        import_batch_id: str,
        created_via: str,
        auth_override: Optional[AuthenticationValues],
        job_id: str,
    ) -> Tuple[List[Dict], List[Dict]]:
        """Register tools in the database, checking for cancellation periodically."""
        # First-Party
        from mcpgateway.services.tool_service import ToolService  # pylint: disable=import-outside-toplevel

        tool_svc = ToolService()
        mock_request = _MockRequest()
        metadata = MetadataCapture.extract_creation_metadata(mock_request, user)
        metadata["created_via"] = created_via

        auth_overridden = auth_override is not None
        created_tools = []
        failed_tools = []

        for i, tool in enumerate(tools):
            # Check cancellation every 5 tools
            if i > 0 and i % 5 == 0 and self._is_cancelled(session, job_id):
                logger.info(f"Job {job_id} cancelled during tool registration at {i}/{len(tools)}")
                break

            try:
                created_tool = await tool_svc.register_tool(
                    db=session,
                    tool=tool,
                    created_by=metadata["created_by"],
                    created_from_ip=metadata.get("created_from_ip"),
                    created_via=metadata["created_via"],
                    created_user_agent=metadata.get("created_user_agent"),
                    import_batch_id=import_batch_id,
                    federation_source=metadata.get("federation_source"),
                )
                tool_data = created_tool.model_dump(by_alias=True)
                auth_required = tool.auth is not None and getattr(tool.auth, "auth_type", None) is not None
                tool_data["auth_required"] = auth_required
                tool_data["auth_type"] = tool.auth.auth_type if tool.auth else None
                tool_data["auth_configured"] = auth_overridden
                created_tools.append(tool_data)
            except Exception as e:
                logger.error(f"Failed to create tool {tool.name}: {e}")
                failed_tools.append({"name": tool.name, "error": str(e)})

        return created_tools, failed_tools

    # ------------------------------------------------------------------
    # Runners
    # ------------------------------------------------------------------

    async def _run_openapi_upload(
        self,
        job_id: str,
        params: Dict[str, Any],
        auth_override: Optional[AuthenticationValues],
        user: str,
        file_content: bytes = b"",
        filename: Optional[str] = None,
    ):
        """Background runner for openapi/upload jobs."""
        # First-Party
        from mcpgateway.services.openapi_service import OpenAPIService  # pylint: disable=import-outside-toplevel
        from mcpgateway.agents.openapi_agent import OpenAPIAgent  # pylint: disable=import-outside-toplevel

        openapi_svc = OpenAPIService()
        openapi_agt = OpenAPIAgent()

        async with self._semaphore:
            with SessionLocal() as session:
                try:
                    # Mark running
                    job = session.get(ToolGenerationJob, job_id)
                    job.status = "running"
                    job.started_at = utc_now()
                    session.commit()

                    import_batch_id = str(uuid.uuid4())
                    enhance_with_ai = params.get("enhance_with_ai", True)
                    base_url = params.get("base_url")
                    gateway_id = params.get("gateway_id")
                    additional_tags = [t.strip() for t in (params.get("tags") or "").split(",") if t.strip()]

                    # Step 1: Parse spec
                    self._update_progress(session, job_id, 10, "Parsing specification...")
                    content_type = "json"
                    if filename and (filename.endswith(".yaml") or filename.endswith(".yml")):
                        content_type = "yaml"
                    spec = await openapi_svc.parse_openapi_spec(file_content.decode("utf-8"), content_type)

                    if self._is_cancelled(session, job_id):
                        return

                    # Step 2: Generate tools
                    self._update_progress(session, job_id, 30, "Generating tools from spec...")
                    tools = await openapi_svc.generate_tools_from_spec(spec, base_url=base_url, gateway_id=gateway_id, tags=additional_tags)
                    tools = self._deduplicate_tools(tools)

                    # Apply auth override
                    if auth_override:
                        for tool in tools:
                            tool.auth = auth_override

                    if self._is_cancelled(session, job_id):
                        return

                    # Step 3: AI enhancement
                    if enhance_with_ai and tools:
                        self._update_progress(session, job_id, 50, "Enhancing with AI...")
                        try:
                            analysis = await openapi_agt.generate_comprehensive_analysis(spec)
                            tools_dict = [
                                {
                                    "name": tool.name,
                                    "description": tool.description,
                                    "url": str(tool.url),
                                    "method": tool.request_type,
                                    "path": tool.annotations.get("openapi_path", ""),
                                    "operation_id": tool.annotations.get("openapi_operation_id", ""),
                                    "tags": tool.tags,
                                    "annotations": tool.annotations,
                                    "requires_auth": tool.auth is not None,
                                    "auth_type": tool.auth.auth_type if tool.auth else None,
                                    "input_schema": tool.input_schema,
                                }
                                for tool in tools
                            ]
                            enhanced = await openapi_agt.enhance_tool_descriptions(tools_dict, analysis)
                            for i, enh in enumerate(enhanced):
                                if i < len(tools):
                                    if enh.get("name"):
                                        tools[i].name = enh["name"]
                                    if enh.get("description"):
                                        tools[i].description = enh["description"]
                                    if enh.get("tags"):
                                        tools[i].tags = enh["tags"]
                                    if enh.get("annotations"):
                                        tools[i].annotations.update(enh["annotations"])
                        except Exception as ai_err:
                            logger.warning(f"AI enhancement failed for job {job_id}: {ai_err}")

                    if self._is_cancelled(session, job_id):
                        return

                    # Step 4: Register tools
                    self._update_progress(session, job_id, 75, "Registering tools...")
                    created_tools, failed_tools = await self._register_tools_in_db(
                        session, tools, user, import_batch_id, "openapi_upload", auth_override, job_id,
                    )

                    if self._is_cancelled(session, job_id):
                        # Store partial results
                        job = session.get(ToolGenerationJob, job_id)
                        if job:
                            job.result = {"created_tools": created_tools, "tools_created": len(created_tools), "partial": True}
                            session.commit()
                        return

                    # Done
                    result = {
                        "status": "success",
                        "message": f"Processed OpenAPI specification from {filename}",
                        "import_batch_id": import_batch_id,
                        "api_info": {
                            "title": spec.get("info", {}).get("title", "Unknown API"),
                            "version": spec.get("info", {}).get("version", "unknown"),
                            "openapi_version": spec.get("openapi", "unknown"),
                        },
                        "tools_created": len(created_tools),
                        "tools_failed": len(failed_tools),
                        "created_tools": created_tools,
                        "failed_tools": failed_tools,
                        "ai_enhanced": enhance_with_ai and len(tools) > 0,
                        "auth_summary": self._build_auth_summary(created_tools, auth_override is not None),
                    }

                    job = session.get(ToolGenerationJob, job_id)
                    job.status = "completed"
                    job.progress = 100
                    job.progress_message = "Done"
                    job.result = result
                    job.completed_at = utc_now()
                    session.commit()

                except Exception as e:
                    logger.error(f"Job {job_id} (openapi_upload) failed: {e}")
                    job = session.get(ToolGenerationJob, job_id)
                    if job and job.status not in ("cancelled",):
                        job.status = "failed"
                        job.error = str(e)
                        job.completed_at = utc_now()
                        session.commit()

    async def _run_openapi_url(
        self,
        job_id: str,
        params: Dict[str, Any],
        auth_override: Optional[AuthenticationValues],
        user: str,
    ):
        """Background runner for openapi/url jobs."""
        # First-Party
        from mcpgateway.services.openapi_service import OpenAPIService  # pylint: disable=import-outside-toplevel
        from mcpgateway.agents.openapi_agent import OpenAPIAgent  # pylint: disable=import-outside-toplevel
        from mcpgateway.services.doc_crawler_service import DocumentationCrawlerService  # pylint: disable=import-outside-toplevel
        from mcpgateway.utils.url_validation import validate_url_not_internal  # pylint: disable=import-outside-toplevel

        openapi_svc = OpenAPIService()
        openapi_agt = OpenAPIAgent()

        async with self._semaphore:
            with SessionLocal() as session:
                try:
                    job = session.get(ToolGenerationJob, job_id)
                    job.status = "running"
                    job.started_at = utc_now()
                    session.commit()

                    url = params["url"]
                    import_batch_id = str(uuid.uuid4())
                    enhance_with_ai = params.get("enhance_with_ai", True)
                    base_url = params.get("base_url")
                    gateway_id = params.get("gateway_id")
                    additional_tags = [t.strip() for t in (params.get("tags") or "").split(",") if t.strip()]

                    # Step 1: Fetch URL
                    self._update_progress(session, job_id, 5, "Fetching specification...")
                    validate_url_not_internal(url)
                    crawler = DocumentationCrawlerService()
                    final_url, response = await crawler._fetch_with_redirects(url)

                    max_content_size = 20 * 1024 * 1024
                    content = response.text
                    if len(content.encode("utf-8")) > max_content_size:
                        raise ValueError(f"Response exceeds maximum size of {max_content_size} bytes")

                    # Detect content type
                    content_type = "json"
                    response_ct = response.headers.get("content-type", "").lower()

                    if "html" in response_ct:
                        spec_url = crawler._extract_spec_url_from_html(content, final_url)
                        if spec_url:
                            validate_url_not_internal(spec_url)
                            spec_final_url, spec_response = await crawler._fetch_with_redirects(spec_url)
                            content = spec_response.text
                            response_ct = spec_response.headers.get("content-type", "").lower()
                            final_url = spec_final_url
                        else:
                            raise ValueError("URL returned HTML. Use the API Docs URL tab for HTML documentation.")

                    if "yaml" in response_ct or "x-yaml" in response_ct or final_url.endswith((".yaml", ".yml")):
                        content_type = "yaml"
                    else:
                        try:
                            json.loads(content)
                        except (json.JSONDecodeError, ValueError):
                            try:
                                yaml.safe_load(content)
                                content_type = "yaml"
                            except Exception:
                                pass

                    # Step 2: Parse spec
                    self._update_progress(session, job_id, 15, "Parsing specification...")
                    spec = await openapi_svc.parse_openapi_spec(content, content_type)

                    if self._is_cancelled(session, job_id):
                        return

                    # Step 3: Generate tools
                    self._update_progress(session, job_id, 30, "Generating tools from spec...")
                    tools = await openapi_svc.generate_tools_from_spec(spec, base_url=base_url, gateway_id=gateway_id, tags=additional_tags)

                    if auth_override:
                        for tool in tools:
                            tool.auth = auth_override

                    if self._is_cancelled(session, job_id):
                        return

                    # Step 4: AI enhancement
                    if enhance_with_ai and tools:
                        self._update_progress(session, job_id, 50, "Enhancing with AI...")
                        try:
                            analysis = await openapi_agt.generate_comprehensive_analysis(spec, {"source_url": url})
                            tools_dict = [
                                {
                                    "name": tool.name,
                                    "description": tool.description,
                                    "url": str(tool.url),
                                    "method": tool.request_type,
                                    "path": tool.annotations.get("openapi_path", ""),
                                    "operation_id": tool.annotations.get("openapi_operation_id", ""),
                                    "tags": tool.tags,
                                    "annotations": tool.annotations,
                                    "requires_auth": tool.auth is not None,
                                    "auth_type": tool.auth.auth_type if tool.auth else None,
                                    "input_schema": tool.input_schema,
                                }
                                for tool in tools
                            ]
                            enhanced = await openapi_agt.enhance_tool_descriptions(tools_dict, analysis)
                            for i, enh in enumerate(enhanced):
                                if i < len(tools):
                                    if enh.get("name"):
                                        tools[i].name = enh["name"]
                                    if enh.get("description"):
                                        tools[i].description = enh["description"]
                                    if enh.get("tags"):
                                        tools[i].tags = enh["tags"]
                                    if enh.get("annotations"):
                                        tools[i].annotations.update(enh["annotations"])
                        except Exception as ai_err:
                            logger.warning(f"AI enhancement failed for job {job_id}: {ai_err}")

                    if self._is_cancelled(session, job_id):
                        return

                    # Step 5: Register tools
                    self._update_progress(session, job_id, 75, "Registering tools...")
                    created_tools, failed_tools = await self._register_tools_in_db(
                        session, tools, user, import_batch_id, "openapi_url", auth_override, job_id,
                    )

                    if self._is_cancelled(session, job_id):
                        job = session.get(ToolGenerationJob, job_id)
                        if job:
                            job.result = {"created_tools": created_tools, "tools_created": len(created_tools), "partial": True}
                            session.commit()
                        return

                    result = {
                        "status": "success",
                        "message": f"Processed OpenAPI specification from {url}",
                        "import_batch_id": import_batch_id,
                        "source_url": url,
                        "api_info": {
                            "title": spec.get("info", {}).get("title", "Unknown API"),
                            "version": spec.get("info", {}).get("version", "unknown"),
                            "openapi_version": spec.get("openapi", "unknown"),
                        },
                        "tools_created": len(created_tools),
                        "tools_failed": len(failed_tools),
                        "created_tools": created_tools,
                        "failed_tools": failed_tools,
                        "ai_enhanced": enhance_with_ai and len(tools) > 0,
                        "auth_summary": self._build_auth_summary(created_tools, auth_override is not None),
                    }

                    job = session.get(ToolGenerationJob, job_id)
                    job.status = "completed"
                    job.progress = 100
                    job.progress_message = "Done"
                    job.result = result
                    job.completed_at = utc_now()
                    session.commit()

                except Exception as e:
                    logger.error(f"Job {job_id} (openapi_url) failed: {e}")
                    job = session.get(ToolGenerationJob, job_id)
                    if job and job.status not in ("cancelled",):
                        job.status = "failed"
                        job.error = str(e)
                        job.completed_at = utc_now()
                        session.commit()

    async def _run_apidoc_upload(
        self,
        job_id: str,
        params: Dict[str, Any],
        auth_override: Optional[AuthenticationValues],
        user: str,
        file_content: bytes = b"",
        filename: Optional[str] = None,
    ):
        """Background runner for api-docs/upload jobs."""
        # First-Party
        from mcpgateway.agents.openapi_agent import OpenAPIAgent  # pylint: disable=import-outside-toplevel
        from mcpgateway.services.api_doc_parser_service import APIDocumentationParserService  # pylint: disable=import-outside-toplevel
        from mcpgateway.services.llm_postprocessor_service import LLMPostProcessorService  # pylint: disable=import-outside-toplevel

        openapi_agt = OpenAPIAgent()
        api_doc_svc = APIDocumentationParserService()

        async with self._semaphore:
            with SessionLocal() as session:
                try:
                    job = session.get(ToolGenerationJob, job_id)
                    job.status = "running"
                    job.started_at = utc_now()
                    session.commit()

                    import_batch_id = str(uuid.uuid4())
                    enhance_with_ai = params.get("enhance_with_ai", True)
                    base_url = params.get("base_url")
                    gateway_id = params.get("gateway_id")
                    format_hint = params.get("format_hint", "auto")
                    additional_tags = [t.strip() for t in (params.get("tags") or "").split(",") if t.strip()]

                    # Step 1: Parse documentation
                    self._update_progress(session, job_id, 10, "Parsing documentation...")
                    doc_structure = await api_doc_svc.parse_documentation_file(file_content, filename or "unknown", format_hint, base_url)

                    if not base_url:
                        base_url = "http://api.example.com"

                    if self._is_cancelled(session, job_id):
                        return

                    tools = []
                    analysis = {}
                    postprocessed = None

                    # Step 2: Extract tools
                    if enhance_with_ai:
                        self._update_progress(session, job_id, 30, "Extracting tools with AI...")
                        try:
                            raw_content = doc_structure.get("raw_content", "")
                            if not raw_content:
                                raise ValueError("No raw content extracted from documentation")

                            source_info = {"filename": filename, "format": doc_structure.get("source_format", "unknown")}
                            llm_tool_defs = await openapi_agt.extract_tools_from_raw_content(raw_content=raw_content, base_url=base_url, source_info=source_info)

                            self._update_progress(session, job_id, 50, "Post-processing tools...")
                            postprocessor = LLMPostProcessorService()
                            postprocessed = await postprocessor.postprocess_extracted_tools(raw_tools=llm_tool_defs, doc_structure=doc_structure, base_url=base_url)

                            base_url = postprocessed.validated_base_url or base_url
                            tools = self._llm_tool_defs_to_tool_creates(postprocessed.tools, gateway_id, additional_tags, doc_structure, auth_override=postprocessed.auth_config)

                            analysis = {
                                "extraction_method": "llm_direct",
                                "tools_extracted": len(llm_tool_defs),
                                "tools_after_postprocessing": len(postprocessed.tools),
                                "tools_created": len(tools),
                                "removed_tools": postprocessed.removed_tools,
                                "merged_tools": postprocessed.merged_tools,
                                "postprocessing_notes": postprocessed.processing_notes,
                                "source_format": doc_structure.get("source_format", "unknown"),
                                "content_length": doc_structure.get("content_length", 0),
                            }

                        except Exception as e:
                            logger.warning(f"LLM extraction failed for job {job_id}: {e}, falling back to regex")
                            tools = await api_doc_svc.generate_tools_from_documentation(doc_structure, base_url, gateway_id=gateway_id, tags=additional_tags)
                            analysis = {"error": f"LLM extraction failed: {str(e)}", "fallback": "regex_extraction"}
                    else:
                        self._update_progress(session, job_id, 30, "Extracting tools with regex...")
                        tools = await api_doc_svc.generate_tools_from_documentation(doc_structure, base_url, gateway_id=gateway_id, tags=additional_tags)
                        analysis = {"extraction_method": "regex", "ai_enabled": False}

                    # Deduplicate tools
                    tools = self._deduplicate_tools(tools)

                    # Apply auth override
                    if auth_override:
                        for tool in tools:
                            tool.auth = auth_override

                    if self._is_cancelled(session, job_id):
                        return

                    # Step 3: Register tools
                    self._update_progress(session, job_id, 75, "Registering tools...")
                    result = await self._finalize_api_doc_tools(
                        session, tools, analysis, user, import_batch_id,
                        filename or "upload", doc_structure, enhance_with_ai,
                        postprocessed, auth_override is not None, job_id,
                    )

                    if self._is_cancelled(session, job_id):
                        return

                    job = session.get(ToolGenerationJob, job_id)
                    job.status = "completed"
                    job.progress = 100
                    job.progress_message = "Done"
                    job.result = result
                    job.completed_at = utc_now()
                    session.commit()

                except Exception as e:
                    logger.error(f"Job {job_id} (apidoc_upload) failed: {e}")
                    job = session.get(ToolGenerationJob, job_id)
                    if job and job.status not in ("cancelled",):
                        job.status = "failed"
                        job.error = str(e)
                        job.completed_at = utc_now()
                        session.commit()

    async def _run_apidoc_url(
        self,
        job_id: str,
        params: Dict[str, Any],
        auth_override: Optional[AuthenticationValues],
        user: str,
    ):
        """Background runner for api-docs/url jobs."""
        # First-Party
        from mcpgateway.agents.openapi_agent import OpenAPIAgent  # pylint: disable=import-outside-toplevel
        from mcpgateway.services.api_doc_parser_service import APIDocumentationParserService  # pylint: disable=import-outside-toplevel
        from mcpgateway.services.llm_postprocessor_service import LLMPostProcessorService  # pylint: disable=import-outside-toplevel
        from mcpgateway.services.openapi_service import OpenAPIService  # pylint: disable=import-outside-toplevel

        openapi_agt = OpenAPIAgent()
        openapi_svc = OpenAPIService()
        api_doc_svc = APIDocumentationParserService()

        async with self._semaphore:
            with SessionLocal() as session:
                try:
                    job = session.get(ToolGenerationJob, job_id)
                    job.status = "running"
                    job.started_at = utc_now()
                    session.commit()

                    url = params["url"]
                    import_batch_id = str(uuid.uuid4())
                    enhance_with_ai = params.get("enhance_with_ai", True)
                    enable_crawling = params.get("enable_crawling", True)
                    base_url = params.get("base_url")
                    gateway_id = params.get("gateway_id")
                    format_hint = params.get("format_hint", "auto")
                    additional_tags = [t.strip() for t in (params.get("tags") or "").split(",") if t.strip()]

                    # Step 1: Parse documentation URL (with possible crawling)
                    self._update_progress(session, job_id, 5, "Fetching documentation...")
                    doc_structure = await api_doc_svc.parse_documentation_url(url, format_hint, base_url, enable_crawling=enable_crawling)

                    crawl_result = doc_structure.get("crawl_result")
                    crawl_stats = doc_structure.get("crawl_stats", {})

                    # Save crawled data locally for evaluation
                    if crawl_result:
                        try:
                            crawl_dir = Path(__file__).resolve().parent.parent.parent / "crawled-data" / job_id
                            crawl_dir.mkdir(parents=True, exist_ok=True)
                            # Save each page as a separate markdown file
                            for i, page in enumerate(crawl_result.pages):
                                page_file = crawl_dir / f"page_{i:03d}.md"
                                header = f"# {page.title or 'Untitled'}\n\n**URL:** {page.url}\n**Auth Page:** {page.is_auth_page}\n\n---\n\n"
                                page_file.write_text(header + page.content, encoding="utf-8")
                            # Save aggregated content
                            (crawl_dir / "aggregated.md").write_text(crawl_result.aggregated_content, encoding="utf-8")
                            # Save crawl stats and metadata
                            meta = {
                                "job_id": job_id,
                                "source_url": url,
                                "crawler": crawl_result.crawl_stats.get("crawler", "builtin"),
                                "crawl_stats": crawl_result.crawl_stats,
                                "discovered_openapi_spec_url": crawl_result.discovered_openapi_spec_url,
                                "base_url_candidates": crawl_result.base_url_candidates,
                                "auth_pages": [p.url for p in crawl_result.auth_pages],
                                "pages": [{"url": p.url, "title": p.title, "is_auth_page": p.is_auth_page, "content_length": len(p.content)} for p in crawl_result.pages],
                            }
                            (crawl_dir / "metadata.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
                            logger.info(f"Saved crawl data for job {job_id} to {crawl_dir}")
                        except Exception as e:
                            logger.warning(f"Failed to save crawl data for job {job_id}: {e}")

                    if self._is_cancelled(session, job_id):
                        return

                    # Check for auto-detected OpenAPI spec
                    discovered_spec = doc_structure.get("discovered_openapi_spec")
                    if discovered_spec:
                        self._update_progress(session, job_id, 20, "Processing discovered OpenAPI spec...")
                        try:
                            spec_tools = await openapi_svc.generate_tools_from_spec(discovered_spec, base_url=base_url, gateway_id=gateway_id, tags=additional_tags)
                            if spec_tools:
                                postprocessor = LLMPostProcessorService()
                                raw_tool_dicts = [
                                    {"name": t.name, "method": t.request_type, "path": "", "url": str(t.url), "description": t.description, "auth_required": t.auth is not None, "tags": t.tags or []}
                                    for t in spec_tools
                                ]
                                postprocessed = await postprocessor.postprocess_extracted_tools(
                                    raw_tools=raw_tool_dicts, crawl_result=crawl_result, doc_structure=doc_structure, base_url=base_url or "", skip_endpoint_validation=True,
                                )
                                analysis = {
                                    "extraction_method": "openapi_auto_detected",
                                    "spec_url": doc_structure.get("discovered_openapi_spec_url"),
                                    "tools_from_spec": len(spec_tools),
                                    "crawl_stats": crawl_stats,
                                    "postprocessing_notes": postprocessed.processing_notes,
                                }
                                tools = self._deduplicate_tools(spec_tools)

                                if auth_override:
                                    for tool in tools:
                                        tool.auth = auth_override

                                self._update_progress(session, job_id, 75, "Registering tools...")
                                result = await self._finalize_api_doc_tools(
                                    session, tools, analysis, user, import_batch_id,
                                    url, doc_structure, enhance_with_ai, postprocessed,
                                    auth_override is not None, job_id,
                                )

                                job = session.get(ToolGenerationJob, job_id)
                                job.status = "completed"
                                job.progress = 100
                                job.progress_message = "Done"
                                job.result = result
                                job.completed_at = utc_now()
                                session.commit()
                                return
                        except Exception as e:
                            logger.warning(f"OpenAPI spec processing failed ({e}), falling back to doc extraction")

                    # Set base_url
                    if not base_url:
                        base_url_candidates = doc_structure.get("base_url_candidates", [])
                        if base_url_candidates:
                            base_url = base_url_candidates[0]
                        else:
                            from urllib.parse import urlparse  # pylint: disable=import-outside-toplevel
                            parsed_url = urlparse(url)
                            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

                    tools = []
                    analysis = {}
                    postprocessed = None
                    auth_context = doc_structure.get("authentication_info")

                    # Step 2: Extract tools
                    if enhance_with_ai:
                        self._update_progress(session, job_id, 30, "Extracting tools with AI...")
                        try:
                            source_info = {"url": url, "format": doc_structure.get("source_format", "unknown")}

                            # Prefer per-page extraction when crawl pages are available
                            if crawl_result and crawl_result.pages:
                                llm_tool_defs = await openapi_agt.extract_tools_from_pages(
                                    pages=crawl_result.pages,
                                    base_url=base_url,
                                    source_info=source_info,
                                    auth_context=auth_context,
                                )
                                extraction_method = "llm_per_page"
                            else:
                                # Fallback to chunked extraction (no crawl result)
                                raw_content = doc_structure.get("raw_content", "")
                                if not raw_content:
                                    raise ValueError("No raw content extracted from documentation")
                                llm_tool_defs = await openapi_agt.extract_tools_from_raw_content(
                                    raw_content=raw_content,
                                    base_url=base_url,
                                    source_info=source_info,
                                    auth_context=auth_context,
                                )
                                extraction_method = "llm_direct"

                            self._update_progress(session, job_id, 50, "Post-processing tools...")
                            postprocessor = LLMPostProcessorService()
                            postprocessed = await postprocessor.postprocess_extracted_tools(raw_tools=llm_tool_defs, crawl_result=crawl_result, doc_structure=doc_structure, base_url=base_url)

                            validated_base_url = postprocessed.validated_base_url or base_url
                            tools = self._llm_tool_defs_to_tool_creates(postprocessed.tools, gateway_id, additional_tags, doc_structure, auth_override=postprocessed.auth_config)

                            analysis = {
                                "extraction_method": extraction_method,
                                "pages_total": len(crawl_result.pages) if crawl_result and crawl_result.pages else 0,
                                "tools_extracted": len(llm_tool_defs),
                                "tools_after_postprocessing": len(postprocessed.tools),
                                "tools_created": len(tools),
                                "removed_tools": postprocessed.removed_tools,
                                "merged_tools": postprocessed.merged_tools,
                                "confidence_scores": postprocessed.confidence_scores,
                                "postprocessing_notes": postprocessed.processing_notes,
                                "validated_base_url": validated_base_url,
                                "source_url": url,
                                "source_format": doc_structure.get("source_format", "unknown"),
                                "content_length": doc_structure.get("content_length", 0),
                                "crawl_stats": crawl_stats,
                            }

                        except Exception as e:
                            logger.warning(f"LLM extraction failed for job {job_id}: {e}, falling back to regex")
                            tools = await api_doc_svc.generate_tools_from_documentation(doc_structure, base_url, gateway_id=gateway_id, tags=additional_tags)
                            analysis = {"error": f"LLM extraction failed: {str(e)}", "fallback": "regex_extraction", "crawl_stats": crawl_stats}
                    else:
                        self._update_progress(session, job_id, 30, "Extracting tools with regex...")
                        if crawl_result and crawl_result.pages:
                            tools = await api_doc_svc.generate_tools_from_pages(
                                pages=crawl_result.pages,
                                base_url=base_url,
                                source_url=url,
                                gateway_id=gateway_id,
                                tags=additional_tags,
                            )
                            analysis = {"extraction_method": "regex_per_page", "ai_enabled": False, "crawl_stats": crawl_stats}
                        else:
                            tools = await api_doc_svc.generate_tools_from_documentation(doc_structure, base_url, gateway_id=gateway_id, tags=additional_tags)
                            analysis = {"extraction_method": "regex", "ai_enabled": False, "crawl_stats": crawl_stats}

                    # Deduplicate tools
                    tools = self._deduplicate_tools(tools)

                    # Apply auth override
                    if auth_override:
                        for tool in tools:
                            tool.auth = auth_override

                    if self._is_cancelled(session, job_id):
                        return

                    # Step 3: Register tools
                    self._update_progress(session, job_id, 75, "Registering tools...")
                    result = await self._finalize_api_doc_tools(
                        session, tools, analysis, user, import_batch_id,
                        url, doc_structure, enhance_with_ai, postprocessed,
                        auth_override is not None, job_id,
                    )

                    if self._is_cancelled(session, job_id):
                        return

                    job = session.get(ToolGenerationJob, job_id)
                    job.status = "completed"
                    job.progress = 100
                    job.progress_message = "Done"
                    job.result = result
                    job.completed_at = utc_now()
                    session.commit()

                except Exception as e:
                    logger.error(f"Job {job_id} (apidoc_url) failed: {e}")
                    job = session.get(ToolGenerationJob, job_id)
                    if job and job.status not in ("cancelled",):
                        job.status = "failed"
                        job.error = str(e)
                        job.completed_at = utc_now()
                        session.commit()

    # ------------------------------------------------------------------
    # Shared helpers for api-doc runners (mirrors main.py helpers)
    # ------------------------------------------------------------------

    @staticmethod
    def _llm_tool_defs_to_tool_creates(
        llm_tool_defs: List[Dict[str, Any]],
        gateway_id: Optional[str],
        additional_tags: List[str],
        doc_structure: Optional[Dict[str, Any]] = None,
        auth_override: Optional[Dict[str, Any]] = None,
    ) -> List:
        """Convert LLM-extracted tool definitions to ToolCreate objects. Mirrors main.py._llm_tool_defs_to_tool_creates."""
        # Import here to avoid circular imports at module level
        from mcpgateway.main import _llm_tool_defs_to_tool_creates  # pylint: disable=import-outside-toplevel

        return _llm_tool_defs_to_tool_creates(llm_tool_defs, gateway_id, additional_tags, doc_structure, auth_override)

    @staticmethod
    def _deduplicate_tools(tools: List) -> List:
        """Remove duplicate tools based on (HTTP method, URL path).

        When duplicates exist, keeps the tool with the longest description
        (most detail). Returns deduplicated list.
        """
        seen: Dict[Tuple[str, str], Any] = {}
        for tool in tools:
            method = getattr(tool, "request_type", "") or ""
            url = str(getattr(tool, "url", "")) or ""
            key = (method.upper(), url)
            existing = seen.get(key)
            if existing is None or len(tool.description or "") > len(existing.description or ""):
                seen[key] = tool
        deduped = list(seen.values())
        if len(deduped) < len(tools):
            logger.info(f"Deduplicated {len(tools)} tools to {len(deduped)}")
        return deduped

    async def _finalize_api_doc_tools(
        self,
        session,
        tools: List,
        analysis: Dict[str, Any],
        user: str,
        import_batch_id: str,
        url: str,
        doc_structure: Dict[str, Any],
        enhance_with_ai: bool,
        postprocessed=None,
        auth_overridden: bool = False,
        job_id: str = "",
    ) -> Dict[str, Any]:
        """Register api-doc tools and build result dict. Mirrors _finalize_api_doc_tools from main.py but uses a provided session."""
        if not tools:
            return {"success": False, "message": "No API endpoints could be extracted from the documentation", "analysis": analysis, "tool_count": 0}

        # First-Party
        from mcpgateway.services.tool_service import ToolService  # pylint: disable=import-outside-toplevel

        tool_svc = ToolService()
        mock_request = _MockRequest()
        metadata = MetadataCapture.extract_creation_metadata(mock_request, user)

        created_tools = []
        errors = []

        for i, tool in enumerate(tools):
            if i > 0 and i % 5 == 0 and self._is_cancelled(session, job_id):
                break

            try:
                creation_metadata = {"created_via": "api_doc_url", "source_url": url, "source_format": doc_structure.get("source_format", "unknown"), "ai_enhanced": enhance_with_ai}
                if tool.annotations is None:
                    tool.annotations = {}
                tool.annotations.update(creation_metadata)

                db_tool = await tool_svc.register_tool(
                    db=session,
                    tool=tool,
                    created_by=metadata["created_by"],
                    created_via="api_doc_url",
                    import_batch_id=import_batch_id,
                )
                auth_required = tool.auth is not None and getattr(tool.auth, "auth_type", None) is not None
                created_tools.append({
                    "id": db_tool.id,
                    "name": db_tool.name,
                    "url": db_tool.url,
                    "description": db_tool.description,
                    "tags": db_tool.tags or [],
                    "auth_required": auth_required,
                    "auth_type": tool.auth.auth_type if tool.auth else None,
                    "auth_configured": auth_overridden,
                })
            except Exception as e:
                logger.error(f"Failed to create tool {tool.name}: {e}")
                errors.append(f"Tool '{tool.name}': {str(e)}")

        result = {
            "success": len(created_tools) > 0,
            "message": f"Created {len(created_tools)} tools from API documentation",
            "import_batch_id": import_batch_id,
            "tool_count": len(created_tools),
            "created_tools": created_tools,
            "analysis": analysis,
            "auth_summary": self._build_auth_summary(created_tools, auth_overridden),
        }
        if errors:
            result["errors"] = errors
        if postprocessed:
            result["removed_tools"] = postprocessed.removed_tools
            result["merged_tools"] = postprocessed.merged_tools
            result["confidence_scores"] = postprocessed.confidence_scores
            result["postprocessing_notes"] = postprocessed.processing_notes
        if doc_structure.get("base_url_candidates"):
            result["base_url_candidates"] = doc_structure["base_url_candidates"]

        return result
