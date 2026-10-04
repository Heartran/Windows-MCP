"""MCP Apps resources — interactive HTML views for tool results.

Hosts that implement the MCP Apps extension (``io.modelcontextprotocol/ui``),
such as Claude Desktop and claude.ai, render a tool's result inside a sandboxed
iframe when the tool advertises ``_meta.ui.resourceUri`` and the server serves
that ``ui://`` resource with the ``text/html;profile=mcp-app`` MIME type. The
view talks to the host over JSON-RPC ``postMessage`` and receives the tool
arguments (``ui/notifications/tool-input``) and the tool result
(``ui/notifications/tool-result``).

Hosts without MCP Apps support ignore the metadata and show the plain text
content, so nothing here changes what the model sees.
"""

from importlib import resources
import os

from fastmcp.apps import AppConfig, ResourcePermissions

__all__ = [
    "POWERSHELL_UI_URI",
    "POWERSHELL_UI_ENV",
    "load_powershell_ui",
    "powershell_app_config",
    "powershell_ui_enabled",
]

POWERSHELL_UI_URI = "ui://windows-mcp/powershell-window.html"
POWERSHELL_UI_ENV = "WINDOWS_MCP_POWERSHELL_UI"

_DISABLING_VALUES = frozenset({"0", "false", "no", "off", "disabled"})


def powershell_ui_enabled() -> bool:
    """Whether the PowerShell tool advertises its MCP App view.

    Reads ``WINDOWS_MCP_POWERSHELL_UI``. Unset means enabled; ``0``, ``false``,
    ``no``, ``off`` or ``disabled`` (case-insensitive, whitespace-trimmed) turn
    the view off so hosts fall back to the plain text result.

    Returns:
        ``True`` when the view should be registered and advertised.
    """
    value = os.getenv(POWERSHELL_UI_ENV)
    if value is None:
        return True
    return value.strip().lower() not in _DISABLING_VALUES


def load_powershell_ui() -> str:
    """Return the HTML document of the PowerShell window view.

    The page is self-contained (inline CSS/JS, no external origins) so it runs
    under the host's default Content Security Policy.
    """
    return resources.files(__package__).joinpath("powershell.html").read_text(encoding="utf-8")


def powershell_app_config() -> AppConfig:
    """Build the ``_meta.ui`` declaration attached to the PowerShell tool."""
    return AppConfig(
        resource_uri=POWERSHELL_UI_URI,
        prefers_border=False,
        permissions=ResourcePermissions(clipboard_write={}),
    )
