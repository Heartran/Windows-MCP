"""Policy behavior at the MCP boundary, without hooks or physical input."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastmcp import Client, FastMCP

from windows_mcp.desktop.control import ControlCoordinator
from windows_mcp.tools import register_all
from windows_mcp.tools.control_notifications import ControlNotifier, ControlToolGate
from windows_mcp.tools.control_policy import (
    CAPTURE,
    CLASSIFIED_TOOLS,
    MODES,
    requires_control,
    serializes_with_control,
)


@pytest.mark.parametrize("policy", ["relaxed", "balanced", "strict"])
@pytest.mark.parametrize(
    "name,args,category",
    [
        (name, {}, "direct")
        for name in ("Click", "Type", "Scroll", "Move", "Shortcut", "MultiSelect", "MultiEdit")
    ]
    + [(name, {}, "possible") for name in ("PowerShell", "Notification")]
    + [
        (name, {}, "other")
        for name in (
            "FileSystem",
            "Registry",
            "Snapshot",
            "Screenshot",
            "Scrape",
            "Wait",
            "WaitFor",
            "DisplayInventory",
        )
    ]
    + [
        ("App", {}, "possible"),
        ("App", {"mode": "launch"}, "possible"),
        ("App", {"mode": "launch_executable"}, "possible"),
        ("App", {"mode": "switch"}, "direct"),
        ("App", {"mode": "resize"}, "direct"),
    ]
    + [
        ("Process", {"mode": "list"}, "other"),
        ("Process", {"mode": "kill"}, "possible"),
        ("Clipboard", {"mode": "get"}, "other"),
        ("Clipboard", {"mode": "set"}, "possible"),
    ],
)
def test_operation_matrix(policy, name, args, category):
    expected = (
        category == "direct"
        or policy == "strict"
        or (policy == "balanced" and category == "possible")
    )
    assert requires_control(policy, name, args) is expected


@pytest.mark.parametrize("policy", ["relaxed", "balanced", "strict"])
def test_unknown_and_status(policy):
    assert requires_control(policy, "NewTool")
    for tool in MODES:
        for mode in ("new-mode", None, [], {}):
            assert requires_control(policy, tool, {"mode": mode})
    assert not requires_control(policy, "ControlStatus")


@pytest.mark.asyncio
async def test_registration_and_mode_coverage():
    mcp = FastMCP("policy-coverage")
    register_all(mcp, get_desktop=lambda: None, get_analytics=lambda: None)
    async with Client(mcp) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools} == CLASSIFIED_TOOLS
    for tool in tools:
        if tool.name in MODES:
            schema = tool.input_schema if hasattr(tool, "input_schema") else tool.inputSchema
            assert set(schema["properties"]["mode"]["enum"]) == set(MODES[tool.name])


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["user", "unavailable"])
@pytest.mark.parametrize("policy", ["relaxed", "balanced", "strict"])
async def test_live_mcp_gate_decisions(policy, state):
    owner = ControlCoordinator()
    owner.policy = policy
    owner._state = state
    owner._last_user = time.monotonic()
    mcp = FastMCP("policy-gate")
    mcp.add_middleware(ControlToolGate(owner, ControlNotifier(owner)))
    effects = []
    for name in ("PowerShell", "Wait", "Click", "Clipboard"):

        def probe(mode: str = "get", _name=name):
            effects.append(_name)
            return "executed"

        mcp.tool(name=name)(probe)
    async with Client(mcp) as client:
        for name, arguments, expected in (
            ("PowerShell", {}, policy != "relaxed"),
            ("Wait", {}, policy == "strict"),
            ("Click", {}, True),
            ("Clipboard", {"mode": "get"}, policy == "strict"),
            ("Clipboard", {"mode": "set"}, policy != "relaxed"),
        ):
            count = len(effects)
            result = await client.call_tool(name, arguments, raise_on_error=False)
            assert result.is_error is expected
            assert len(effects) == count + (not expected)


def context(name):
    return SimpleNamespace(message=SimpleNamespace(name=name, arguments={}))


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "error", "cancel"])
async def test_background_does_not_take_lock_or_lease(ending):
    owner = ControlCoordinator()  # Default policy is A; no hooks installed.
    owner.begin_call = Mock(side_effect=AssertionError("background acquired lease"))
    owner.end_call = Mock(side_effect=AssertionError("background extended lease"))
    gate = ControlToolGate(owner, SimpleNamespace(remember=lambda _: None))
    entered, release = asyncio.Event(), asyncio.Event()

    async def background(_):
        entered.set()
        await release.wait()
        if ending == "error":
            raise RuntimeError("background failed")
        return "done"

    # A held desktop lock must not delay this background request.
    await gate._call_lock.acquire()
    task = asyncio.create_task(gate.on_call_tool(context("PowerShell"), background))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        result = await asyncio.wait_for(
            gate.on_call_tool(context("Wait"), lambda _: asyncio.sleep(0, result="query")), 2
        )
        assert result == "query"
        gate._call_lock.release()
        # The long-running background operation must not hold the lock either.
        await asyncio.wait_for(gate._call_lock.acquire(), 2)
        gate._call_lock.release()
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if ending == "error":
                with pytest.raises(RuntimeError, match="background failed"):
                    await task
            else:
                assert await task == "done"
        owner.begin_call.assert_not_called()
        owner.end_call.assert_not_called()
        assert owner._lease_until == 0
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if gate._call_lock.locked():
            gate._call_lock.release()


def test_capture_tools_serialize_but_never_require_ownership():
    assert CAPTURE == {"Screenshot", "Snapshot"}
    for name in CAPTURE:
        assert serializes_with_control(name)
        assert not requires_control("relaxed", name)
        assert not requires_control("balanced", name)
    for name in ("PowerShell", "Wait", "FileSystem", "ControlStatus", "Click"):
        assert not serializes_with_control(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CAPTURE))
async def test_capture_waits_for_desktop_lock_without_a_lease(name):
    """A capture must not overlap a protected call: get_screenshot pauses the
    control indicator and input suppression, which would preempt that call."""
    owner = ControlCoordinator()  # Default policy is relaxed; no hooks installed.
    owner.status = Mock(side_effect=AssertionError("capture consulted ownership"))
    owner.begin_call = Mock(side_effect=AssertionError("capture acquired lease"))
    owner.end_call = Mock(side_effect=AssertionError("capture extended lease"))
    gate = ControlToolGate(owner, SimpleNamespace(remember=lambda _: None))
    captured = asyncio.Event()

    async def capture(_):
        captured.set()
        return "captured"

    # While a protected call holds the lock, the capture queues instead of running.
    await gate._call_lock.acquire()
    task = asyncio.create_task(gate.on_call_tool(context(name), capture))
    try:
        await asyncio.sleep(0.05)
        assert not captured.is_set()
        # A background tool is still not held back by the same lock.
        assert (
            await asyncio.wait_for(
                gate.on_call_tool(context("PowerShell"), lambda _: asyncio.sleep(0, result="bg")),
                2,
            )
            == "bg"
        )
        gate._call_lock.release()
        assert await asyncio.wait_for(task, 2) == "captured"
        # The lock was released again, and no ownership state was touched.
        await asyncio.wait_for(gate._call_lock.acquire(), 2)
        gate._call_lock.release()
        owner.begin_call.assert_not_called()
        owner.end_call.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if gate._call_lock.locked():
            gate._call_lock.release()


@pytest.mark.asyncio
async def test_protected_call_can_run_while_background_is_running():
    owner = ControlCoordinator()
    owner.status = Mock(return_value={"state": "ready"})
    owner.begin_call = Mock(return_value=7)
    owner.checkpoint = Mock()
    owner.end_call = Mock()
    gate = ControlToolGate(owner, SimpleNamespace(remember=lambda _: None))
    entered, release = asyncio.Event(), asyncio.Event()

    async def background(_):
        entered.set()
        await release.wait()

    task = asyncio.create_task(gate.on_call_tool(context("PowerShell"), background))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (
            await asyncio.wait_for(
                gate.on_call_tool(context("Click"), lambda _: asyncio.sleep(0, result="clicked")), 2
            )
            == "clicked"
        )
        owner.begin_call.assert_called_once_with("Click")
        owner.end_call.assert_called_once_with(7)
        assert not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, 2)
