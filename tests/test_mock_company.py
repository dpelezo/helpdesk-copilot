"""Unit tests for the mock company generator."""

from collections import Counter
from datetime import timedelta

import pytest

from app.mock import company as c


@pytest.fixture(scope="module")
def co() -> c.Company:
    return c.generate()


def test_same_seed_gives_the_same_company():
    assert c.generate(42).model_dump_json() == c.generate(42).model_dump_json()


def test_a_different_seed_gives_a_different_company():
    assert c.generate(1).employees != c.generate(2).employees


def test_the_committed_fixture_matches_the_generator():
    """Regenerate with `make company` if this fails after a generator change."""
    assert c.FIXTURE.read_text() == c.generate().model_dump_json(indent=2) + "\n"


def test_fifty_employees_with_unique_ids_emails_and_usernames(co):
    assert len(co.employees) == 50
    for values in (
        [e.id for e in co.employees],
        [e.email for e in co.employees],
        [a.username for a in co.accounts],
        [e.name for e in co.employees],
    ):
        assert len(set(values)) == len(values)


def test_department_sizes_and_one_lead_each(co):
    assert Counter(e.department for e in co.employees) == c.DEPARTMENTS
    leads = [e for e in co.employees if e.manager_id is None]
    assert sorted(e.department for e in leads) == sorted(c.DEPARTMENTS)


def test_every_manager_exists_in_the_same_department(co):
    by_id = {e.id: e for e in co.employees}
    for e in co.employees:
        if e.manager_id:
            assert by_id[e.manager_id].department == e.department


def test_each_employee_has_one_account_device_and_vpn_profile(co):
    ids = [e.id for e in co.employees]
    assert [a.employee_id for a in co.accounts] == ids
    assert [d.owner_id for d in co.devices] == ids
    assert [v.employee_id for v in co.vpn] == ids


def test_no_real_domains(co):
    assert co.domain.endswith(".example")
    assert all(e.email.endswith("@" + c.DOMAIN) for e in co.employees)


def test_accounts_start_healthy(co):
    for a in co.accounts:
        assert not a.locked and a.failed_logins == 0
        assert a.mfa_enrolled and not a.mfa_locked
        assert (co.as_of - a.password_last_set).days < co.password_max_age_days
        assert a.mailbox_used_gb < a.mailbox_quota_gb


def test_devices_start_healthy(co):
    for d in co.devices:
        assert d.compliant and d.pending_updates == 0
        assert d.disk_free_gb >= d.disk_total_gb * 0.25
        assert d.cpu_temp_c < 70
        assert co.as_of - d.antivirus_definitions <= timedelta(days=1)
        assert {"VPN client", "Antivirus", "Mail client"} <= set(d.software)


def test_vpn_printers_and_services_start_healthy(co):
    assert all(v.certificate_expires - co.as_of >= timedelta(days=60) for v in co.vpn)
    assert all(v.last_error is None for v in co.vpn)
    assert all(p.status == "online" and p.queue_length == 0 for p in co.printers)
    assert all(s.status == "operational" for s in co.services)
    assert co.tickets == []


def test_both_operating_systems_are_present(co):
    assert {d.os for d in co.devices} == {"Windows 11", "macOS 15"}


def test_main_writes_the_fixture(tmp_path, capsys):
    out = tmp_path / "company.json"
    assert c.main(["--seed", "7", "--out", str(out)]) == 0
    assert c.Company.model_validate_json(out.read_text()) == c.generate(7)
    assert "50 employees" in capsys.readouterr().out
