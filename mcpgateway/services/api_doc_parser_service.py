# -*- coding: utf-8 -*-
"""API Documentation Parser Service Implementation.

Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

This module implements parsing and tool generation from various API documentation formats.
It handles:
- PDF API documentation parsing
- URL-based documentation parsing
- HTML/Markdown content extraction
- AI-enhanced endpoint detection
- Tool schema generation from documentation
"""

# Standard
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

# Third-Party
from bs4 import BeautifulSoup
from markdown import markdown
import requests

# First-Party
from mcpgateway.schemas import ToolCreate
from mcpgateway.services.logging_service import LoggingService

# Initialize logging service
logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


class APIDocumentationError(Exception):
    """Base class for API documentation parsing errors."""


class DocumentFormatError(APIDocumentationError):
    """Raised when document format is unsupported or invalid."""


class ContentExtractionError(APIDocumentationError):
    """Raised when content extraction fails."""


class APIDocumentationParserService:
    """Service for parsing various API documentation formats and generating tools."""

    def __init__(self):
        """Initialize the API documentation parser service."""
        self.supported_formats = ["pdf", "html", "markdown", "text", "auto"]
        self.max_file_size = 50 * 1024 * 1024  # 50MB
        self.request_timeout = 30  # 30 seconds
        self.max_content_length = 10 * 1024 * 1024  # 10MB for web content

    async def parse_documentation_file(self, file_content: bytes, filename: str, format_hint: str = "auto", base_url: Optional[str] = None) -> Dict[str, Any]:
        """Parse API documentation from uploaded file.

        Args:
            file_content: Raw file content
            filename: Original filename
            format_hint: Format hint ("auto", "pdf", "html", "markdown", "text")
            base_url: Base URL for the API

        Returns:
            Parsed documentation structure

        Raises:
            DocumentFormatError: If format is unsupported
            ContentExtractionError: If content extraction fails
        """
        if len(file_content) > self.max_file_size:
            raise DocumentFormatError(f"File size {len(file_content)} bytes exceeds maximum {self.max_file_size} bytes")

        # Auto-detect format if needed
        detected_format = self._detect_format(file_content, filename, format_hint)
        logger.info(f"Parsing documentation file '{filename}' as format '{detected_format}'")

        try:
            if detected_format == "pdf":
                return await self._parse_pdf_content(file_content)
            elif detected_format == "html":
                return await self._parse_html_content(file_content.decode("utf-8", errors="ignore"))
            elif detected_format == "markdown":
                return await self._parse_markdown_content(file_content.decode("utf-8", errors="ignore"))
            elif detected_format == "text":
                return await self._parse_text_content(file_content.decode("utf-8", errors="ignore"))
            else:
                raise DocumentFormatError(f"Unsupported format: {detected_format}")

        except Exception as e:
            raise ContentExtractionError(f"Failed to parse {detected_format} content: {str(e)}")

    async def parse_documentation_url(self, url: str, format_hint: str = "auto", base_url: Optional[str] = None) -> Dict[str, Any]:
        """Parse API documentation from URL.

        Args:
            url: URL to API documentation
            format_hint: Format hint ("auto", "html", "markdown", "text")
            base_url: Base URL for the API

        Returns:
            Parsed documentation structure

        Raises:
            ContentExtractionError: If URL fetching or parsing fails
        """
        try:
            logger.info(f"Fetching API documentation from URL: {url}")

            # Fetch content with timeout and size limits
            headers = {
                "User-Agent": "MCP-Gateway API Documentation Parser 1.0",
                "Accept": "text/html,application/xhtml+xml,text/markdown,text/plain,*/*",
                "Accept-Encoding": "gzip, deflate",  # Avoid brotli streaming decode issues
            }

            response = requests.get(url, headers=headers, timeout=self.request_timeout, stream=True)
            response.raise_for_status()

            # Check content length
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > self.max_content_length:
                raise ContentExtractionError(f"Content length {content_length} exceeds maximum {self.max_content_length} bytes")

            # Read content with size limit
            content = b""
            for chunk in response.iter_content(chunk_size=8192):
                content += chunk
                if len(content) > self.max_content_length:
                    raise ContentExtractionError(f"Content size exceeds maximum {self.max_content_length} bytes")

            # Detect format from content-type or URL
            content_type = response.headers.get("content-type", "").lower()
            detected_format = self._detect_url_format(url, content_type, format_hint)

            logger.info(f"Detected format '{detected_format}' for URL content")

            # Parse content based on format
            if detected_format == "html":
                return await self._parse_html_content(content.decode("utf-8", errors="ignore"), source_url=url)
            elif detected_format == "markdown":
                return await self._parse_markdown_content(content.decode("utf-8", errors="ignore"), source_url=url)
            elif detected_format == "text":
                return await self._parse_text_content(content.decode("utf-8", errors="ignore"), source_url=url)
            else:
                # Default to HTML parsing for web content
                return await self._parse_html_content(content.decode("utf-8", errors="ignore"), source_url=url)

        except requests.exceptions.RequestException as e:
            raise ContentExtractionError(f"Failed to fetch URL {url}: {str(e)}")
        except Exception as e:
            raise ContentExtractionError(f"Failed to parse documentation from URL: {str(e)}")

    def _detect_format(self, content: bytes, filename: str, format_hint: str) -> str:
        """Detect file format from content and filename.

        Args:
            content: Raw file content
            filename: Original filename
            format_hint: User-provided format hint

        Returns:
            Detected format string
        """
        if format_hint != "auto":
            return format_hint

        # Check file extension
        filename_lower = filename.lower()
        if filename_lower.endswith(".pdf"):
            return "pdf"
        elif filename_lower.endswith((".html", ".htm")):
            return "html"
        elif filename_lower.endswith((".md", ".markdown")):
            return "markdown"
        elif filename_lower.endswith(".txt"):
            return "text"

        # Check content magic bytes
        if content.startswith(b"%PDF"):
            return "pdf"
        elif content.startswith(b"<!DOCTYPE") or content.startswith(b"<html"):
            return "html"

        # Try to decode and check for markdown patterns
        try:
            text_content = content.decode("utf-8", errors="ignore")[:1000]  # Check first 1KB
            if re.search(r"^#{1,6}\s+\w+", text_content, re.MULTILINE):  # Markdown headers
                return "markdown"
            elif "<" in text_content and ">" in text_content:
                return "html"
        except:
            pass

        # Default to text
        return "text"

    def _detect_url_format(self, url: str, content_type: str, format_hint: str) -> str:
        """Detect format from URL and content-type.

        Args:
            url: Source URL
            content_type: HTTP Content-Type header
            format_hint: User-provided format hint

        Returns:
            Detected format string
        """
        if format_hint != "auto":
            return format_hint

        # Check content-type
        if "text/html" in content_type:
            return "html"
        elif "text/markdown" in content_type:
            return "markdown"
        elif "text/plain" in content_type:
            return "text"

        # Check URL path
        url_lower = url.lower()
        if url_lower.endswith((".html", ".htm")):
            return "html"
        elif url_lower.endswith((".md", ".markdown")):
            return "markdown"

        # Default to HTML for web content
        return "html"

    async def _parse_pdf_content(self, content: bytes) -> Dict[str, Any]:
        """Parse PDF content to extract API documentation.

        Args:
            content: PDF file content

        Returns:
            Parsed documentation structure
        """
        try:
            # Import PDF library here to avoid dependency issues if not installed
            # Standard
            from io import BytesIO

            # Third-Party
            import PyPDF2

            pdf_reader = PyPDF2.PdfReader(BytesIO(content))

            # Extract text from all pages
            text_content = ""
            for page in pdf_reader.pages:
                text_content += page.extract_text() + "\n\n"

            logger.info(f"Extracted {len(text_content)} characters from PDF")

            # Parse the extracted text
            return await self._parse_text_content(text_content)

        except ImportError:
            raise DocumentFormatError("PyPDF2 library not available for PDF parsing")
        except Exception as e:
            raise ContentExtractionError(f"PDF parsing failed: {str(e)}")

    async def _parse_html_content(self, content: str, source_url: Optional[str] = None) -> Dict[str, Any]:
        """Parse HTML content to extract API documentation.

        Args:
            content: HTML content
            source_url: Source URL if applicable

        Returns:
            Parsed documentation structure
        """
        try:
            soup = BeautifulSoup(content, "html.parser")

            # Remove script and style elements
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()

            # Extract main content areas
            main_content = ""

            # Try to find main content containers
            content_selectors = ["main", "article", ".content", ".main-content", ".documentation", ".api-docs", "#content", ".container"]

            content_found = False
            for selector in content_selectors:
                elements = soup.select(selector)
                if elements:
                    for element in elements:
                        main_content += element.get_text(separator="\n", strip=True) + "\n\n"
                    content_found = True
                    break

            # If no main content found, use body
            if not content_found:
                body = soup.find("body")
                if body:
                    main_content = body.get_text(separator="\n", strip=True)
                else:
                    main_content = soup.get_text(separator="\n", strip=True)

            logger.info(f"Extracted {len(main_content)} characters from HTML")

            # Parse the extracted text
            doc_structure = await self._parse_text_content(main_content, source_url=source_url)
            doc_structure["source_format"] = "html"
            doc_structure["source_url"] = source_url

            return doc_structure

        except Exception as e:
            raise ContentExtractionError(f"HTML parsing failed: {str(e)}")

    async def _parse_markdown_content(self, content: str, source_url: Optional[str] = None) -> Dict[str, Any]:
        """Parse Markdown content to extract API documentation.

        Args:
            content: Markdown content
            source_url: Source URL if applicable

        Returns:
            Parsed documentation structure
        """
        try:
            # Convert markdown to HTML for structured parsing
            html_content = markdown(content)

            # Also keep the raw markdown for text analysis
            logger.info(f"Processing {len(content)} characters of Markdown content")

            # Parse both HTML structure and raw text
            html_doc = await self._parse_html_content(html_content, source_url=source_url)
            text_doc = await self._parse_text_content(content, source_url=source_url)

            # Combine results, preferring HTML structure analysis
            doc_structure = html_doc
            doc_structure["source_format"] = "markdown"
            doc_structure["raw_content"] = content

            # Merge any additional endpoints found in text analysis
            if text_doc.get("potential_endpoints"):
                existing_endpoints = {ep.get("path", "") for ep in doc_structure.get("potential_endpoints", [])}
                for endpoint in text_doc["potential_endpoints"]:
                    if endpoint.get("path", "") not in existing_endpoints:
                        doc_structure["potential_endpoints"].append(endpoint)

            return doc_structure

        except Exception as e:
            raise ContentExtractionError(f"Markdown parsing failed: {str(e)}")

    async def _parse_text_content(self, content: str, source_url: Optional[str] = None) -> Dict[str, Any]:
        """Parse plain text content to extract API documentation.

        Args:
            content: Text content
            source_url: Source URL if applicable

        Returns:
            Parsed documentation structure
        """
        try:
            logger.info(f"Analyzing {len(content)} characters of text content")

            # Extract potential API endpoints using regex patterns
            endpoints = []

            # Pattern 1: HTTP method + path
            method_path_pattern = r"\b(GET|POST|PUT|DELETE|PATCH)\s+([/\w\-{}:]+)"
            for match in re.finditer(method_path_pattern, content, re.IGNORECASE):
                method, path = match.groups()
                endpoints.append(
                    {
                        "method": method.upper(),
                        "path": path,
                        "context_start": max(0, match.start() - 100),
                        "context_end": min(len(content), match.end() + 100),
                        "context": content[max(0, match.start() - 100) : min(len(content), match.end() + 100)],
                    }
                )

            # Pattern 2: URL patterns with base URLs
            url_pattern = r"https?://[^\s/]+(/[^\s]*)"
            for match in re.finditer(url_pattern, content):
                path = match.group(1)
                if path and len(path) > 1:  # Ignore root paths
                    endpoints.append(
                        {
                            "method": "GET",  # Default assumption
                            "path": path,
                            "context_start": max(0, match.start() - 100),
                            "context_end": min(len(content), match.end() + 100),
                            "context": content[max(0, match.start() - 100) : min(len(content), match.end() + 100)],
                            "full_url": match.group(0),
                        }
                    )

            # Pattern 3: Path-only patterns (starting with /)
            path_pattern = r"(?:^|\s)(/[/\w\-{}:]+)(?=\s|$)"
            for match in re.finditer(path_pattern, content, re.MULTILINE):
                path = match.group(1)
                if len(path) > 1 and not path.endswith(".") and "{" not in path or "}" in path:
                    endpoints.append(
                        {
                            "method": "GET",  # Default assumption
                            "path": path,
                            "context_start": max(0, match.start() - 100),
                            "context_end": min(len(content), match.end() + 100),
                            "context": content[max(0, match.start() - 100) : min(len(content), match.end() + 100)],
                        }
                    )

            # Remove duplicates based on method + path
            unique_endpoints = []
            seen = set()
            for endpoint in endpoints:
                key = (endpoint.get("method", "GET"), endpoint.get("path", ""))
                if key not in seen and key[1]:  # Ensure path is not empty
                    seen.add(key)
                    unique_endpoints.append(endpoint)

            # Extract API title and description
            title = self._extract_title(content)
            description = self._extract_description(content)

            # Extract authentication info
            auth_info = self._extract_auth_patterns(content)

            # Extract parameter patterns
            parameters = self._extract_parameter_patterns(content)

            doc_structure = {
                "title": title,
                "description": description,
                "source_format": "text",
                "source_url": source_url,
                "content_length": len(content),
                "potential_endpoints": unique_endpoints,
                "authentication_info": auth_info,
                "parameters": parameters,
                "raw_content": content[:50000] if len(content) > 50000 else content,  # First 50KB for AI analysis
            }

            logger.info(f"Extracted {len(unique_endpoints)} potential endpoints from text content")
            return doc_structure

        except Exception as e:
            raise ContentExtractionError(f"Text parsing failed: {str(e)}")

    def _extract_title(self, content: str) -> str:
        """Extract API title from content.

        Args:
            content: Text content

        Returns:
            Extracted title or default
        """
        # Look for title patterns
        title_patterns = [
            r"^#\s+(.+)$",  # Markdown H1
            r"^(.+)\s+API\s*$",  # Line ending with "API"
            r"^(.+)\s+Documentation\s*$",  # Line ending with "Documentation"
            r"<title>(.+)</title>",  # HTML title tag
        ]

        lines = content.split("\n")[:20]  # Check first 20 lines
        for line in lines:
            line = line.strip()
            for pattern in title_patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    title = match.group(1).strip()
                    if len(title) > 3 and len(title) < 100:
                        return title

        return "API Documentation"

    def _extract_description(self, content: str) -> str:
        """Extract API description from content.

        Args:
            content: Text content

        Returns:
            Extracted description
        """
        # Look for description patterns
        lines = content.split("\n")
        description_lines = []

        for i, line in enumerate(lines[:50]):  # Check first 50 lines
            line = line.strip()
            if not line:
                continue

            # Skip lines that look like endpoints
            if re.match(r"\b(GET|POST|PUT|DELETE|PATCH)\s+", line, re.IGNORECASE):
                continue

            # Skip URLs
            if line.startswith("http"):
                continue

            # Collect non-header lines that look like descriptions
            if not line.startswith("#") and len(line) > 20 and "." in line:
                description_lines.append(line)
                if len(description_lines) >= 3:  # Collect up to 3 lines
                    break

        if description_lines:
            return " ".join(description_lines)

        return "API documentation parsed from text content"

    def _extract_auth_patterns(self, content: str) -> Dict[str, Any]:
        """Extract authentication patterns from content.

        Args:
            content: Text content

        Returns:
            Authentication information
        """
        auth_info = {"methods": [], "details": {}}

        # Common authentication keywords
        auth_patterns = {
            "bearer": r"\b(bearer\s+token|authorization:\s*bearer|jwt\s+token)\b",
            "api_key": r"\b(api\s*key|x-api-key|apikey)\b",
            "basic": r"\b(basic\s+auth|http\s+basic|username.*password)\b",
            "oauth": r"\b(oauth|o-?auth\s*2\.0)\b",
        }

        content_lower = content.lower()
        for auth_type, pattern in auth_patterns.items():
            if re.search(pattern, content_lower):
                auth_info["methods"].append(auth_type)

        return auth_info

    def _extract_parameter_patterns(self, content: str) -> List[Dict[str, Any]]:
        """Extract parameter patterns from content.

        Args:
            content: Text content

        Returns:
            List of detected parameters
        """
        parameters = []

        # Pattern for path parameters
        path_param_pattern = r"\{(\w+)\}"
        for match in re.finditer(path_param_pattern, content):
            param_name = match.group(1)
            parameters.append({"name": param_name, "type": "path", "description": f"Path parameter: {param_name}"})

        # Pattern for query parameters (common documentation patterns)
        query_patterns = [
            r"\?(\w+)=",  # ?param=
            r"&(\w+)=",  # &param=
            r"query.*?(\w+).*?:",  # Query parameter: name:
        ]

        for pattern in query_patterns:
            for match in re.finditer(pattern, content):
                param_name = match.group(1)
                if param_name not in [p["name"] for p in parameters]:
                    parameters.append({"name": param_name, "type": "query", "description": f"Query parameter: {param_name}"})

        return parameters

    async def generate_tools_from_documentation(self, doc_structure: Dict[str, Any], base_url: str, gateway_id: Optional[str] = None, tags: Optional[List[str]] = None) -> List[ToolCreate]:
        """Generate MCP Gateway tools from parsed documentation.

        Args:
            doc_structure: Parsed documentation structure
            base_url: Base URL for the API
            gateway_id: Gateway ID to associate tools with
            tags: Additional tags for generated tools

        Returns:
            List of ToolCreate objects
        """
        tools = []
        endpoints = doc_structure.get("potential_endpoints", [])

        logger.info(f"Generating tools from {len(endpoints)} detected endpoints")

        for endpoint in endpoints:
            try:
                tool = await self._create_tool_from_endpoint(endpoint=endpoint, doc_structure=doc_structure, base_url=base_url, gateway_id=gateway_id, additional_tags=tags or [])

                if tool:
                    tools.append(tool)

            except Exception as e:
                logger.warning(f"Failed to create tool from endpoint {endpoint.get('path', 'unknown')}: {str(e)}")
                continue

        logger.info(f"Generated {len(tools)} tools from documentation")
        return tools

    async def generate_tools_from_llm_analysis(self, extracted_endpoints: List[Dict[str, Any]], base_url: str, gateway_id: Optional[str] = None, tags: Optional[List[str]] = None) -> List[ToolCreate]:
        """Generate MCP Gateway tools from LLM-extracted endpoints.

        Args:
            extracted_endpoints: List of endpoints from LLM analysis
            base_url: Base URL for the API
            gateway_id: Gateway ID to associate tools with
            tags: Additional tags for generated tools

        Returns:
            List of ToolCreate objects
        """
        tools = []
        logger.info(f"Generating tools from {len(extracted_endpoints)} LLM-extracted endpoints")

        for idx, ep in enumerate(extracted_endpoints):
            try:
                # Get method with fallback
                method = (ep.get("method") or "GET").upper()
                if method not in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
                    logger.warning(f"Invalid HTTP method '{method}' for endpoint {idx}, defaulting to GET")
                    method = "GET"

                # Get path - skip if completely missing
                path = ep.get("path", "") or ep.get("url", "") or ep.get("endpoint", "")
                if not path:
                    logger.warning(f"Skipping endpoint {idx}: no path/url/endpoint field found")
                    continue

                # Clean path
                path = path.strip()
                if not path.startswith("/") and not path.startswith("http"):
                    path = "/" + path

                # Generate tool name with validation
                tool_name = self._generate_tool_name(method, path)

                # Ensure tool name is valid (at least 3 chars, alphanumeric with underscores)
                if len(tool_name) < 3 or tool_name.endswith("_"):
                    # Try to use endpoint name from LLM if available
                    llm_name = ep.get("name", "") or ep.get("operationId", "")
                    if llm_name and len(llm_name) >= 3:
                        tool_name = re.sub(r"[^a-zA-Z0-9_]", "_", llm_name)
                    else:
                        # Generate a fallback name
                        tool_name = f"{method.lower()}_endpoint_{idx}"
                    logger.info(f"Using fallback tool name: {tool_name}")

                # Generate URL
                if path.startswith("http"):
                    full_url = path
                else:
                    full_url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))

                # Validate URL
                if not full_url or not full_url.startswith(("http://", "https://")):
                    logger.warning(f"Skipping endpoint {idx}: invalid URL '{full_url}'")
                    continue

                # Get description with fallback
                description = ep.get("description") or ep.get("summary") or f"{method} {path}"

                # Generate schema from LLM parameters
                input_schema = {"type": "object", "properties": {}, "required": []}

                for param in ep.get("parameters", []):
                    p_name = param.get("name")
                    if not p_name:
                        continue

                    p_type = param.get("type", "string")
                    p_desc = param.get("description", "")
                    p_req = param.get("required", False)

                    # Basic type mapping
                    json_type = "string"
                    if p_type.lower() in ["integer", "int", "number"]:
                        json_type = "integer"
                    elif p_type.lower() in ["boolean", "bool"]:
                        json_type = "boolean"

                    input_schema["properties"][p_name] = {"type": json_type, "description": p_desc}
                    if p_req:
                        input_schema["required"].append(p_name)

                # Add body if needed and not present
                if method in ["POST", "PUT", "PATCH"] and "body" not in input_schema["properties"]:
                    input_schema["properties"]["body"] = {"type": "object", "description": "Request body"}

                all_tags = (tags or []) + ["api-docs", "llm-extracted"]

                annotations = {"title": tool_name, "api_doc_method": method, "api_doc_path": path, "confidence_rating": ep.get("confidence", 5), "generated_from": "llm_analysis"}

                tool = ToolCreate(
                    name=tool_name,
                    url=full_url,
                    description=description,
                    integration_type="REST",
                    request_type=method,
                    input_schema=input_schema,
                    annotations=annotations,
                    auth=None,  # Auth handled later or via defaults
                    gateway_id=gateway_id,
                    tags=all_tags,
                )
                tools.append(tool)

            except Exception as e:
                logger.warning(f"Failed to create tool from LLM endpoint {ep.get('path')}: {str(e)}")

        return tools

    async def _create_tool_from_endpoint(self, endpoint: Dict[str, Any], doc_structure: Dict[str, Any], base_url: str, gateway_id: Optional[str], additional_tags: List[str]) -> Optional[ToolCreate]:
        """Create a tool from a detected endpoint.

        Args:
            endpoint: Endpoint information
            doc_structure: Full documentation structure
            base_url: Base URL for the API
            gateway_id: Gateway ID
            additional_tags: Additional tags

        Returns:
            ToolCreate object or None if creation fails
        """
        try:
            method = endpoint.get("method", "GET")
            path = endpoint.get("path", "")

            if not path:
                return None

            # Generate tool name
            tool_name = self._generate_tool_name(method, path)

            # Build full URL
            if endpoint.get("full_url"):
                full_url = endpoint["full_url"]
            else:
                full_url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))

            # Generate description from context
            description = self._generate_description(endpoint, doc_structure)

            # Generate input schema
            input_schema = self._generate_input_schema_from_endpoint(endpoint, doc_structure)

            # Generate tags
            all_tags = additional_tags + ["api-docs", "auto-generated", doc_structure.get("source_format", "unknown")]

            # Create annotations
            annotations = {
                "title": tool_name,
                "api_doc_method": method,
                "api_doc_path": path,
                "source_format": doc_structure.get("source_format", "unknown"),
                "destructiveHint": method in ["DELETE", "POST", "PUT", "PATCH"],
                "idempotentHint": method in ["GET", "PUT", "DELETE"],
                "generated_from": "api_documentation",
            }

            if doc_structure.get("source_url"):
                annotations["source_url"] = doc_structure["source_url"]

            # Basic authentication setup based on detected patterns
            auth = None
            auth_methods = doc_structure.get("authentication_info", {}).get("methods", [])
            if auth_methods:
                if "bearer" in auth_methods:
                    auth = {"auth_type": "bearer", "auth_value": "REPLACE_WITH_BEARER_TOKEN"}
                elif "api_key" in auth_methods:
                    auth = {"auth_type": "authheaders", "auth_header_key": "X-API-Key", "auth_header_value": "REPLACE_WITH_API_KEY"}
                elif "basic" in auth_methods:
                    auth = {"auth_type": "basic", "username": "REPLACE_WITH_USERNAME", "password": "REPLACE_WITH_PASSWORD"}

            tool = ToolCreate(
                name=tool_name,
                url=full_url,
                description=description,
                integration_type="REST",
                request_type=method,
                input_schema=input_schema,
                annotations=annotations,
                auth=auth,
                gateway_id=gateway_id,
                tags=all_tags,
            )

            return tool

        except Exception as e:
            logger.error(f"Failed to create tool from endpoint: {str(e)}")
            return None

    def _generate_tool_name(self, method: str, path: str) -> str:
        """Generate a tool name from method and path.

        Args:
            method: HTTP method
            path: API path

        Returns:
            Generated tool name
        """
        # Clean path and convert to camelCase
        clean_path = re.sub(r"\{[^}]+\}", "", path)  # Remove path parameters
        path_parts = [part for part in clean_path.split("/") if part]

        # Convert to camelCase
        if path_parts:
            tool_name = method.lower() + "".join(word.capitalize() for word in path_parts)
        else:
            tool_name = method.lower() + "Root"

        # Sanitize tool name
        return re.sub(r"[^a-zA-Z0-9_]", "_", tool_name)

    def _generate_description(self, endpoint: Dict[str, Any], doc_structure: Dict[str, Any]) -> str:
        """Generate description for an endpoint tool.

        Args:
            endpoint: Endpoint information
            doc_structure: Full documentation structure

        Returns:
            Generated description
        """
        method = endpoint.get("method", "GET")
        path = endpoint.get("path", "")
        context = endpoint.get("context", "")

        # Try to extract meaningful description from context
        if context:
            # Look for sentences containing the endpoint
            sentences = re.split(r"[.!?]+", context)
            for sentence in sentences:
                if path in sentence or method.lower() in sentence.lower():
                    clean_sentence = sentence.strip()
                    if len(clean_sentence) > 10:
                        return clean_sentence

        # Generate default description
        action_map = {"GET": "Retrieve", "POST": "Create", "PUT": "Update", "DELETE": "Delete", "PATCH": "Partially update"}

        action = action_map.get(method, method.title())
        resource = path.split("/")[-1] if path else "resource"

        return f"{action} {resource} via {method} {path}"

    def _generate_input_schema_from_endpoint(self, endpoint: Dict[str, Any], doc_structure: Dict[str, Any]) -> Dict[str, Any]:
        """Generate input schema for an endpoint.

        Args:
            endpoint: Endpoint information
            doc_structure: Full documentation structure

        Returns:
            JSON schema for tool input
        """
        schema = {"type": "object", "properties": {}, "required": []}

        path = endpoint.get("path", "")

        # Extract path parameters
        path_params = re.findall(r"\{([^}]+)\}", path)
        for param_name in path_params:
            schema["properties"][param_name] = {"type": "string", "description": f"Path parameter: {param_name}"}
            schema["required"].append(param_name)

        # Add common parameters from documentation
        doc_params = doc_structure.get("parameters", [])
        for param in doc_params:
            param_name = param.get("name", "")
            if param_name and param_name not in schema["properties"]:
                param_type = param.get("type", "query")
                schema["properties"][param_name] = {"type": "string", "description": param.get("description", f"{param_type} parameter: {param_name}")}

        # Add request body for POST/PUT/PATCH
        method = endpoint.get("method", "GET")
        if method in ["POST", "PUT", "PATCH"]:
            schema["properties"]["body"] = {"type": "object", "description": "Request body data"}

        return schema
