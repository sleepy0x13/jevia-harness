"""Roles: what kind of worker each subtask gets.

A subtask is not just a prompt on a model. It is a small agent with a job
description, a floor on how capable its model must be, and permission to go and
look things up or not. Those three things are what make a step's output good,
and picking between them is a typed choice — so Jev makes it, for every step at
once, inside the routing call that was already happening.

Adding a role is adding one entry to ``BUILTIN``. Nothing else in the harness
needs to know it exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from .questions import Choice

ROLE_PREFIX = "r"
DEFAULT_ROLE = "writer"


@dataclass(frozen=True)
class Role:
    """One kind of worker."""

    id: str
    label: Dict[str, str]
    persona: str
    summary: str          # the criterion Jev sees when choosing
    min_capability: int = 1
    may_research: bool = False
    tools: Tuple[str, ...] = ()

    def name(self, language: str = "en") -> str:
        return self.label.get(language, self.label.get("en", self.id))

    def to_dict(self, language: str = "en") -> dict:
        return {
            "id": self.id,
            "name": self.name(language),
            "min_capability": self.min_capability,
            "may_research": self.may_research,
            "tools": list(self.tools),
        }


BUILTIN: Tuple[Role, ...] = (
    Role(
        id="extractor",
        label={"en": "Extractor", "zh": "提取"},
        persona=(
            "You pull specific values out of material exactly as they appear. "
            "You do not interpret, summarise or add anything that is not there, "
            "and when something is absent you say so plainly."
        ),
        summary=(
            "Pulling named values, fields or lists straight out of the material "
            "with no interpretation."
        ),
        min_capability=1,
        tools=("read_file",),
    ),
    Role(
        id="researcher",
        label={"en": "Researcher", "zh": "调研"},
        persona=(
            "You establish what is actually true and where it was said. Every "
            "claim you make is traceable to a source you cite. You distinguish "
            "what a source states from what it merely implies, and you say when "
            "the record is thin or the sources disagree."
        ),
        summary=(
            "Finding out facts that are not in the material yet, and saying "
            "where each one came from."
        ),
        min_capability=2,
        may_research=True,
        tools=("fetch_url", "now"),
    ),
    Role(
        id="analyst",
        label={"en": "Analyst", "zh": "分析"},
        persona=(
            "You weigh evidence and reach a judgement. You state the judgement "
            "first and the reasoning after, you name the strongest case against "
            "it, and you never present a preference as a finding."
        ),
        summary=(
            "Weighing evidence, comparing options, or reaching a judgement that "
            "could reasonably go either way."
        ),
        min_capability=3,
        may_research=True,
        tools=("calculate", "now"),
    ),
    Role(
        id="writer",
        label={"en": "Writer", "zh": "撰写"},
        persona=(
            "You write for a reader who is busy and intelligent. Concrete nouns, "
            "short sentences, no throat-clearing, no summary of what you are "
            "about to say."
        ),
        summary="Producing prose a reader will actually read: explanation, narrative, a reply.",
        min_capability=2,
    ),
    Role(
        id="editor",
        label={"en": "Editor", "zh": "编辑"},
        persona=(
            "You improve text that already exists. You cut what does not earn "
            "its place, fix what is wrong, and leave the author's voice alone. "
            "You return the finished text, never notes about it."
        ),
        summary="Rewriting, tightening, correcting or restructuring text that already exists.",
        min_capability=2,
    ),
    Role(
        id="coder",
        label={"en": "Engineer", "zh": "工程"},
        persona=(
            "You write code that runs. You match the surrounding style, handle "
            "the error cases, and say plainly what you did not handle. No "
            "placeholder comments standing in for work."
        ),
        summary="Writing, changing or explaining code, queries, configuration or schemas.",
        min_capability=3,
        tools=("read_file", "write_file", "search_files", "list_dir"),
    ),
)

BY_ID: Dict[str, Role] = {role.id: role for role in BUILTIN}


def get(role_id: Optional[str]) -> Role:
    return BY_ID.get(role_id or "", BY_ID[DEFAULT_ROLE])


def question(step: str) -> Choice:
    """The choice Jev answers to staff one step."""
    return Choice(
        instructions=(
            "Which kind of worker should carry out this step? Judge by what the "
            f"step actually requires. The step is: {step.strip()}"
        ),
        options={role.id: role.summary for role in BUILTIN},
    )


def roster(language: str = "en") -> List[dict]:
    return [role.to_dict(language) for role in BUILTIN]
