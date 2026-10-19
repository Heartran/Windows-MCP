# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Windows-MCP is a Python MCP (Model Context Protocol) server that bridges AI LLM agents with the Windows OS, enabling direct desktop automation. It exposes 21 tools via FastMCP:

| Group | Tools |
|---|---|
| Capture | `Screenshot`, `Snapshot`, `Scrape`, `DisplayInventory` |
| Input | `Click`, `Type`, `Scroll`, `Move` (also drag-and-drop via `drag=True`), `Shortcut`, `MultiSelect`, `MultiEdit` |
| Timing | `Wait`, `WaitFor` |
| System | `App`, `PowerShell`, `FileSystem`, `Registry`, `Process`, `Clipboard`, `Notification`, `ControlStatus` |

Tool names are defined by the `name=` argument of each `@mcp.tool(...)` in `src/windows_mcp/tools/`; that directory is the source of truth. Note the shell tool is registered as `PowerShell`, not `Shell`. Any subset can be removed at startup with `serve --exclude-tools` (e.g. `windows-mcp serve --exclude-tools PowerShell,Registry`).

## Build & Development Commands

```bash
uv sync --extra dev        # Install runtime and development dependencies
uv run windows-mcp serve   # Run the MCP server
uv run --extra dev ruff format .  # Format code
uv run --extra dev ruff check .   # Lint code
uv run --extra dev ruff check --fix .  # Lint and auto-fix
uv run --extra dev pytest         # Run all tests
uv run --extra dev pytest tests/test_desktop_views.py  # Run a single test file
```

**Package manager**: UV (not pip). **Python**: 3.14+. **Build backend**: setuptools.

## Architecture

The codebase follows a layered service architecture under `src/windows_mcp/`:

**Entry point** — `__main__.py`: Builds the FastMCP server, parses CLI flags, and selects the transport. Tool registration is delegated to `tools.register_all()`; an async lifespan initializes the Desktop, WatchDog, and Analytics singletons, which tools resolve lazily through the `get_desktop` / `get_analytics` callables.

**Tools layer** — `tools/`: One module per tool group, each exposing `register(mcp, *, get_desktop, get_analytics)`. `tools/__init__.py` holds the module list and `register_all()`. Tool functions are thin — they normalize arguments and delegate to a service package. The `@with_analytics` decorator wraps each one for telemetry, making it the existing precedent for cross-cutting concerns at the tool boundary.

**MCP App views** — `tools/ui/`: HTML pages served as `ui://` resources (MIME `text/html;profile=mcp-app`) for hosts that implement the MCP Apps extension, such as Claude Desktop and claude.ai. A tool opts in with `app=AppConfig(resource_uri=...)` on its `@mcp.tool(...)`, which FastMCP turns into `_meta.ui.resourceUri`; the host then renders the page in a sandboxed iframe and feeds it the tool arguments and result over JSON-RPC `postMessage` (`ui/notifications/tool-input`, `ui/notifications/tool-result`). Today only `PowerShell` has a view (`powershell.html`, a terminal-window rendering of command, output, exit code and duration); its result keeps the legacy `Response: ...\nStatus Code: N` text block and adds the same data as structured content for the page. Pages must be self-contained (inline CSS/JS, no external origins) and are shipped via `[tool.setuptools.package-data]`. Hosts without MCP Apps ignore the metadata and show the text.

**Control gate** — `tools/control_notifications.py` + `desktop/control.py` + `tools/control_policy.py`: the `ControlToolGate` middleware refuses tool calls with `USER_CONTROL` while the user owns the desktop (any physical mouse/keyboard event within the idle timeout, 10 seconds by default, typing a prompt included). Which tools need desktop ownership is decided by one policy table in `control_policy.py`: `relaxed` (default) gates only direct input (`Click`, `Type`, `Scroll`, `Move`, `Shortcut`, `MultiSelect`, `MultiEdit`) and `App` `switch`/`resize`; `balanced` adds `PowerShell`, `Notification`, `App` launches, `Process` `kill` and `Clipboard` `set`; `strict` gates everything. Unclassified tools are gated, so a new tool or mode must be added to that table. `Screenshot` and `Snapshot` are never gated but still queue on the gate's call lock (`CAPTURE` / `serializes_with_control`), because `Desktop.get_screenshot()` pauses the control indicator and input suppression and would preempt a protected call running concurrently. Configured via `WINDOWS_MCP_CONTROL`, `WINDOWS_MCP_CONTROL_POLICY`, `WINDOWS_MCP_CONTROL_IDLE_TIMEOUT` or the matching CLI flags.

**Desktop service** — `desktop/service.py`: High-level orchestrator. Manages window operations (launch, resize, switch), screenshots, mouse/keyboard actions, and clipboard. Interfaces with Tree service for UI element discovery. `desktop/views.py` defines data models: `DesktopState`, `Window`, `Size`, `BoundingBox`, `Status`.

**Tree service** — `tree/service.py`: Captures the Windows accessibility tree from active and background windows. Identifies interactive elements and scrollable areas. Uses `ThreadPoolExecutor` for multi-threaded UI traversal. `tree/views.py` defines `TreeElementNode`, `ScrollElementNode`, `TreeState`. `tree/config.py` has control type configurations.

