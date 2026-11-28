"""Possible causes for each in-scope issue, and how to plant one in the mock company.

Each ``Cause`` changes a copy of the healthy company (``app.mock.company``) so that
one check, or a few, comes back wrong for one employee. The ticket generator picks
an employee and a cause, plants it, and stores the cause as the ticket's answer key.

Some causes are deliberate misdirection: the employee describes one problem (VPN
will not connect) but the cause sits in another system (their account is locked).

These are the ground truth for the eval. Change them only in their own pull request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from app.mock.company import Company, Employee

# The 13 canonical issues in scope (issue #7). Ids match reports/seed_profile.md;
# the last two are written by hand because the seed has no identity issues.
ISSUES: dict[str, str] = {
    "network--vpn-not-connecting": "VPN not connecting",
    "network--wi-fi-not-connecting": "Wi-Fi not connecting",
    "microsoft-office--outlook-not-sending-emails": "Outlook not sending emails",
    "system--disk-space-running-out": "Disk space running out",
    "system--system-running-slow": "System running slow",
    "hardware--laptop-overheating": "Laptop overheating",
    "software--program-won-t-install": "Program won't install",
    "software--application-not-updating": "Application not updating",
    "security--antivirus-not-updating": "Antivirus not updating",
    "security--phishing-email-detected": "Phishing email detected",
    "hardware--printer-not-responding": "Printer not responding",
    "identity--account-locked": "Account locked",
    "identity--mfa-lockout": "MFA lockout",
}

Plant = Callable[[Company, str], Company]


@dataclass(frozen=True)
class Cause:
    id: str
    issue: str
    summary: str
    plant: Plant
    # Checks (mock-system tools) whose answer for this employee differs from a healthy world.
    reveals: tuple[str, ...]
    fix: str
    # True when the fix uses an account-changing tool (ADR 0001: the agent approves it).
    requires_approval: bool = False
    eligible: Callable[[Employee], bool] = field(default=lambda e: True)


# ── Helpers that change one employee's records in a copy of the company ──


def _account(company: Company, emp: str, **changes: object) -> Company:
    rows = [a.model_copy(update=changes) if a.employee_id == emp else a for a in company.accounts]
    return company.model_copy(update={"accounts": rows})


def _device(company: Company, emp: str, **changes: object) -> Company:
    rows = [d.model_copy(update=changes) if d.owner_id == emp else d for d in company.devices]
    return company.model_copy(update={"devices": rows})


def _low_disk(company: Company, emp: str, percent: float) -> Company:
    device = next(d for d in company.devices if d.owner_id == emp)
    return _device(company, emp, disk_free_gb=max(1, int(device.disk_total_gb * percent)))


def _vpn(company: Company, emp: str, **changes: object) -> Company:
    rows = [v.model_copy(update=changes) if v.employee_id == emp else v for v in company.vpn]
    return company.model_copy(update={"vpn": rows})


def _service(company: Company, name: str, status: str) -> Company:
    rows = [s.model_copy(update={"status": status}) if s.name == name else s for s in company.services]
    return company.model_copy(update={"services": rows})


def _printer_for(company: Company, emp: str) -> str:
    site = next(e.site for e in company.employees if e.id == emp)
    return "PRN-WH-1" if site == "Warehouse" else "PRN-HQ-1"


def _printer(company: Company, emp: str, **changes: object) -> Company:
    target = _printer_for(company, emp)
    rows = [p.model_copy(update=changes) if p.id == target else p for p in company.printers]
    return company.model_copy(update={"printers": rows})


def _ticket(company: Company, emp: str, ticket_id: str, days_ago: int, summary: str) -> Company:
    opened = (company.as_of - timedelta(days=days_ago)).isoformat()
    row = {"id": ticket_id, "employee_id": emp, "opened": opened, "summary": summary}
    return company.model_copy(update={"tickets": [*company.tickets, row]})


def _expired_password(company: Company, emp: str) -> Company:
    return _account(company, emp, password_last_set=company.as_of - timedelta(days=company.password_max_age_days + 6))


def _locked(company: Company, emp: str, failed: int = 6) -> Company:
    return _account(company, emp, locked=True, failed_logins=failed)


def _on_site(e: Employee) -> bool:
    return e.site != "Remote"


def _remote(e: Employee) -> bool:
    return e.site == "Remote"


_OVERHEAT = {"cpu_temp_c": 96}

CAUSES: tuple[Cause, ...] = (
    # VPN not connecting
    Cause("vpn-certificate-expired", "network--vpn-not-connecting", "The VPN client certificate has expired.",
          lambda c, e: _vpn(c, e, certificate_expires=c.as_of - timedelta(days=3),
                            last_error="Certificate validation failed"),
          ("vpn_certificate_status", "vpn_last_session"), "Issue a new VPN client certificate."),
    Cause("vpn-client-outdated", "network--vpn-not-connecting", "The VPN client is too old for the gateway.",
          lambda c, e: _vpn(c, e, client_version="4.9.0", last_error="Unsupported client version"),
          ("vpn_last_session",), "Update the VPN client from the software catalog."),
    Cause("vpn-gateway-outage", "network--vpn-not-connecting", "The VPN service has an outage.",
          lambda c, e: _service(c, "vpn", "outage"),
          ("service_status",), "Tell the employee about the outage and link the status page."),
    Cause("vpn-account-locked", "network--vpn-not-connecting", "The employee's account is locked, so VPN sign-in fails.",
          lambda c, e: _locked(c, e), ("account_status",), "Unlock the account.", requires_approval=True),
    # Wi-Fi not connecting
    Cause("wifi-service-degraded", "network--wi-fi-not-connecting", "Office Wi-Fi is degraded.",
          lambda c, e: _service(c, "wifi", "degraded"), ("service_status",),
          "Share the incident status; use a wired connection meanwhile.", eligible=_on_site),
    Cause("wifi-password-expired", "network--wi-fi-not-connecting",
          "The password has expired, so network sign-in is rejected.",
          _expired_password, ("account_status",), "Guide the employee through a self-service password change.",
          eligible=_on_site),
    Cause("wifi-device-noncompliant", "network--wi-fi-not-connecting",
          "The device is out of compliance, so network access control blocks it.",
          lambda c, e: _device(c, e, compliant=False, pending_updates=6), ("device_health",),
          "Install the pending updates to restore compliance.", eligible=_on_site),
    # Outlook not sending
    Cause("mailbox-full", "microsoft-office--outlook-not-sending-emails", "The mailbox is over its quota.",
          lambda c, e: _account(c, e, mailbox_used_gb=50.0), ("account_status",),
          "Archive or delete old mail to get under the quota."),
    Cause("email-service-outage", "microsoft-office--outlook-not-sending-emails", "The email service has an outage.",
          lambda c, e: _service(c, "email", "outage"), ("service_status",),
          "Tell the employee about the outage and link the status page."),
    Cause("outlook-password-expired", "microsoft-office--outlook-not-sending-emails",
          "The password has expired, so the mail client cannot authenticate.",
          _expired_password, ("account_status",), "Guide the employee through a self-service password change."),
    # Disk space running out
    Cause("disk-nearly-full", "system--disk-space-running-out", "The disk is almost full of user files.",
          lambda c, e: _low_disk(c, e, 0.03), ("device_health",), "Clear downloads and move files to cloud storage."),
    Cause("update-cache-filling-disk", "system--disk-space-running-out",
          "Pending updates have filled the disk with their download cache.",
          lambda c, e: _device(_low_disk(c, e, 0.06), e, pending_updates=14), ("device_health",),
          "Install the pending updates, then clear the update cache."),
    # System running slow
    Cause("slow-low-disk", "system--system-running-slow", "The disk is nearly full, so the system swaps.",
          lambda c, e: _low_disk(c, e, 0.04), ("device_health",), "Free disk space."),
    Cause("slow-thermal-throttling", "system--system-running-slow", "The CPU is throttling because it runs hot.",
          lambda c, e: _device(c, e, **_OVERHEAT), ("device_health",), "Clean the vents and check the fan; book hardware service."),
    Cause("slow-pending-updates", "system--system-running-slow", "Many updates are pending and installing in the background.",
          lambda c, e: _device(c, e, pending_updates=17), ("device_health",), "Install the updates and restart."),
    # Laptop overheating
    Cause("overheating-fan", "hardware--laptop-overheating", "The CPU temperature is too high: likely a blocked or failing fan.",
          lambda c, e: _device(c, e, cpu_temp_c=97), ("device_health",), "Book hardware service for the fan."),
    Cause("overheating-pending-firmware", "hardware--laptop-overheating",
          "Pending firmware and OS updates include the thermal fix.",
          lambda c, e: _device(c, e, cpu_temp_c=88, pending_updates=4), ("device_health",),
          "Install the pending updates and restart."),
    # Program won't install
    Cause("install-catalog-outage", "software--program-won-t-install", "The software catalog has an outage.",
          lambda c, e: _service(c, "software-catalog", "outage"), ("service_status",),
          "Tell the employee about the outage and retry later."),
    Cause("install-disk-full", "software--program-won-t-install", "There is not enough free disk space to install.",
          lambda c, e: _low_disk(c, e, 0.02), ("device_health",), "Free disk space, then retry the install."),
    Cause("install-device-noncompliant", "software--program-won-t-install",
          "The device is out of compliance, so installs are blocked.",
          lambda c, e: _device(c, e, compliant=False, pending_updates=5), ("device_health",),
          "Install the pending updates to restore compliance."),
    # Application not updating
    Cause("app-update-catalog-degraded", "software--application-not-updating", "The software catalog is degraded.",
          lambda c, e: _service(c, "software-catalog", "degraded"), ("service_status",),
          "Share the incident status and retry later."),
    Cause("app-update-low-disk", "software--application-not-updating", "There is not enough free disk space for the update.",
          lambda c, e: _low_disk(c, e, 0.03), ("device_health",), "Free disk space, then retry the update."),
    Cause("app-update-waits-for-os", "software--application-not-updating", "The app update waits for pending OS updates.",
          lambda c, e: _device(c, e, pending_updates=9), ("device_health",), "Install the OS updates first."),
    # Antivirus not updating
    Cause("av-definitions-stale", "security--antivirus-not-updating",
          "Antivirus definitions are days old and the device is out of compliance.",
          lambda c, e: _device(c, e, antivirus_definitions=c.as_of - timedelta(days=9), compliant=False),
          ("device_health",), "Force a definitions update and check the antivirus service."),
    Cause("av-low-disk", "security--antivirus-not-updating", "There is no disk space for new definitions.",
          lambda c, e: _device(_low_disk(c, e, 0.01), e, antivirus_definitions=c.as_of - timedelta(days=4)),
          ("device_health",), "Free disk space, then update definitions."),
    Cause("av-remote-vpn-expired", "security--antivirus-not-updating",
          "A remote device cannot reach the update server because its VPN certificate expired.",
          lambda c, e: _device(_vpn(c, e, certificate_expires=c.as_of - timedelta(days=6)), e,
                               antivirus_definitions=c.as_of - timedelta(days=6)),
          ("vpn_certificate_status", "device_health"), "Issue a new VPN certificate; definitions then update.",
          eligible=_remote),
    # Phishing email detected
    Cause("phishing-suspicious-sign-ins", "security--phishing-email-detected",
          "Someone is trying the employee's password after a phishing email.",
          lambda c, e: _ticket(_account(c, e, failed_logins=4), e, "T-0901", 2, "Odd password reset email"),
          ("account_status", "recent_tickets"),
          "Report to security and have the employee change their password now."),
    Cause("phishing-account-locked", "security--phishing-email-detected",
          "Repeated sign-in attempts after a phishing email locked the account.",
          lambda c, e: _ticket(_locked(c, e, failed=9), e, "T-0902", 1, "Clicked a link in a fake invoice"),
          ("account_status", "recent_tickets"),
          "Report to security, then unlock the account after a password change.", requires_approval=True),
    # Printer not responding
    Cause("printer-offline", "hardware--printer-not-responding", "The site printer is offline.",
          lambda c, e: _printer(c, e, status="offline", queue_length=6), ("printer_status",),
          "Power-cycle the printer and check its network cable.", eligible=_on_site),
    Cause("printer-paper-jam", "hardware--printer-not-responding", "The site printer has a paper jam.",
          lambda c, e: _printer(c, e, status="paper_jam", queue_length=3), ("printer_status",),
          "Clear the jam, then release the queue.", eligible=_on_site),
    Cause("printing-service-outage", "hardware--printer-not-responding", "The print service has an outage.",
          lambda c, e: _service(c, "printing", "outage"), ("service_status",),
          "Tell the employee about the outage.", eligible=_on_site),
    # Account locked
    Cause("locked-failed-sign-ins", "identity--account-locked", "Too many failed sign-ins locked the account.",
          lambda c, e: _locked(c, e), ("account_status",), "Unlock the account.", requires_approval=True),
    Cause("locked-expired-password", "identity--account-locked",
          "The password expired and retries with the old one locked the account.",
          lambda c, e: _locked(_expired_password(c, e), e, failed=5), ("account_status",),
          "Unlock the account, then have the employee set a new password.", requires_approval=True),
    # MFA lockout
    Cause("mfa-locked", "identity--mfa-lockout", "MFA is locked after too many failed codes.",
          lambda c, e: _account(c, e, mfa_locked=True), ("account_status",), "Reset MFA.", requires_approval=True),
    Cause("mfa-not-enrolled", "identity--mfa-lockout", "MFA enrolment was lost (for example a new phone).",
          lambda c, e: _account(c, e, mfa_enrolled=False), ("account_status",), "Reset MFA so they can enrol again.",
          requires_approval=True),
    Cause("mfa-sso-outage", "identity--mfa-lockout", "The single sign-on service has an outage.",
          lambda c, e: _service(c, "sso", "outage"), ("service_status",),
          "Tell the employee about the outage; do not reset MFA."),
)

# An escalation ticket plants nothing: every check is healthy, so the copilot should
# escalate with the evidence it gathered instead of guessing a cause.
ESCALATE = "escalate"

BY_ID: dict[str, Cause] = {c.id: c for c in CAUSES}


def causes_for(issue: str) -> list[Cause]:
    return [c for c in CAUSES if c.issue == issue]


def plant(company: Company, cause_id: str, employee_id: str) -> Company:
    """A copy of ``company`` with ``cause_id`` planted for ``employee_id``."""
    if cause_id == ESCALATE:
        return company
    return BY_ID[cause_id].plant(company, employee_id)


Outcome = Literal["fix", "escalate"]
