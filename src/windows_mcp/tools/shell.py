"""PowerShell tool — shell/command execution."""

import os
import time

from fastmcp import Context
from fastmcp.apps import AppConfig, ResourcePermissions
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations

from windows_mcp.infrastructure import with_analytics
from windows_mcp.powershell import PowerShellExecutor
from windows_mcp.tools.ui import (
    POWERSHELL_UI_URI,
    load_powershell_ui,
    powershell_app_config,
    powershell_ui_enabled,
)


def build_powershell_result(
    command: str,
    timeout: int,
    response: str,
    status_code: int,
    duration_ms: int,
) -> ToolResult:
    """Wrap a PowerShell run as a tool result.

    The text block keeps the historical ``Response: ...\\nStatus Code: N`` layout
    that models and clients already parse. The structured content carries the
    same data as typed fields for the MCP App view (and any client that prefers
    JSON over scraping the text).

    Args:
        command: The PowerShell command that was executed.
        timeout: The timeout, in seconds, the command was given.
        response: Captured stdout (or stderr when stdout was empty).
        status_code: The process exit code.
        duration_ms: Wall-clock duration of the run in milliseconds.

    Returns:
        A ``ToolResult`` with one text block and structured content.
    """
    text = f"Response: {response}\nStatus Code: {status_code}"
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={
            "command": command,
            "timeout": timeout,
            "cwd": os.path.expanduser("~"),
            "output": response,
            "status_code": status_code,
            "duration_ms": duration_ms,
        },
    )


def register(mcp, *, get_desktop, get_analytics):
    ui_enabled = powershell_ui_enabled()
    if ui_enabled:
        mcp.resource(
            POWERSHELL_UI_URI,
            name="PowerShellWindow",
            title="PowerShell window",
            description=(
                "Interactive view of a PowerShell tool call: the command, its output "
                "and exit code rendered as a terminal window. Rendered by hosts that "
                "support MCP Apps; not meant to be read by the model."
            ),
            app=AppConfig(
                prefers_border=False,
                permissions=ResourcePermissions(clipboard_write={}),
            ),
        )(load_powershell_ui)

    @mcp.tool(
        name="PowerShell",
        description="Shell/command execution. Keywords: shell, run, execute, cmd, terminal, command line, script. A comprehensive system tool for executing any PowerShell commands. Use it to navigate the file system, manage files and processes, and execute system-level operations. Capable of accessing web content (e.g., via Invoke-WebRequest), interacting with network resources, and performing complex administrative tasks. This tool provides full access to the underlying operating system capabilities, making it the primary interface for system automation, scripting, and deep system interaction.",
        annotations=ToolAnnotations(
            title="PowerShell",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
        app=powershell_app_config() if ui_enabled else None,
    )
    @with_analytics(get_analytics(), "Powershell-Tool")
    def powershell_tool(command: str, timeout: int = 30, ctx: Context = None) -> ToolResult:
        try:
            started = time.monotonic()
            response, status_code = PowerShellExecutor.execute_command(command, timeout)
            duration_ms = int((time.monotonic() - started) * 1000)
            return build_powershell_result(command, timeout, response, status_code, duration_ms)
        except Exception:
            raise
