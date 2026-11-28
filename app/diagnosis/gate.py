"""The policy gate (issue #51, ADR 0001): an account change runs only after the agent approves.

When a diagnosis is supported and its fix needs ``unlock_account`` or ``reset_mfa``, the
diagnosis graph stops at a LangGraph interrupt with a ``Proposal``: the action, the employee,
the cause and the evidence. The helpdesk agent answers with an ``Approval``:

- ``approve``: run the proposed action. Needs identity verified first (handbook ``pw-4``).
- ``edit``: run a different account change instead, also after identity verification.
- ``reject``: run nothing, and say why.

``ActionRunner`` is the only code that can call the account-changing tools, and only the
gate's ``act`` step holds one. Each run is recorded: who approved, when, what ran, the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from mcp import Client
from pydantic import BaseModel, ConfigDict, model_validator

from app.mock.company import Company
from app.mock.systems import ACCOUNT_CHANGING_TOOLS, SERVERS, World

AccountChange = Literal["unlock_account", "reset_mfa"]


class Approval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["approve", "edit", "reject"]
    approver: str
    identity_verified: bool = False
    tool: AccountChange | None = None  # the change to run instead, for edit
    reason: str | None = None

    @model_validator(mode="after")
    def _rules(self) -> Approval:
        if not self.approver.strip():
            raise ValueError("an approval needs the approver's name")
        if self.action in ("approve", "edit") and not self.identity_verified:
            raise ValueError("verify the employee's identity first (handbook pw-4)")
        if self.action == "edit" and self.tool is None:
            raise ValueError("edit needs the account change to run instead")
        if self.action != "edit" and self.tool is not None:
            raise ValueError("only edit names a different account change")
        if self.action == "reject" and not (self.reason or "").strip():
            raise ValueError("a rejection needs a reason")
        return self


class Refused(Exception):
    """An account change that may not run."""


@dataclass
class ActionRunner:
    """Calls the actions MCP server. Held only by the gate's ``act`` step."""

    company: Company

    def __post_init__(self) -> None:
        self.world = World(self.company)
        self.calls: list[tuple[str, str]] = []

    async def run(self, tool: str, employee_id: str) -> dict[str, Any]:
        if tool not in ACCOUNT_CHANGING_TOOLS:
            raise Refused(f"{tool} is not an account change; diagnosis checks go through CheckRunner")
        self.calls.append((tool, employee_id))
        async with Client(SERVERS["actions"](self.world)) as client:
            reply = await client.call_tool(tool, {"employee": employee_id})
        if reply.is_error:
            text = " ".join(getattr(c, "text", "") for c in reply.content)
            return {"error": text[:300]}
        return reply.structured_content
