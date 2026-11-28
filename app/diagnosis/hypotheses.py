"""What the diagnosis loop reasons about: possible causes and the checks that test them."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Hypothesis:
    """A possible cause from the graph, with the read-only checks that ``TESTS_FOR`` it."""

    cause: str
    issue: str  # the issue's title, as shown to the model
    summary: str
    fix: str
    checks: tuple[str, ...] = ()
    sources: tuple[str, ...] = field(default=())  # provenance: where the cause and fix come from

    def line(self) -> str:
        """The cause as condition B showed it, so conditions B and C see the same text."""
        return (f"- Issue: {self.issue}. Possible cause ({self.cause}): {self.summary} Fix: {self.fix} "
                f"Revealed by: {', '.join(self.checks) or 'no check'}.")
