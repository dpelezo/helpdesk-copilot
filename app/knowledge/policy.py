"""Read the IT policy handbook into sections, and load the policy questions.

The handbook (``knowledge/handbook/it-policy.md``) is a knowledge source:
Workflow A ingests it, and policy answers cite its section ids, such as ``pw-2``.
The policy questions (``evals/policy/questions.jsonl``) are eval ground truth:
each names the sections a correct answer must cite and facts it must mention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

HANDBOOK = Path("knowledge/handbook/it-policy.md")
QUESTIONS = Path("evals/policy/questions.jsonl")

_TOPIC = re.compile(r"^## (?P<title>.+)$")
_SECTION = re.compile(r"^### \[(?P<id>[a-z]+-\d+)\] (?P<title>.+)$")


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    topic: str
    text: str


def sections(path: Path = HANDBOOK) -> dict[str, Section]:
    """The handbook's sections by id, in order. Raises on a repeated id."""
    found: dict[str, Section] = {}
    topic = ""
    current: tuple[str, str] | None = None
    body: list[str] = []

    def close() -> None:
        if current is None:
            return
        section_id, title = current
        if section_id in found:
            raise ValueError(f"handbook section id repeated: {section_id}")
        found[section_id] = Section(section_id, title, topic, "\n".join(body).strip())

    for line in path.read_text().splitlines():
        if match := _SECTION.match(line):
            close()
            current, body = (match["id"], match["title"]), []
        elif match := _TOPIC.match(line):
            close()
            topic, current, body = match["title"], None, []
        elif current is not None:
            body.append(line)
    close()
    return found


class Question(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    split: Literal["tuning", "heldout"]
    topic: str
    question: str
    route: Literal["policy"] = "policy"
    # Sections a correct answer must cite.
    sections: list[str] = Field(min_length=1)
    # Facts a correct answer must state; each appears in one of the cited sections.
    must_mention: list[str] = Field(min_length=1)


def questions(path: Path = QUESTIONS) -> list[Question]:
    return [Question.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]
