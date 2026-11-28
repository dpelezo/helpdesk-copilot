"""The mock company's IT systems, served as MCP servers.

Five read-only systems answer the checks the copilot runs, and one separate
server holds the two actions that change an account (ADR 0001: those run only
through the policy gate, after the agent approves).

| Server     | Tools                                             |
| ---------- | ------------------------------------------------- |
| identity   | account_status                                    |
| devices    | device_health, installed_software                 |
| vpn        | vpn_certificate_status, vpn_last_session          |
| status     | service_status, printer_status                    |
| tickets    | recent_tickets                                    |
| actions    | unlock_account, reset_mfa  (change an account)    |

All servers share one ``World``: the generated company, or a copy with a fault
planted in it (#7). Read-only servers never change it.

    python -m app.mock.systems identity            # stdio
    python -m app.mock.systems vpn --http --port 8003
    MOCK_COMPANY=path/to/company.json python -m app.mock.systems devices
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from app.mock.company import FIXTURE, Account, Company, Device, Employee, VpnProfile

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CHANGES_ACCOUNT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

# Tools that change an account. The policy gate must approve every call (ADR 0001).
ACCOUNT_CHANGING_TOOLS = frozenset({"unlock_account", "reset_mfa"})

EmployeeRef = Annotated[
    str,
    Field(description="The employee's id (E001), username (jhicel) or email address.", min_length=1),
]


@dataclass
class World:
    """The company the mock systems read. Actions replace it with an updated copy."""

    company: Company

    @classmethod
    def load(cls, path: Path | None = None) -> World:
        source = path or Path(os.environ.get("MOCK_COMPANY", FIXTURE))
        return cls(Company.model_validate_json(source.read_text()))

    def employee(self, ref: str) -> Employee:
        key = ref.strip().lower()
        accounts = {a.employee_id: a for a in self.company.accounts}
        for e in self.company.employees:
            if key in (e.id.lower(), e.email.lower(), accounts[e.id].username):
                return e
        raise ToolError(f"No employee matches {ref!r}. Use an employee id, username or email.")

    def account(self, ref: str) -> Account:
        emp = self.employee(ref)
        return next(a for a in self.company.accounts if a.employee_id == emp.id)

    def device(self, ref: str) -> Device:
        emp = self.employee(ref)
        return next(d for d in self.company.devices if d.owner_id == emp.id)

    def vpn(self, ref: str) -> VpnProfile:
        emp = self.employee(ref)
        return next(v for v in self.company.vpn if v.employee_id == emp.id)

    def days_since(self, day: date) -> int:
        return (self.company.as_of - day).days

    def replace_account(self, updated: Account) -> None:
        accounts = [updated if a.employee_id == updated.employee_id else a for a in self.company.accounts]
        self.company = self.company.model_copy(update={"accounts": accounts})


# ── Results ────────────────────────────────────────────────────────────


class AccountStatus(BaseModel):
    employee_id: str
    username: str
    locked: bool
    failed_logins: int
    lockout_threshold: int
    password_age_days: int
    password_expired: bool
    mfa_enrolled: bool
    mfa_method: str
    mfa_locked: bool
    mailbox_used_gb: float
    mailbox_quota_gb: float


class DeviceHealth(BaseModel):
    device_id: str
    os: str
    os_build: str
    disk_free_gb: int
    disk_free_percent: float
    cpu_temp_c: int
    compliant: bool
    antivirus_definitions_age_days: int
    pending_updates: int


class Software(BaseModel):
    device_id: str
    installed: list[str]


class CertificateStatus(BaseModel):
    employee_id: str
    expires: date
    days_left: int
    expired: bool


class VpnSession(BaseModel):
    employee_id: str
    client_version: str
    last_error: str | None


class ServiceInfo(BaseModel):
    name: str
    status: str


class PrinterInfo(BaseModel):
    id: str
    site: str
    status: str
    queue_length: int


class Ticket(BaseModel):
    id: str
    opened: str
    summary: str


class ActionResult(BaseModel):
    employee_id: str
    action: str
    changed: bool
    detail: str


# ── Servers ────────────────────────────────────────────────────────────


def identity_server(world: World) -> MCPServer:
    mcp = MCPServer(name="identity", instructions="Read-only identity directory for Kestrel Ridge Logistics.")

    @mcp.tool(name="account_status", annotations=READ_ONLY, description=(
        "Lock state, failed sign-ins, password age and expiry, MFA enrolment and lock, "
        "and mailbox usage for one employee."
    ))
    def account_status(employee: EmployeeRef) -> AccountStatus:
        a = world.account(employee)
        age = world.days_since(a.password_last_set)
        return AccountStatus(
            employee_id=a.employee_id, username=a.username, locked=a.locked,
            failed_logins=a.failed_logins, lockout_threshold=world.company.lockout_threshold,
            password_age_days=age, password_expired=age >= world.company.password_max_age_days,
            mfa_enrolled=a.mfa_enrolled, mfa_method=a.mfa_method, mfa_locked=a.mfa_locked,
            mailbox_used_gb=a.mailbox_used_gb, mailbox_quota_gb=a.mailbox_quota_gb,
        )

    return mcp


def devices_server(world: World) -> MCPServer:
    mcp = MCPServer(name="devices", instructions="Read-only device management for Kestrel Ridge Logistics.")

    @mcp.tool(name="device_health", annotations=READ_ONLY, description=(
        "OS and build, free disk space, CPU temperature, compliance, antivirus definition age "
        "and pending updates for an employee's device."
    ))
    def device_health(employee: EmployeeRef) -> DeviceHealth:
        d = world.device(employee)
        return DeviceHealth(
            device_id=d.id, os=d.os, os_build=d.os_build, disk_free_gb=d.disk_free_gb,
            disk_free_percent=round(100 * d.disk_free_gb / d.disk_total_gb, 1),
            cpu_temp_c=d.cpu_temp_c, compliant=d.compliant,
            antivirus_definitions_age_days=world.days_since(d.antivirus_definitions),
            pending_updates=d.pending_updates,
        )

    @mcp.tool(name="installed_software", annotations=READ_ONLY,
              description="The software installed on an employee's device.")
    def installed_software(employee: EmployeeRef) -> Software:
        d = world.device(employee)
        return Software(device_id=d.id, installed=d.software)

    return mcp


def vpn_server(world: World) -> MCPServer:
    mcp = MCPServer(name="vpn", instructions="Read-only VPN gateway for Kestrel Ridge Logistics.")

    @mcp.tool(name="vpn_certificate_status", annotations=READ_ONLY,
              description="When an employee's VPN client certificate expires, and whether it already has.")
    def vpn_certificate_status(employee: EmployeeRef) -> CertificateStatus:
        v = world.vpn(employee)
        left = (v.certificate_expires - world.company.as_of).days
        return CertificateStatus(employee_id=v.employee_id, expires=v.certificate_expires,
                                 days_left=left, expired=left < 0)

    @mcp.tool(name="vpn_last_session", annotations=READ_ONLY,
              description="The VPN client version and the error from an employee's last session, if any.")
    def vpn_last_session(employee: EmployeeRef) -> VpnSession:
        v = world.vpn(employee)
        return VpnSession(employee_id=v.employee_id, client_version=v.client_version, last_error=v.last_error)

    return mcp


def status_server(world: World) -> MCPServer:
    mcp = MCPServer(name="status", instructions="Read-only service status page for Kestrel Ridge Logistics.")

    @mcp.tool(name="service_status", annotations=READ_ONLY,
              description="Current status of company services: email, SSO, VPN, Wi-Fi, printing, software catalog.")
    def service_status() -> list[ServiceInfo]:
        return [ServiceInfo(name=s.name, status=s.status) for s in world.company.services]

    @mcp.tool(name="printer_status", annotations=READ_ONLY,
              description="Status and queue length of the printers, optionally for one site.")
    def printer_status(
        site: Annotated[Literal["Head office", "Warehouse"] | None, Field(description="Only this site.")] = None,
    ) -> list[PrinterInfo]:
        return [
            PrinterInfo(id=p.id, site=p.site, status=p.status, queue_length=p.queue_length)
            for p in world.company.printers
            if site is None or p.site == site
        ]

    return mcp


def tickets_server(world: World) -> MCPServer:
    mcp = MCPServer(name="tickets", instructions="Read-only ticket history for Kestrel Ridge Logistics.")

    @mcp.tool(name="recent_tickets", annotations=READ_ONLY,
              description="Recent helpdesk tickets opened by an employee, newest first.")
    def recent_tickets(employee: EmployeeRef) -> list[Ticket]:
        emp = world.employee(employee)
        mine = [t for t in world.company.tickets if t.get("employee_id") == emp.id]
        mine.sort(key=lambda t: t.get("opened", ""), reverse=True)
        return [Ticket(id=t["id"], opened=t.get("opened", ""), summary=t.get("summary", "")) for t in mine]

    return mcp


def actions_server(world: World) -> MCPServer:
    """Account changes. Never handed to diagnosis directly: the policy gate calls these."""
    mcp = MCPServer(name="actions", instructions=(
        "Account changes for Kestrel Ridge Logistics. Every call needs the helpdesk agent's approval."
    ))

    @mcp.tool(name="unlock_account", annotations=CHANGES_ACCOUNT,
              description="Unlock an employee's account and reset the failed sign-in count. Needs approval.")
    def unlock_account(employee: EmployeeRef) -> ActionResult:
        a = world.account(employee)
        changed = a.locked or a.failed_logins > 0
        world.replace_account(a.model_copy(update={"locked": False, "failed_logins": 0}))
        return ActionResult(employee_id=a.employee_id, action="unlock_account", changed=changed,
                            detail="Account unlocked." if changed else "Account was not locked.")

    @mcp.tool(name="reset_mfa", annotations=CHANGES_ACCOUNT,
              description="Clear an employee's MFA lock so they can enrol or sign in again. Needs approval.")
    def reset_mfa(employee: EmployeeRef) -> ActionResult:
        a = world.account(employee)
        changed = a.mfa_locked or not a.mfa_enrolled
        world.replace_account(a.model_copy(update={"mfa_locked": False, "mfa_enrolled": True}))
        return ActionResult(employee_id=a.employee_id, action="reset_mfa", changed=changed,
                            detail="MFA reset." if changed else "MFA was already working.")

    return mcp


SERVERS: dict[str, Callable[[World], MCPServer]] = {
    "identity": identity_server,
    "devices": devices_server,
    "vpn": vpn_server,
    "status": status_server,
    "tickets": tickets_server,
    "actions": actions_server,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve one mock IT system over MCP.")
    parser.add_argument("server", choices=sorted(SERVERS))
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP, not stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    mcp = SERVERS[args.server](World.load())
    if args.http:
        mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
