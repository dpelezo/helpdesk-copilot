"""A fictional company for the mock IT systems: people, accounts, devices, VPN, printers.

Everything is generated from a seed, so the same seed gives the same company on
every run. Names are built from invented syllables and the domain is under
``.example``, so nothing here belongs to a real person or organisation.

The generated company is healthy: every check the copilot can run comes back
clean. The fault injector (#7) copies it and plants one root cause per ticket.

    python -m app.mock.company                 # writes fixtures/company.json
    python -m app.mock.company --seed 7 --out /tmp/c.json
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_SEED = 20261201
# Fixed "today" for the mock world, so generated dates never depend on the clock.
AS_OF = date(2026, 12, 1)
DOMAIN = "kestrelridge.example"
COMPANY_NAME = "Kestrel Ridge Logistics"
FIXTURE = Path("fixtures/company.json")

PASSWORD_MAX_AGE_DAYS = 90
LOCKOUT_THRESHOLD = 5

# Head count per department; 50 in total.
DEPARTMENTS: dict[str, int] = {
    "Operations": 14,
    "Sales": 10,
    "Engineering": 9,
    "Finance": 6,
    "Customer Support": 6,
    "People": 3,
    "IT": 2,
}
SITES = ("Head office", "Warehouse", "Remote")
SOFTWARE = ("Office suite", "Mail client", "VPN client", "Browser", "Antivirus", "Chat")
EXTRA_SOFTWARE = {
    "Engineering": ("IDE", "Container runtime"),
    "Finance": ("Accounting suite",),
    "Sales": ("CRM client",),
    "Operations": ("Route planner",),
}

_ONSETS = ("b", "br", "c", "d", "f", "g", "h", "j", "k", "l", "m", "n", "p", "r", "s", "t", "v", "w", "z")
_VOWELS = ("a", "e", "i", "o", "u", "ae", "ei", "ou")
_CODAS = ("", "n", "r", "l", "s", "th", "m", "nd")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Employee(_Model):
    id: str
    name: str
    email: str
    department: str
    title: str
    site: Literal["Head office", "Warehouse", "Remote"]
    manager_id: str | None


class Account(_Model):
    employee_id: str
    username: str
    locked: bool = False
    failed_logins: int = 0
    password_last_set: date
    mfa_enrolled: bool = True
    mfa_method: Literal["authenticator", "hardware_key", "sms"]
    mfa_locked: bool = False
    mailbox_used_gb: float
    mailbox_quota_gb: float = 50.0


class Device(_Model):
    id: str
    owner_id: str
    kind: Literal["laptop", "desktop"]
    os: Literal["Windows 11", "macOS 15"]
    os_build: str
    disk_total_gb: int
    disk_free_gb: int
    cpu_temp_c: int
    compliant: bool = True
    antivirus_definitions: date
    pending_updates: int = 0
    software: list[str]


class VpnProfile(_Model):
    employee_id: str
    certificate_expires: date
    client_version: str
    last_error: str | None = None


class Printer(_Model):
    id: str
    site: Literal["Head office", "Warehouse"]
    status: Literal["online", "offline", "paper_jam", "toner_low"] = "online"
    queue_length: int = 0


class Service(_Model):
    name: str
    status: Literal["operational", "degraded", "outage"] = "operational"


class Company(_Model):
    name: str
    domain: str
    as_of: date
    seed: int
    password_max_age_days: int = PASSWORD_MAX_AGE_DAYS
    lockout_threshold: int = LOCKOUT_THRESHOLD
    employees: list[Employee]
    accounts: list[Account]
    devices: list[Device]
    vpn: list[VpnProfile]
    printers: list[Printer]
    services: list[Service]
    tickets: list[dict[str, str]] = Field(default_factory=list)


def _word(rng: random.Random, syllables: int) -> str:
    parts = [rng.choice(_ONSETS) + rng.choice(_VOWELS) for _ in range(syllables)]
    return ("".join(parts) + rng.choice(_CODAS)).capitalize()


def _unique_names(rng: random.Random, count: int) -> list[str]:
    names: list[str] = []
    while len(names) < count:
        name = f"{_word(rng, rng.choice((1, 2)))} {_word(rng, 2)}"
        if name not in names:
            names.append(name)
    return names


def _username(name: str, taken: set[str]) -> str:
    first, last = name.lower().split()
    base = f"{first[0]}{last}"
    username, n = base, 2
    while username in taken:
        username, n = f"{base}{n}", n + 1
    taken.add(username)
    return username


def generate(seed: int = DEFAULT_SEED) -> Company:
    """Build a healthy company from ``seed``. Same seed, same company."""
    rng = random.Random(seed)
    names = _unique_names(rng, sum(DEPARTMENTS.values()))
    employees: list[Employee] = []
    accounts: list[Account] = []
    devices: list[Device] = []
    vpn: list[VpnProfile] = []
    taken: set[str] = set()

    number = 0
    for department, size in DEPARTMENTS.items():
        lead_id: str | None = None
        for position in range(size):
            number += 1
            emp_id = f"E{number:03d}"
            name = names[number - 1]
            username = _username(name, taken)
            is_lead = position == 0
            site = "Remote" if department in ("Engineering", "Sales") and rng.random() < 0.5 else (
                "Warehouse" if department == "Operations" and rng.random() < 0.6 else "Head office"
            )
            employees.append(Employee(
                id=emp_id,
                name=name,
                email=f"{username}@{DOMAIN}",
                department=department,
                title=f"{department} lead" if is_lead else f"{department} specialist",
                site=site,
                manager_id=None if is_lead else lead_id,
            ))
            if is_lead:
                lead_id = emp_id

            accounts.append(Account(
                employee_id=emp_id,
                username=username,
                password_last_set=AS_OF - timedelta(days=rng.randint(1, PASSWORD_MAX_AGE_DAYS - 15)),
                mfa_method=rng.choice(("authenticator", "authenticator", "hardware_key", "sms")),
                mailbox_used_gb=round(rng.uniform(2, 30), 1),
            ))

            mac = department in ("Engineering", "People") or rng.random() < 0.15
            total = rng.choice((256, 512, 1024))
            devices.append(Device(
                id=f"D{number:03d}",
                owner_id=emp_id,
                kind="desktop" if department == "Operations" and site == "Warehouse" else "laptop",
                os="macOS 15" if mac else "Windows 11",
                os_build=f"15.{rng.randint(1, 3)}" if mac else f"26100.{rng.randint(2000, 2600)}",
                disk_total_gb=total,
                disk_free_gb=int(total * rng.uniform(0.25, 0.7)),
                cpu_temp_c=rng.randint(42, 68),
                antivirus_definitions=AS_OF - timedelta(days=rng.randint(0, 1)),
                software=sorted({*SOFTWARE, *EXTRA_SOFTWARE.get(department, ())}),
            ))

            vpn.append(VpnProfile(
                employee_id=emp_id,
                certificate_expires=AS_OF + timedelta(days=rng.randint(60, 365)),
                client_version=rng.choice(("5.2.1", "5.2.3")),
            ))

    return Company(
        name=COMPANY_NAME,
        domain=DOMAIN,
        as_of=AS_OF,
        seed=seed,
        employees=employees,
        accounts=accounts,
        devices=devices,
        vpn=vpn,
        printers=[
            Printer(id="PRN-HQ-1", site="Head office"),
            Printer(id="PRN-HQ-2", site="Head office"),
            Printer(id="PRN-WH-1", site="Warehouse"),
        ],
        services=[Service(name=n) for n in ("email", "sso", "vpn", "wifi", "printing", "software-catalog")],
    )


def write(company: Company, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(company.model_dump_json(indent=2) + "\n")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the mock company fixture.")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=FIXTURE)
    args = parser.parse_args(argv)
    company = generate(args.seed)
    write(company, args.out)
    print(f"{company.name}: {len(company.employees)} employees -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