**UIAutomation wrapper** — `uia/`: Low-level abstraction over the Windows UIAutomation COM API via `comtypes`. `core.py` wraps the main automation object, `controls.py` has control-specific logic, `patterns.py` wraps UIAutomation patterns, `enums.py` has COM enumerations, `events.py` handles event subscriptions.

**WatchDog** — `watchdog/service.py`: Runs in a separate thread monitoring UI focus changes via UIAutomation events. Notifies the Tree service of focus changes so the accessibility tree stays current.

**Virtual Desktop Manager** — `vdm/core.py`: Tracks which windows belong to which Windows virtual desktop (Win10/11).

**Domain services** — thin packages backing the system tools: `filesystem/` (read/write/copy/move/delete/list/search/info), `registry/` (get/set/delete/list, implemented via PowerShell cmdlets), `powershell/` (`PowerShellExecutor` plus environment resolution), `process/` (list/kill), `notifications/`. Registry and PowerShell tools shell out, so their latency is dominated by process startup.

**Infrastructure** — `infrastructure/`: cross-cutting concerns. `analytics.py` (optional PostHog telemetry, disabled with `ANONYMIZED_TELEMETRY=false`; records tool names and errors only, never arguments or outputs), `auth.py` and `oauth.py` (bearer-token and OAuth middleware for HTTP transports), `security.py` (SSRF validation, IP allowlist middleware), `config.py` (server configuration). Note `windows_mcp/config.py` at the package root is unrelated — it only holds the `WINDOWS_MCP_DEBUG` helpers.

## Code Style

- Formatter/linter: **Ruff** (line length 100, double quotes)
- Naming: PEP 8 — `snake_case` functions/variables, `PascalCase` classes, `UPPER_CASE` constants
- Type hints required on function signatures
- Google-style docstrings for public functions/classes

## Key Design Details

- Screenshots are capped to 1920x1080 for token efficiency
- Mouse/keyboard input uses UIA (same coordinate space as BoundingRectangle; no DPI mismatch)
- Screenshot is the preferred fast visual-context tool; Snapshot is the heavier path for UI element ids and DOM extraction
- Browser detection (Chrome, Edge, Firefox) triggers special DOM extraction mode in Snapshot
- Fuzzy string matching (`thefuzz`) is used for element name matching
- UI element fetching has retry logic (`THREAD_MAX_RETRIES=3` in tree service)
- The server supports stdio, SSE, and streamable HTTP transports

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `WINDOWS_MCP_SCREENSHOT_SCALE` | `1.0` | Scale factor for screenshots (range `0.1`–`1.0`). Lower on 1440p/4K to stay under Claude Desktop's 1 MB limit. Resolved in `tools/_snapshot_helpers.py`. |
| `WINDOWS_MCP_SCREENSHOT_BACKEND` | `auto` | Screenshot backend: `auto`, `dxcam`, `mss`, `pillow`. Resolved in `desktop/screenshot.py`. |
| `WINDOWS_MCP_MAX_TREE_ELEMENTS` | `500` | Max UI elements a single Snapshot/WaitFor tree capture may collect before it stops descending and returns a truncated tree (with a note in the output). Bounds both traversal time and response size on huge flat lists/grids (e.g. an unfiltered inventory view with thousands of rows). Resolved in `tree/budget.py`. |
| `WINDOWS_MCP_PROFILE_SNAPSHOT` | _(off)_ | Set to `1`/`true`/`yes`/`on` to log per-stage timing for Screenshot/Snapshot. Checked in `tools/_snapshot_helpers.py` and `desktop/service.py`. |
| `ANONYMIZED_TELEMETRY` | `true` | Set to `false` to disable PostHog telemetry. Checked in `__main__.py` and `infrastructure/analytics.py`. |
| `POSTHOG_API_KEY` | Project default | Override the PostHog project write key used for anonymous telemetry. Set to an empty string to skip PostHog client initialization. Checked in `infrastructure/analytics.py`. |
| `POSTHOG_HOST` | `https://us.i.posthog.com` | Override the PostHog host for anonymous telemetry, such as for a self-hosted PostHog deployment. Checked in `infrastructure/analytics.py`. |
| `WINDOWS_MCP_WATCHDOG` | _(off)_ | Set to `on`/`1`/`true`/`yes`/`enabled` to start the UIA focus WatchDog thread. Unset, or any other value, leaves it off. Opt-in because it only emits debug logging today but can crash the server via the UIA event pump (#332). Resolved in `__main__.py`. |
| `WINDOWS_MCP_DEBUG` | `false` | Set to `1`/`true`/`yes`/`on` to enable debug mode. Checked in `config.py`. Also available as `--debug` CLI flag. |
| `WINDOWS_MCP_DISABLE_FLASH` | _(off)_ | Set to `1`/`true`/`yes`/`on` to suppress the orange-red glowing border that briefly appears after every screenshot. Resolved in `desktop/flash_overlay.py`. |
| `WINDOWS_MCP_POWERSHELL_UI` | `on` | Set to `off`/`0`/`false`/`no`/`disabled` to stop the `PowerShell` tool from registering and advertising its MCP App view. Resolved in `tools/ui/__init__.py`. |

## Security Context

This server has **full system access** with no sandboxing. `PowerShell`, `FileSystem`, `Registry`, `Process`, and `App` can all perform irreversible operations, and there is no audit log or rollback. The recommended deployment target is a VM or Windows Sandbox. Use `serve --exclude-tools` to drop the tools a given deployment does not need.
