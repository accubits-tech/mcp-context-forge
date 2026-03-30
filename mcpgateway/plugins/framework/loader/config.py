# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/plugins/framework/loader/config.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Teryl Taylor, Mihai Criveti

Configuration loader implementation.
This module loads configurations for plugins.
"""

# Standard
import os
from typing import Dict

# Third-Party
import jinja2
import yaml

# First-Party
from mcpgateway.plugins.framework.models import Config, PluginSettings

_SENSITIVE_ENV_PREFIXES = (
    "JWT_",
    "DATABASE_",
    "REDIS_",
    "BASIC_AUTH_",
    "OPENAI_",
    "ANTHROPIC_",
    "SSO_",
    "OAUTH_",
    "PLATFORM_ADMIN_",
    "AUTH_ENCRYPTION_",
    "POSTGRES_",
    "MYSQL_",
    "SECRET_",
    "PRIVATE_KEY_",
    "API_KEY",
    "AWS_SECRET",
    "AZURE_",
    "GCP_",
    "KEYCLOAK_",
)


def _get_safe_template_env() -> Dict[str, str]:
    """Return filtered environment variables safe for Jinja2 template rendering.

    Strips out any environment variable whose uppercased name starts with a
    known sensitive prefix (e.g. JWT_, DATABASE_, SECRET_) to prevent
    accidental exposure of credentials through plugin config templates.
    """
    return {k: v for k, v in os.environ.items() if not any(k.upper().startswith(prefix) for prefix in _SENSITIVE_ENV_PREFIXES)}


class ConfigLoader:
    """A configuration loader.

    Examples:
        >>> import tempfile
        >>> import os
        >>> from mcpgateway.plugins.framework.models import PluginSettings
        >>> # Create a temporary config file
        >>> with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        ...     _ = f.write(\"\"\"
        ... plugin_settings:
        ...   enable_plugin_api: true
        ...   plugin_timeout: 30
        ... plugin_dirs: ['/path/to/plugins']
        ... \"\"\")
        ...     temp_path = f.name
        >>> try:
        ...     config = ConfigLoader.load_config(temp_path, use_jinja=False)
        ...     config.plugin_settings.enable_plugin_api
        ... finally:
        ...     os.unlink(temp_path)
        True
    """

    @staticmethod
    def load_config(config: str, use_jinja: bool = True) -> Config:
        """Load the plugin configuration from a file path.

        Args:
            config: the configuration path.
            use_jinja: use jinja to replace env variables if true.

        Returns:
            The plugin configuration object.

        Examples:
            >>> import tempfile
            >>> import os
            >>> with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            ...     _ = f.write(\"\"\"
            ... plugin_settings:
            ...   plugin_timeout: 60
            ...   enable_plugin_api: false
            ... plugin_dirs: []
            ... \"\"\")
            ...     temp_path = f.name
            >>> try:
            ...     cfg = ConfigLoader.load_config(temp_path, use_jinja=False)
            ...     cfg.plugin_settings.plugin_timeout
            ... finally:
            ...     os.unlink(temp_path)
            60
        """
        try:
            with open(os.path.normpath(config), "r", encoding="utf-8") as file:
                template = file.read()
                if use_jinja:
                    jinja_env = jinja2.Environment(loader=jinja2.BaseLoader(), autoescape=True)
                    rendered_template = jinja_env.from_string(template).render(env=_get_safe_template_env())
                else:
                    rendered_template = template
                config_data = yaml.safe_load(rendered_template) or {}
            return Config(**config_data)
        except FileNotFoundError:
            # Graceful fallback for tests and minimal environments without plugin config
            return Config(plugins=[], plugin_dirs=[], plugin_settings=PluginSettings())
