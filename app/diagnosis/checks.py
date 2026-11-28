"""Run read-only checks on the company's systems through their MCP servers (#50).

The runner talks to the six mock servers in-process, as a real MCP client: it lists their
tools, keeps only the read-only ones, and refuses everything else. The account-changing
tools (``unlock_account``, ``reset_mfa``) are not reachable from here at all; only the policy
gate (#51) can run them, after the agent approves (ADR 0001).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mcp import Client

from app.mock.company import Company
from app.mock.systems import ACCOUNT_CHANGING_TOOLS, SERVERS, World


class CheckRefused(Exception):
    """A check the diagnosis loop may not run."""


@dataclass(frozen=True)
class CheckResult:
    tool: str
    arguments: dict[str, Any]
    result: Any  # the tool's structured answer, or {"error": ...}
    ok: bool

    def line(self) -> str:
        return f"- {self.tool}: {json.dumps(self.result, sort_keys=True, default=str)}"


@dataclass
class CheckRunner:
    """Read-only checks against one company: the one a ticket's employee is in."""

    company: Company
    _servers: dict[str, str] = field(default_factory=dict, init=False)  # tool → server
    _params: dict[str, set[str]] = field(default_factory=dict, init=False)  # tool → parameter names
    _world: World = field(init=False)

    def __post_init__(self) -> None:
        self._world = World(self.company)

    async def tools(self) -> dict[str, str]:
        """The read-only tools the servers register, mapped to their server. Listed once."""
        if not self._servers:
            for name, build in SERVERS.items():
                async with Client(build(self._world)) as client:
                    for tool in (await client.list_tools()).tools:
                        read_only = bool(tool.annotations and tool.annotations.read_only_hint)
                        if read_only and tool.name not in ACCOUNT_CHANGING_TOOLS:
                            self._servers[tool.name] = name
                            self._params[tool.name] = set((tool.input_schema or {}).get("properties", {}))
        return self._servers

    async def run(self, tool: str, employee_id: str) -> CheckResult:
        servers = await self.tools()
        if tool in ACCOUNT_CHANGING_TOOLS:
            raise CheckRefused(f"{tool} changes an account; only the policy gate can run it, after approval")
        if tool not in servers:
            raise CheckRefused(f"{tool} is not a registered read-only check")
        arguments = {"employee": employee_id} if "employee" in self._params[tool] else {}
        async with Client(SERVERS[servers[tool]](self._world)) as client:
            reply = await client.call_tool(tool, arguments)
        if reply.is_error:
            text = " ".join(getattr(c, "text", "") for c in reply.content)
            return CheckResult(tool, arguments, {"error": text[:300]}, ok=False)
        result = reply.structured_content
        if isinstance(result, dict) and set(result) == {"result"}:  # list results are wrapped by the SDK
            result = result["result"]
        return CheckResult(tool, arguments, result, ok=True)
