"""Unit tests for the mock IT systems, through an in-process MCP client."""

from datetime import timedelta

import pytest
from mcp import Client

from app.mock import company as c
from app.mock import systems as s

EXPECTED_TOOLS = {
    "identity": {"account_status"},
    "devices": {"device_health", "installed_software"},
    "vpn": {"vpn_certificate_status", "vpn_last_session"},
    "status": {"service_status", "printer_status"},
    "tickets": {"recent_tickets"},
    "actions": {"unlock_account", "reset_mfa"},
}


@pytest.fixture
def world() -> s.World:
    return s.World(c.generate())


def planted(world: s.World, **sections) -> s.World:
    """A copy of the world with whole sections replaced, as the fault injector will do."""
    return s.World(world.company.model_copy(update=sections))


async def call(world: s.World, server: str, tool: str, args: dict | None = None):
    async with Client(s.SERVERS[server](world)) as client:
        return await client.call_tool(tool, args or {})


async def ok(world: s.World, server: str, tool: str, args: dict | None = None):
    result = await call(world, server, tool, args)
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.parametrize("server", sorted(EXPECTED_TOOLS))
async def test_each_server_lists_its_tools(world, server):
    async with Client(s.SERVERS[server](world)) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS[server]


async def test_only_the_actions_server_can_change_anything(world):
    for server in EXPECTED_TOOLS:
        async with Client(s.SERVERS[server](world)) as client:
            for tool in (await client.list_tools()).tools:
                assert tool.annotations is not None
                changes = tool.name in s.ACCOUNT_CHANGING_TOOLS
                assert tool.annotations.read_only_hint is not changes
                assert (server == "actions") is changes


@pytest.mark.parametrize("ref", ["E001", "e001", "jhicel", "jhicel@kestrelridge.example"])
async def test_an_employee_can_be_named_by_id_username_or_email(world, ref):
    assert (await ok(world, "identity", "account_status", {"employee": ref}))["employee_id"] == "E001"


async def test_an_unknown_employee_is_a_clear_tool_error(world):
    result = await call(world, "identity", "account_status", {"employee": "nobody"})
    assert result.is_error
    assert "No employee matches 'nobody'" in result.content[0].text


async def test_the_baseline_world_reports_healthy(world):
    account = await ok(world, "identity", "account_status", {"employee": "E010"})
    assert not account["locked"] and not account["password_expired"] and not account["mfa_locked"]
    device = await ok(world, "devices", "device_health", {"employee": "E010"})
    assert device["compliant"] and device["pending_updates"] == 0 and device["disk_free_percent"] >= 25
    cert = await ok(world, "vpn", "vpn_certificate_status", {"employee": "E010"})
    assert not cert["expired"] and cert["days_left"] >= 60
    services = await ok(world, "status", "service_status")
    assert {x["status"] for x in services["result"]} == {"operational"}
    assert (await ok(world, "tickets", "recent_tickets", {"employee": "E010"}))["result"] == []


async def test_a_planted_lockout_and_expired_password_show_up(world):
    acc = world.company.accounts[0]
    bad = acc.model_copy(update={"locked": True, "failed_logins": 6,
                                 "password_last_set": world.company.as_of - timedelta(days=120)})
    w = planted(world, accounts=[bad, *world.company.accounts[1:]])
    got = await ok(w, "identity", "account_status", {"employee": "E001"})
    assert got["locked"] and got["failed_logins"] == 6
    assert got["password_age_days"] == 120 and got["password_expired"]


