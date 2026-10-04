"""The PowerShell tool ships an MCP App view of its request and response.

Hosts that implement the MCP Apps extension (Claude Desktop, claude.ai) render
``_meta.ui.resourceUri`` inside an iframe fed by ``ui/notifications/tool-input``
and ``ui/notifications/tool-result``. These tests pin the wire contract: the
tool advertises the view, the view is served with the MCP App MIME type, and
the result carries structured fields next to the unchanged text block.
"""

import asyncio

import pytest

try:
    from fastmcp import FastMCP
except ImportError:  # fastmcp not on the test platform
    FastMCP = None

pytestmark = pytest.mark.skipif(FastMCP is None, reason="fastmcp not installed")

from windows_mcp.tools.ui import (  # noqa: E402
    POWERSHELL_UI_ENV,
    POWERSHELL_UI_URI,
    load_powershell_ui,
    powershell_ui_enabled,
)

MCP_APP_MIME = "text/html;profile=mcp-app"


def _server(monkeypatch, ui_env: str | None = None) -> "FastMCP":
    if ui_env is None:
        monkeypatch.delenv(POWERSHELL_UI_ENV, raising=False)
    else:
        monkeypatch.setenv(POWERSHELL_UI_ENV, ui_env)
    from windows_mcp.tools.shell import register

    server = FastMCP(name="windows-mcp-test")
    register(server, get_desktop=lambda: None, get_analytics=lambda: None)
    return server


def _fake_executor(monkeypatch, output: str, status_code: int) -> list:
    from windows_mcp.powershell import PowerShellExecutor

    calls: list = []

    def fake(command, timeout=10, shell=None):  # noqa: ARG001
        calls.append((command, timeout))
        return output, status_code

    monkeypatch.setattr(PowerShellExecutor, "execute_command", staticmethod(fake))
    return calls


def test_tool_advertises_ui_resource(monkeypatch):
    server = _server(monkeypatch)

    tool = asyncio.run(server.get_tool("PowerShell"))
    assert tool.meta["ui"]["resourceUri"] == POWERSHELL_UI_URI
    assert tool.meta["ui"]["permissions"] == {"clipboardWrite": {}}

    wire = tool.to_mcp_tool()
    assert wire.meta["ui"]["resourceUri"] == POWERSHELL_UI_URI


def test_ui_resource_is_served_as_mcp_app(monkeypatch):
    server = _server(monkeypatch)

    listed = {str(r.uri): r for r in asyncio.run(server.list_resources())}
    resource = listed[POWERSHELL_UI_URI]
    assert resource.mime_type == MCP_APP_MIME

    read = asyncio.run(server.read_resource(POWERSHELL_UI_URI))
    assert len(read.contents) == 1
    content = read.contents[0]
    assert content.mime_type == MCP_APP_MIME
    assert content.content == load_powershell_ui()
    # Hosts read the iframe policy from the resource's _meta.ui.
    assert content.meta["ui"] == {"permissions": {"clipboardWrite": {}}, "prefersBorder": False}


def test_ui_page_speaks_the_mcp_apps_protocol():
    html = load_powershell_ui()
    assert html.lstrip().lower().startswith("<!doctype html>")
    for method in (
        "ui/initialize",
        "ui/notifications/initialized",
        "ui/notifications/tool-input",
        "ui/notifications/tool-result",
        "ui/notifications/tool-cancelled",
        "ui/notifications/host-context-changed",
        "ui/notifications/size-changed",
    ):
        assert method in html, method
    # Self-contained: no external scripts or stylesheets to trip the host CSP.
    assert "<script src=" not in html
    assert "<link " not in html


def test_result_keeps_text_block_and_adds_structured_content(monkeypatch):
    server = _server(monkeypatch)
    calls = _fake_executor(monkeypatch, "hello\r\n", 0)

    result = asyncio.run(server.call_tool("PowerShell", {"command": "Get-Date", "timeout": 5}))

    assert calls == [("Get-Date", 5)]
    assert [block.text for block in result.content] == ["Response: hello\r\n\nStatus Code: 0"]
    structured = result.structured_content
    assert structured["command"] == "Get-Date"
    assert structured["timeout"] == 5
    assert structured["output"] == "hello\r\n"
    assert structured["status_code"] == 0
    assert isinstance(structured["cwd"], str) and structured["cwd"]
    assert isinstance(structured["duration_ms"], int) and structured["duration_ms"] >= 0


def test_non_zero_exit_code_is_reported_verbatim(monkeypatch):
    server = _server(monkeypatch)
    _fake_executor(monkeypatch, "Access is denied", 1)

    result = asyncio.run(server.call_tool("PowerShell", {"command": "Stop-Service x"}))

    assert result.content[0].text == "Response: Access is denied\nStatus Code: 1"
    assert result.structured_content["status_code"] == 1
    assert result.structured_content["timeout"] == 30


def test_ui_can_be_switched_off(monkeypatch):
    server = _server(monkeypatch, ui_env="off")

    tool = asyncio.run(server.get_tool("PowerShell"))
    assert not (tool.meta or {}).get("ui")
    assert POWERSHELL_UI_URI not in {str(r.uri) for r in asyncio.run(server.list_resources())}

    # The structured result stays: it is harmless for hosts without MCP Apps.
    _fake_executor(monkeypatch, "ok", 0)
    result = asyncio.run(server.call_tool("PowerShell", {"command": "echo ok"}))
    assert result.content[0].text == "Response: ok\nStatus Code: 0"
    assert result.structured_content["output"] == "ok"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        ("", True),
        ("1", True),
        ("on", True),
        ("anything", True),
        ("0", False),
        ("false", False),
        ("  OFF ", False),
        ("No", False),
        ("disabled", False),
    ],
)
def test_powershell_ui_enabled_env_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(POWERSHELL_UI_ENV, raising=False)
    else:
        monkeypatch.setenv(POWERSHELL_UI_ENV, value)
    assert powershell_ui_enabled() is expected
