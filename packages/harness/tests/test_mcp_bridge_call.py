"""bg3harness.testing.mcp_bridge.call: a failure the tool reports (sent with is_error, bg3ai.tool_classes.failed) comes
back as text for the test to read; an exception in the tool (the server's "Error executing tool ...") raises."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from bg3harness.testing import mcp_bridge
from mcp.types import CallToolResult, TextContent


class Session:
    def __init__(self, text: str, is_error: bool):
        self.res = CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)

    async def call_tool(self, tool: str, args: dict[str, Any]) -> CallToolResult:
        return self.res


def call(text: str, is_error: bool) -> str:
    return asyncio.run(mcp_bridge.call(cast(Any, Session(text, is_error)), "choose_dialogue"))


def test_a_reported_failure_is_returned_and_a_crash_raises():
    assert call("done", False) == "done"
    assert call("FAILED: bg3le: UI busy", True) == "FAILED: bg3le: UI busy"
    with pytest.raises(AssertionError, match="choose_dialogue failed: Error executing tool choose_dialogue"):
        call("Error executing tool choose_dialogue", True)