async def test_planted_device_and_vpn_faults_show_up(world):
    d = world.company.devices[2].model_copy(update={"disk_free_gb": 5, "cpu_temp_c": 94, "pending_updates": 3})
    v = world.company.vpn[2].model_copy(update={
        "certificate_expires": world.company.as_of - timedelta(days=2), "last_error": "TLS handshake failed",
    })
    w = planted(world, devices=[*world.company.devices[:2], d, *world.company.devices[3:]],
                vpn=[*world.company.vpn[:2], v, *world.company.vpn[3:]])
    health = await ok(w, "devices", "device_health", {"employee": "E003"})
    assert health["disk_free_gb"] == 5 and health["cpu_temp_c"] == 94 and health["pending_updates"] == 3
    cert = await ok(w, "vpn", "vpn_certificate_status", {"employee": "E003"})
    assert cert["expired"] and cert["days_left"] == -2
    session = await ok(w, "vpn", "vpn_last_session", {"employee": "E003"})
    assert session["last_error"] == "TLS handshake failed"


async def test_outages_printers_and_site_filter(world):
    services = [x.model_copy(update={"status": "outage"}) if x.name == "email" else x
                for x in world.company.services]
    printers = [p.model_copy(update={"status": "paper_jam", "queue_length": 4}) if p.id == "PRN-WH-1" else p
                for p in world.company.printers]
    w = planted(world, services=services, printers=printers)
    status = {x["name"]: x["status"] for x in (await ok(w, "status", "service_status"))["result"]}
    assert status["email"] == "outage" and status["vpn"] == "operational"
    warehouse = (await ok(w, "status", "printer_status", {"site": "Warehouse"}))["result"]
    assert warehouse == [{"id": "PRN-WH-1", "site": "Warehouse", "status": "paper_jam", "queue_length": 4}]


async def test_recent_tickets_are_the_employees_own_newest_first(world):
    w = planted(world, tickets=[
        {"id": "T-1", "employee_id": "E004", "opened": "2026-11-20", "summary": "VPN slow"},
        {"id": "T-2", "employee_id": "E005", "opened": "2026-11-25", "summary": "Printer"},
        {"id": "T-3", "employee_id": "E004", "opened": "2026-11-28", "summary": "VPN drops"},
    ])
    got = (await ok(w, "tickets", "recent_tickets", {"employee": "E004"}))["result"]
    assert [t["id"] for t in got] == ["T-3", "T-1"]


async def test_read_only_servers_never_change_the_world(world):
    before = world.company.model_dump_json()
    for server, tools in EXPECTED_TOOLS.items():
        if server == "actions":
            continue
        for tool in tools:
            args = {} if tool in ("service_status", "printer_status") else {"employee": "E007"}
            await ok(world, server, tool, args)
    assert world.company.model_dump_json() == before


async def test_unlock_changes_the_shared_world_and_identity_sees_it(world):
    acc = world.company.accounts[5].model_copy(update={"locked": True, "failed_logins": 5})
    w = planted(world, accounts=[*world.company.accounts[:5], acc, *world.company.accounts[6:]])
    result = await ok(w, "actions", "unlock_account", {"employee": "E006"})
    assert result["changed"] and result["detail"] == "Account unlocked."
    after = await ok(w, "identity", "account_status", {"employee": "E006"})
    assert not after["locked"] and after["failed_logins"] == 0


async def test_reset_mfa_clears_the_lock_and_reports_no_op_when_healthy(world):
    acc = world.company.accounts[8].model_copy(update={"mfa_locked": True})
    w = planted(world, accounts=[*world.company.accounts[:8], acc, *world.company.accounts[9:]])
    assert (await ok(w, "actions", "reset_mfa", {"employee": "E009"}))["changed"]
    assert not (await ok(w, "identity", "account_status", {"employee": "E009"}))["mfa_locked"]
    again = await ok(w, "actions", "reset_mfa", {"employee": "E009"})
    assert not again["changed"] and again["detail"] == "MFA was already working."


def test_world_loads_the_fixture_or_the_mock_company_env(tmp_path, monkeypatch):
    assert s.World.load().company == c.generate()
    other = c.write(c.generate(7), tmp_path / "planted.json")
    monkeypatch.setenv("MOCK_COMPANY", str(other))
    assert s.World.load().company.seed == 7
