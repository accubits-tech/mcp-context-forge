# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/postman_service.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Postman Collection to MCP Tool Converter Service.

This service parses Postman Collection v2.1 format and converts requests
to MCP Gateway tool definitions.
"""

# Standard
import json
import logging
import re
from typing import Any, Dict, List, Optional, Union

# Third-Party
from pydantic import ValidationError

# First-Party
from mcpgateway.schemas import AuthenticationValues, ToolCreate

logger = logging.getLogger(__name__)


class PostmanCollectionError(Exception):
    """Raised when Postman collection parsing fails."""


class PostmanCollectionService:
    """Service for converting Postman Collections to MCP Tools."""

    SUPPORTED_SCHEMAS = [
        "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        "https://schema.getpostman.com/json/collection/v2.0.0/collection.json",
    ]

    def __init__(self):
        """Initialize the Postman Collection Service."""
        self.collection_variables: Dict[str, str] = {}
        self.collection_auth: Optional[Dict[str, Any]] = None

    async def parse_collection(self, content: Union[str, Dict]) -> Dict[str, Any]:
        """Parse Postman collection from JSON string or dict.

        Args:
            content: Postman collection as JSON string or dictionary

        Returns:
            Parsed collection dictionary

        Raises:
            PostmanCollectionError: If parsing fails

        Examples:
            >>> service = PostmanCollectionService()
            >>> import asyncio
            >>> collection = asyncio.run(service.parse_collection('{"info": {"name": "Test"}}'))
            >>> isinstance(collection, dict)
            True
        """
        try:
            if isinstance(content, str):
                collection = json.loads(content)
            else:
                collection = content

            # Validate basic structure
            if not isinstance(collection, dict):
                raise PostmanCollectionError("Collection must be a JSON object")

            if "info" not in collection:
                raise PostmanCollectionError("Collection missing 'info' field")

            return collection

        except json.JSONDecodeError as e:
            raise PostmanCollectionError(f"Invalid JSON: {str(e)}")
        except Exception as e:
            raise PostmanCollectionError(f"Failed to parse collection: {str(e)}")

    async def validate_collection(self, collection: Dict[str, Any]) -> bool:
        """Validate Postman collection structure.

        Args:
            collection: Parsed collection dictionary

        Returns:
            True if valid

        Raises:
            PostmanCollectionError: If validation fails

        Examples:
            >>> service = PostmanCollectionService()
            >>> import asyncio
            >>> collection = {"info": {"schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"}, "item": []}
            >>> asyncio.run(service.validate_collection(collection))
            True
        """
        # Check info section
        info = collection.get("info", {})
        schema = info.get("schema", "")

        if not any(supported in schema for supported in self.SUPPORTED_SCHEMAS):
            logger.warning(f"Unsupported or missing schema: {schema}. Attempting to parse anyway.")

        # Check for items
        if "item" not in collection:
            raise PostmanCollectionError("Collection missing 'item' array")

        if not isinstance(collection["item"], list):
            raise PostmanCollectionError("Collection 'item' must be an array")

        return True

    async def generate_tools_from_collection(
        self,
        collection: Dict[str, Any],
        gateway_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        owner_email: Optional[str] = None,
        team_id: Optional[str] = None,
        visibility: str = "public",
    ) -> List[ToolCreate]:
        """Generate MCP tools from Postman collection.

        Args:
            collection: Parsed and validated Postman collection
            gateway_id: Optional gateway to associate tools with
            tags: Optional tags to add to all tools
            owner_email: Email of tool owner
            team_id: Team ID for tool ownership
            visibility: Tool visibility (public, team, private)

        Returns:
            List of ToolCreate objects

        Raises:
            PostmanCollectionError: If conversion fails

        Examples:
            >>> service = PostmanCollectionService()
            >>> import asyncio
            >>> collection = {
            ...     "info": {"name": "Test", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
            ...     "item": [{
            ...         "name": "Get Users",
            ...         "request": {
            ...             "method": "GET",
            ...             "url": {"raw": "https://api.example.com/users"}
            ...         }
            ...     }]
            ... }
            >>> tools = asyncio.run(service.generate_tools_from_collection(collection))
            >>> len(tools)
            1
        """
        # Extract collection-level metadata
        info = collection.get("info", {})
        collection_name = info.get("name", "Untitled Collection")

        # Extract collection-level auth and variables
        self.collection_auth = collection.get("auth")
        self.collection_variables = self._extract_variables(collection)

        # Process all items (requests and folders)
        tools: List[ToolCreate] = []
        items = collection.get("item", [])

        for item in items:
            try:
                item_tools = await self._process_item(
                    item,
                    folder_path=[],
                    gateway_id=gateway_id,
                    base_tags=tags or [],
                    owner_email=owner_email,
                    team_id=team_id,
                    visibility=visibility,
                )
                tools.extend(item_tools)
            except Exception as e:
                logger.error(f"Failed to process item '{item.get('name', 'unnamed')}': {str(e)}")
                # Continue processing other items

        if not tools:
            raise PostmanCollectionError("No valid tools generated from collection")

        logger.info(f"Generated {len(tools)} tools from collection '{collection_name}'")
        return tools

    async def _process_item(
        self,
        item: Dict[str, Any],
        folder_path: List[str],
        gateway_id: Optional[str],
        base_tags: List[str],
        owner_email: Optional[str],
        team_id: Optional[str],
        visibility: str,
    ) -> List[ToolCreate]:
        """Process a single item (request or folder).

        Args:
            item: Postman item (request or folder)
            folder_path: Current folder path
            gateway_id: Gateway ID
            base_tags: Base tags to apply
            owner_email: Owner email
            team_id: Team ID
            visibility: Visibility level

        Returns:
            List of ToolCreate objects
        """
        tools: List[ToolCreate] = []

        # Check if item is a folder (has nested items)
        if "item" in item and isinstance(item["item"], list):
            # Process folder
            folder_name = item.get("name", "Folder")
            new_folder_path = folder_path + [folder_name]

            for sub_item in item["item"]:
                sub_tools = await self._process_item(
                    sub_item,
                    folder_path=new_folder_path,
                    gateway_id=gateway_id,
                    base_tags=base_tags,
                    owner_email=owner_email,
                    team_id=team_id,
                    visibility=visibility,
                )
                tools.extend(sub_tools)

        # Check if item is a request
        elif "request" in item:
            try:
                tool = self._convert_request_to_tool(
                    request_item=item,
                    folder_path=folder_path,
                    gateway_id=gateway_id,
                    base_tags=base_tags,
                    owner_email=owner_email,
                    team_id=team_id,
                    visibility=visibility,
                )
                tools.append(tool)
            except ValidationError as e:
                logger.error(f"Validation error for request '{item.get('name')}': {str(e)}")
            except Exception as e:
                logger.error(f"Failed to convert request '{item.get('name')}': {str(e)}")

        return tools

    def _convert_request_to_tool(
        self,
        request_item: Dict[str, Any],
        folder_path: List[str],
        gateway_id: Optional[str],
        base_tags: List[str],
        owner_email: Optional[str],
        team_id: Optional[str],
        visibility: str,
    ) -> ToolCreate:
        """Convert a Postman request to a ToolCreate object.

        Args:
            request_item: Postman request item
            folder_path: Folder path for tagging
            gateway_id: Gateway ID
            base_tags: Base tags
            owner_email: Owner email
            team_id: Team ID
            visibility: Visibility level

        Returns:
            ToolCreate object
        """
        request = request_item["request"]
        item_name = request_item.get("name", "Unnamed Request")
        item_description = request_item.get("description", "")

        # Extract request details
        method = self._extract_method(request)
        url_info = self._extract_url(request)
        headers = self._extract_headers(request)
        body_info = self._extract_body(request)
        auth = self._extract_auth(request_item, request)

        # Build tool name (sanitize for MCP)
        tool_name = self._sanitize_tool_name(item_name, folder_path)

        # Build tags (folder path + method)
        tool_tags = base_tags + folder_path + [method]

        # Build input schema
        input_schema = self._build_input_schema(url_info, body_info, headers)

        # Build description
        description = f"{item_description}\n\nMethod: {method}\nURL: {url_info['raw']}" if item_description else f"{method} {url_info['raw']}"

        # Create tool
        tool_data = {
            "name": tool_name,
            "displayName": item_name,
            "description": description,
            "url": url_info["base_url"] + url_info["path"],
            "integration_type": "REST",
            "request_type": method,
            "headers": headers if headers else None,
            "input_schema": input_schema,
            "tags": tool_tags,
            "gateway_id": gateway_id,
            "owner_email": owner_email,
            "team_id": team_id,
            "visibility": visibility,
            "expose_passthrough": True,
        }

        # Add auth if present
        if auth:
            tool_data["auth"] = auth

        # Add body handling
        if body_info:
            tool_data["annotations"] = {"postman_body_mode": body_info.get("mode", "raw")}

        # Add path template if there are path variables
        if url_info.get("has_path_variables"):
            tool_data["path_template"] = url_info["path_template"]

        return ToolCreate(**tool_data)

    def _sanitize_tool_name(self, name: str, folder_path: List[str]) -> str:
        """Sanitize tool name for MCP compatibility.

        Args:
            name: Original name
            folder_path: Folder path

        Returns:
            Sanitized name

        Examples:
            >>> service = PostmanCollectionService()
            >>> service._sanitize_tool_name("Get Users", ["API", "Users"])
            'api_users_get_users'
        """
        # Combine folder path and name
        full_path = "_".join(folder_path + [name])

        # Convert to lowercase and replace spaces/special chars with underscores
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", full_path.lower())

        # Remove consecutive underscores
        sanitized = re.sub(r"_+", "_", sanitized)

        # Remove leading/trailing underscores
        sanitized = sanitized.strip("_")

        return sanitized or "unnamed_tool"

    def _extract_method(self, request: Dict[str, Any]) -> str:
        """Extract HTTP method from request.

        Args:
            request: Postman request object

        Returns:
            HTTP method (GET, POST, etc.)

        Examples:
            >>> service = PostmanCollectionService()
            >>> service._extract_method({"method": "GET"})
            'GET'
        """
        method = request.get("method", "GET")
        return method.upper()

    def _extract_url(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Extract and parse URL from request.

        Args:
            request: Postman request object

        Returns:
            Dict with url, base_url, path, query_params, etc.

        Examples:
            >>> service = PostmanCollectionService()
            >>> result = service._extract_url({"url": {"raw": "https://api.example.com/users?limit=10"}})
            >>> result['base_url']
            'https://api.example.com'
        """
        url_obj = request.get("url", {})

        # Handle both string and object formats
        if isinstance(url_obj, str):
            raw_url = url_obj
            parsed = self._parse_url_string(raw_url)
        else:
            raw_url = url_obj.get("raw", "")
            parsed = self._parse_url_object(url_obj)

        # Substitute variables
        raw_url = self._substitute_variables(raw_url)
        parsed["raw"] = raw_url

        return parsed

    def _parse_url_string(self, url: str) -> Dict[str, Any]:
        """Parse URL string.

        Args:
            url: URL string

        Returns:
            Parsed URL components
        """
        # Simple parsing (can be enhanced with urllib.parse)
        # Extract protocol, host, path, query
        match = re.match(r"(https?://)?([^/]+)(/.*)?(\\?.*)?", url)
        if not match:
            return {"base_url": "", "path": "/", "query_params": [], "has_path_variables": False}

        protocol = match.group(1) or "https://"
        host = match.group(2) or ""
        path = match.group(3) or "/"
        match.group(4) or ""

        base_url = f"{protocol}{host}"

        # Check for path variables
        has_path_variables = ":" in path or "{{" in path

        return {
            "base_url": base_url,
            "path": path,
            "path_template": path if has_path_variables else None,
            "query_params": [],
            "has_path_variables": has_path_variables,
        }

    def _parse_url_object(self, url_obj: Dict[str, Any]) -> Dict[str, Any]:
        """Parse Postman URL object.

        Args:
            url_obj: Postman URL object

        Returns:
            Parsed URL components
        """
        # Extract host
        host_parts = url_obj.get("host", [])
        protocol = "https"  # Default

        if isinstance(host_parts, list) and len(host_parts) > 0:
            # Check if first element contains full URL (some Postman exports do this)
            first_part = str(host_parts[0])
            if first_part.startswith("http://") or first_part.startswith("https://"):
                # Extract protocol and host from full URL
                if first_part.startswith("https://"):
                    protocol = "https"
                    host = first_part.replace("https://", "")
                else:
                    protocol = "http"
                    host = first_part.replace("http://", "")
            else:
                # Normal case - join host parts with dots
                host = ".".join(str(part) for part in host_parts)
        else:
            host = str(host_parts) if host_parts else ""
            # Remove protocol if present in host string
            if "://" in host:
                protocol, host = host.split("://", 1)

        # Override protocol if specified in url_obj
        if "protocol" in url_obj and url_obj["protocol"]:
            protocol = url_obj["protocol"].rstrip(":")

        # Build base URL
        base_url = f"{protocol}://{host}" if host else ""

        # Extract path
        path_parts = url_obj.get("path", [])
        if isinstance(path_parts, list):
            path = "/" + "/".join(str(p) for p in path_parts)
        else:
            path = str(path_parts)

        # Check for path variables (Postman uses :variable syntax)
        has_path_variables = any(":" in str(p) or "{{" in str(p) for p in (path_parts if isinstance(path_parts, list) else [path_parts]))

        # Extract query parameters
        query_params = url_obj.get("query", [])

        return {
            "base_url": base_url,
            "path": path,
            "path_template": path if has_path_variables else None,
            "query_params": query_params,
            "has_path_variables": has_path_variables,
        }

    def _extract_headers(self, request: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Extract headers from request.

        Args:
            request: Postman request object

        Returns:
            Headers dictionary or None

        Examples:
            >>> service = PostmanCollectionService()
            >>> headers = service._extract_headers({"header": [{"key": "Content-Type", "value": "application/json"}]})
            >>> headers['Content-Type']
            'application/json'
        """
        header_list = request.get("header", [])
        if not header_list:
            return None

        headers = {}
        for header in header_list:
            if isinstance(header, dict):
                key = header.get("key")
                value = header.get("value")
                disabled = header.get("disabled", False)

                if key and value and not disabled:
                    # Substitute variables
                    value = self._substitute_variables(value)
                    headers[key] = value

        return headers if headers else None

    def _extract_body(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract body from request.

        Args:
            request: Postman request object

        Returns:
            Body information or None
        """
        body = request.get("body", {})
        if not body:
            return None

        mode = body.get("mode", "raw")
        body_info = {"mode": mode}

        if mode == "raw":
            raw_body = body.get("raw", "")
            options = body.get("options", {})
            raw_options = options.get("raw", {})
            language = raw_options.get("language", "json")

            if language == "json" and raw_body:
                try:
                    # Parse JSON to get structure
                    json_body = json.loads(raw_body)
                    body_info["json_structure"] = json_body
                except json.JSONDecodeError:
                    logger.warning("Failed to parse JSON body")

        elif mode == "formdata":
            formdata = body.get("formdata", [])
            body_info["formdata"] = formdata

        elif mode == "urlencoded":
            urlencoded = body.get("urlencoded", [])
            body_info["urlencoded"] = urlencoded

        return body_info

    def _extract_auth(self, request_item: Dict[str, Any], request: Dict[str, Any]) -> Optional[AuthenticationValues]:
        """Extract authentication from request or use collection-level auth.

        Args:
            request_item: Postman request item (may have auth at item level)
            request: Postman request object (may have auth at request level)

        Returns:
            AuthenticationValues or None
        """
        # Priority: request > request_item > collection
        auth = request.get("auth") or request_item.get("auth") or self.collection_auth

        if not auth:
            return None

        auth_type = auth.get("type")

        if auth_type == "bearer":
            token = self._get_auth_param(auth, "bearer", "token")
            if token:
                token = self._substitute_variables(token)
                return AuthenticationValues(auth_type="bearer", auth_value=self._encode_auth({"Authorization": f"Bearer {token}"}))

        elif auth_type == "apikey":
            key = self._get_auth_param(auth, "apikey", "key")
            value = self._get_auth_param(auth, "apikey", "value")
            add_to = self._get_auth_param(auth, "apikey", "in", "header")

            if key and value:
                value = self._substitute_variables(value)
                if add_to == "header":
                    return AuthenticationValues(auth_type="authheaders", auth_value=self._encode_auth({key: value}))

        elif auth_type == "basic":
            username = self._get_auth_param(auth, "basic", "username")
            password = self._get_auth_param(auth, "basic", "password")

            if username and password:
                username = self._substitute_variables(username)
                password = self._substitute_variables(password)
                # Basic auth format: base64(username:password)
                # Standard
                import base64

                credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
                return AuthenticationValues(auth_type="basic", auth_value=self._encode_auth({"Authorization": f"Basic {credentials}"}))

        return None

    def _get_auth_param(self, auth: Dict[str, Any], auth_type: str, param_key: str, default: Any = None) -> Any:
        """Get authentication parameter value.

        Args:
            auth: Auth object
            auth_type: Auth type (bearer, apikey, basic)
            param_key: Parameter key to extract
            default: Default value if not found

        Returns:
            Parameter value or default
        """
        auth_data = auth.get(auth_type, [])
        if isinstance(auth_data, list):
            for item in auth_data:
                if isinstance(item, dict) and item.get("key") == param_key:
                    return item.get("value", default)
        return default

    def _encode_auth(self, headers: Dict[str, str]) -> str:
        """Encode auth headers as JSON string.

        Args:
            headers: Headers dictionary

        Returns:
            JSON encoded string
        """
        # Standard
        import base64

        return base64.b64encode(json.dumps(headers).encode()).decode()

    def _build_input_schema(self, url_info: Dict[str, Any], body_info: Optional[Dict[str, Any]], headers: Optional[Dict[str, str]]) -> Dict[str, Any]:
        """Build JSON Schema for tool inputs.

        Args:
            url_info: URL information with query params
            body_info: Body information
            headers: Request headers

        Returns:
            JSON Schema for input
        """
        schema: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

        # Add query parameters
        query_params = url_info.get("query_params", [])
        if query_params:
            for param in query_params:
                if isinstance(param, dict):
                    key = param.get("key")
                    value = param.get("value", "")
                    disabled = param.get("disabled", False)
                    description = param.get("description", "")

                    if key and not disabled:
                        schema["properties"][key] = {
                            "type": "string",
                            "description": description or f"Query parameter: {key}",
                            "default": value if value else None,
                        }

        # Add path variables
        if url_info.get("has_path_variables"):
            path = url_info.get("path", "")
            # Extract :variable patterns
            path_vars = re.findall(r":([a-zA-Z_][a-zA-Z0-9_]*)", path)
            # Also extract {{variable}} patterns
            path_vars.extend(re.findall(r"{{([^}]+)}}", path))

            for var in path_vars:
                schema["properties"][var] = {"type": "string", "description": f"Path variable: {var}"}
                schema["required"].append(var)

        # Add body schema
        if body_info:
            if body_info.get("mode") == "raw" and "json_structure" in body_info:
                schema["properties"]["body"] = {"type": "object", "description": "Request body", "properties": self._infer_json_schema(body_info["json_structure"])}
                schema["required"].append("body")
            elif body_info.get("mode") == "formdata":
                formdata_props = {}
                for field in body_info.get("formdata", []):
                    if isinstance(field, dict):
                        key = field.get("key")
                        if key:
                            formdata_props[key] = {"type": "string", "description": field.get("description", "")}
                if formdata_props:
                    schema["properties"]["formdata"] = {"type": "object", "properties": formdata_props}
            elif body_info.get("mode") == "urlencoded":
                urlencoded_props = {}
                for field in body_info.get("urlencoded", []):
                    if isinstance(field, dict):
                        key = field.get("key")
                        if key:
                            urlencoded_props[key] = {"type": "string", "description": field.get("description", "")}
                if urlencoded_props:
                    schema["properties"]["urlencoded"] = {"type": "object", "properties": urlencoded_props}

        # If no properties, return minimal schema
        if not schema["properties"]:
            return {"type": "object", "properties": {}}

        return schema

    def _infer_json_schema(self, data: Any) -> Dict[str, Any]:
        """Infer JSON schema from data structure.

        Args:
            data: Data to infer schema from

        Returns:
            JSON Schema properties
        """
        if isinstance(data, dict):
            properties = {}
            for key, value in data.items():
                if isinstance(value, str):
                    properties[key] = {"type": "string"}
                elif isinstance(value, int):
                    properties[key] = {"type": "integer"}
                elif isinstance(value, float):
                    properties[key] = {"type": "number"}
                elif isinstance(value, bool):
                    properties[key] = {"type": "boolean"}
                elif isinstance(value, list):
                    properties[key] = {"type": "array", "items": {"type": "string"}}
                elif isinstance(value, dict):
                    properties[key] = {"type": "object", "properties": self._infer_json_schema(value)}
            return properties
        return {}

    def _extract_variables(self, collection: Dict[str, Any]) -> Dict[str, str]:
        """Extract variables from collection.

        Args:
            collection: Postman collection

        Returns:
            Variables dictionary

        Examples:
            >>> service = PostmanCollectionService()
            >>> variables = service._extract_variables({"variable": [{"key": "baseUrl", "value": "https://api.example.com"}]})
            >>> variables['baseUrl']
            'https://api.example.com'
        """
        variables = {}
        var_list = collection.get("variable", [])

        for var in var_list:
            if isinstance(var, dict):
                key = var.get("key")
                value = var.get("value")
                if key and value:
                    variables[key] = value

        return variables

    def _substitute_variables(self, text: str) -> str:
        """Substitute Postman variables in text.

        Args:
            text: Text with {{variable}} placeholders

        Returns:
            Text with variables substituted

        Examples:
            >>> service = PostmanCollectionService()
            >>> service.collection_variables = {"baseUrl": "https://api.example.com"}
            >>> service._substitute_variables("{{baseUrl}}/users")
            'https://api.example.com/users'
        """
        if not text or not self.collection_variables:
            return text

        for key, value in self.collection_variables.items():
            text = text.replace(f"{{{{{key}}}}}", value)

        return text
