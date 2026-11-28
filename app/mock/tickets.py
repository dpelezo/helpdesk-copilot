"""Generate the eval tickets: employee messages with a planted cause and its answer key.

Each ticket names an employee, an issue and a cause from ``app.mock.faults``. The
world for a ticket is rebuilt on demand with ``world_for(ticket)``: the healthy
company with that one cause planted. Messages come from hand-written templates,
so generation is deterministic and needs no model.

Tickets are split by issue (ADR 0005, issue #7): the tuning set covers three
issues and may be used while building; the held-out set covers the other ten
and is never used for tuning.

    python -m app.mock.tickets          # writes evals/tickets/{tuning,heldout}.jsonl

These files are eval ground truth. Change them only in their own pull request.
"""

from __future__ import annotations

import argparse
import random
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.mock import faults
from app.mock.company import Company, Employee, generate

SEED = 7_100
OUT_DIR = Path("evals/tickets")

TUNING_ISSUES = (
    "hardware--printer-not-responding",
    "system--disk-space-running-out",
    "software--application-not-updating",
)
HELDOUT_ISSUES = tuple(i for i in faults.ISSUES if i not in TUNING_ISSUES)
# (tickets with a planted cause, escalation tickets) per split.
SIZES: dict[str, tuple[int, int]] = {"tuning": (18, 2), "heldout": (92, 8)}

Style = Literal["terse", "detailed", "urgent"]

# Symptom-only messages per issue. They never name the cause.
MESSAGES: dict[str, list[tuple[str, str]]] = {
    "network--vpn-not-connecting": [
        ("VPN won't connect", "The VPN keeps failing when I try to connect."),
        ("Can't get on the VPN", "I can't reach anything internal because the VPN won't connect."),
        ("VPN error", "The VPN client shows an error and gives up after a few seconds."),
    ],
    "network--wi-fi-not-connecting": [
        ("Wi-Fi not working", "My laptop won't join the office Wi-Fi."),
        ("No wireless", "Wi-Fi says it can't connect to the network."),
        ("Wi-Fi keeps failing", "I pick the office network and it never finishes connecting."),
    ],
    "microsoft-office--outlook-not-sending-emails": [
        ("Emails stuck in outbox", "My emails sit in the outbox and never send."),
        ("Can't send mail", "Outlook won't send anything. Receiving seems odd too."),
        ("Outlook not sending", "Every email I send comes back or just stays there."),
    ],
    "system--disk-space-running-out": [
        ("Disk full warning", "I keep getting a warning that my disk is almost full."),
        ("No space left", "I can't save files because there's no space left."),
        ("Storage almost full", "My laptop says storage is nearly full and I don't know why."),
    ],
    "system--system-running-slow": [
        ("Laptop very slow", "Everything on my laptop is really slow today."),
        ("Computer crawling", "Apps take ages to open and the whole machine lags."),
        ("Slow PC", "My computer has become so slow it's hard to work."),
    ],
    "hardware--laptop-overheating": [
        ("Laptop running hot", "My laptop gets very hot and the fan is loud."),
        ("Overheating", "The laptop is too hot to keep on my lap."),
        ("Hot laptop", "My laptop feels hot even when I'm only reading email."),
    ],
    "software--program-won-t-install": [
        ("Install fails", "I'm trying to install an app from the catalog and it fails."),
        ("Can't install software", "The installer stops partway and says it couldn't finish."),
        ("Software won't install", "I need a new program for work but it won't install."),
    ],
    "software--application-not-updating": [
        ("App won't update", "One of my apps says an update is available but it never installs."),
        ("Update stuck", "An app update keeps failing and asks me to retry."),
        ("Can't update app", "I can't get the latest version of an app I use every day."),
    ],
    "security--antivirus-not-updating": [
        ("Antivirus out of date", "My antivirus says its definitions are out of date."),
        ("Security warning", "I got a warning that my antivirus isn't up to date."),
        ("Antivirus not updating", "The antivirus update fails every time it runs."),
    ],
    "security--phishing-email-detected": [
        ("Suspicious email", "I think I got a phishing email and I may have clicked the link."),
        ("Possible phishing", "I opened a strange email asking me to confirm my password."),
        ("Clicked a bad link?", "I clicked a link in an email that now looks fake. What should I do?"),
    ],
    "hardware--printer-not-responding": [
        ("Printer not printing", "I send things to the printer and nothing comes out."),
        ("Printer not responding", "The printer doesn't respond to anything I send."),
        ("Can't print", "My print jobs just sit there and never print."),
    ],
    "identity--account-locked": [
        ("Locked out", "I'm locked out of my account and can't sign in."),
        ("Can't sign in", "It says my account can't be used right now."),
        ("Account locked?", "Sign-in fails on every app. I think my account is locked."),
    ],
    "identity--mfa-lockout": [
        ("MFA not working", "I can't get past the MFA step when I sign in."),
        ("Can't do the second step", "The sign-in asks for my code and then won't let me in."),
        ("Authenticator problem", "My authenticator isn't working, so I can't sign in."),
    ],
}

STYLE_LINES: dict[Style, tuple[str, str]] = {
    "terse": ("", ""),
    "detailed": ("", " I already restarted and tried again, same result. It started this morning."),
    "urgent": ("Urgent: ", " I have a deadline in an hour, please help."),
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Answer(_Frozen):
    cause: str  # a cause id, or "escalate"
    outcome: Literal["fix", "escalate"]
    route: Literal["troubleshoot"] = "troubleshoot"
    requires_approval: bool
    reveals: list[str]
    fix: str | None


class Ticket(_Frozen):
    id: str
    split: Literal["tuning", "heldout"]
    issue: str
    employee_id: str
    opened: date
    style: Style
    subject: str
    message: str
    answer: Answer


def _message(rng: random.Random, issue: str, employee: Employee, style: Style) -> tuple[str, str]:
    subject, body = rng.choice(MESSAGES[issue])
    prefix, suffix = STYLE_LINES[style]
    first = employee.name.split()[0]
    return f"{prefix}{subject}", f"Hi IT,\n\n{body}{suffix}\n\nThanks,\n{first}"


def _pick_employee(rng: random.Random, company: Company, cause: faults.Cause | None, used: set[str]) -> Employee:
    pool = [e for e in company.employees if cause is None or cause.eligible(e)]
    fresh = [e for e in pool if e.id not in used] or pool
    employee = rng.choice(fresh)
    used.add(employee.id)
    return employee


def _plan(issues: tuple[str, ...], planted: int) -> list[faults.Cause]:
    """Spread ``planted`` tickets over the issues, cycling through each issue's causes."""
    per_issue = [planted // len(issues) + (1 if i < planted % len(issues) else 0) for i in range(len(issues))]
    plan: list[faults.Cause] = []
    for issue, count in zip(issues, per_issue, strict=True):
        causes = faults.causes_for(issue)
        plan += [causes[n % len(causes)] for n in range(count)]
    return plan


def generate_split(split: Literal["tuning", "heldout"], company: Company | None = None) -> list[Ticket]:
    company = company or generate()
    issues = TUNING_ISSUES if split == "tuning" else HELDOUT_ISSUES
    planted, escalations = SIZES[split]
    rng = random.Random(f"{SEED}-{split}")
    used: set[str] = set()
    prefix = "TU" if split == "tuning" else "HO"
    styles: tuple[Style, ...] = ("terse", "detailed", "urgent")

    rows: list[tuple[str, faults.Cause | None]] = [(c.issue, c) for c in _plan(issues, planted)]
    rows += [(rng.choice(issues), None) for _ in range(escalations)]
    rng.shuffle(rows)

    tickets = []
    for n, (issue, cause) in enumerate(rows, start=1):
        employee = _pick_employee(rng, company, cause, used)
        style = rng.choice(styles)
        subject, message = _message(rng, issue, employee, style)
        answer = Answer(
            cause=cause.id if cause else faults.ESCALATE,
            outcome="fix" if cause else "escalate",
            requires_approval=cause.requires_approval if cause else False,
            reveals=list(cause.reveals) if cause else [],
            fix=cause.fix if cause else None,
        )
        tickets.append(Ticket(
            id=f"{prefix}-{n:03d}", split=split, issue=issue, employee_id=employee.id,
            opened=company.as_of, style=style, subject=subject, message=message, answer=answer,
        ))
    return tickets


def world_for(ticket: Ticket, company: Company | None = None) -> Company:
    """The company as the copilot will find it for this ticket: one cause planted."""
    return faults.plant(company or generate(), ticket.answer.cause, ticket.employee_id)


def load(split: Literal["tuning", "heldout"], out_dir: Path = OUT_DIR) -> list[Ticket]:
    path = out_dir / f"{split}.jsonl"
    return [Ticket.model_validate_json(line) for line in path.read_text().splitlines() if line]


def render(tickets: list[Ticket]) -> str:
    return "".join(t.model_dump_json() + "\n" for t in tickets)


def write(out_dir: Path = OUT_DIR) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    company = generate()
    counts = {}
    for split in ("tuning", "heldout"):
        tickets = generate_split(split, company)
        (out_dir / f"{split}.jsonl").write_text(render(tickets))
        counts[split] = len(tickets)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the eval tickets.")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)
    counts = write(args.out)
    print(f"tickets: {counts['tuning']} tuning, {counts['heldout']} held-out -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
