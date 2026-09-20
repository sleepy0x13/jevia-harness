"""What the harness remembers about the person using it.

A fact is kept only when the user actually stated it — a preference, a
constraint, a name for something in their world — and only when Jev agrees it is
durable rather than true of this one task. Candidates are pulled out of the
user's own words by pattern, and judged in the call the run was already making,
so remembering costs nothing per run.

Facts live in a JSONL file inside the workspace. It is a plain text file the
user can read, edit or delete, which is the only honest way to store something
claiming to be a profile.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .questions import Noul

STORE = Path(".jevia") / "memory.jsonl"
MAX_FACTS = 400
MAX_IN_PROMPT = 14
MAX_CANDIDATES = 4
KEEP_ABOVE = 0.7

# Sentences where somebody is telling you something about themselves or their
# standing requirements, rather than describing the task at hand.
CANDIDATE = re.compile(
    r"(?:^|[.!?;\n]|，|。|；)\s*("
    r"(?:i|we)\s+(?:prefer|always|never|usually|don't|do not|can't|cannot|use|need|work|am|are)\b[^.!?\n]{4,150}"
    r"|(?:my|our)\s+(?:team|company|stack|style|tone|audience|customers?|product|name|boss|clients?)\b[^.!?\n]{3,150}"
    r"|(?:remember|note|keep in mind|for future reference)[^.!?\n]{4,150}"
    r"|我(?:们)?(?:的|团队|公司|这边|产品|客户|用户|项目)?[^。！？\n]{0,10}"
    r"(?:喜欢|习惯|一般|通常|总是|从不|不要|不用|不能|需要|用的是|偏好|要求)[^。！？\n]{2,80}"
    r"|记住[^。！？\n]{2,80}"
    r"|以后(?:都|一律|统一)?[^。！？\n]{2,80}"
    r")",
    re.IGNORECASE,
)


@dataclass
class Fact:
    text: str
    created: float = field(default_factory=time.time)
    used: int = 0
    source: str = "stated"

    def to_dict(self) -> dict:
        return asdict(self)


class Memory:
    """A small, honest profile: append-only, readable, deletable."""

    def __init__(self, workspace: Optional[Path] = None) -> None:
        self.path: Optional[Path] = (Path(workspace) / STORE) if workspace else None
        self.facts: List[Fact] = []
        self._load()

    # -- storage ------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = str(row.get("text") or "").strip()
                if text:
                    self.facts.append(
                        Fact(text=text, created=float(row.get("created") or 0),
                             used=int(row.get("used") or 0),
                             source=str(row.get("source") or "stated"))
                    )
        except OSError:
            return
        self.facts = self.facts[-MAX_FACTS:]

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                "\n".join(json.dumps(f.to_dict(), ensure_ascii=False)
                          for f in self.facts[-MAX_FACTS:]) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return

    # -- reading ------------------------------------------------------------ #

    def recall(self, limit: int = MAX_IN_PROMPT) -> List[Fact]:
        """The facts worth carrying into a prompt: most used, then most recent."""
        return sorted(self.facts, key=lambda f: (-f.used, -f.created))[:limit]

    def block(self, limit: int = MAX_IN_PROMPT) -> str:
        chosen = self.recall(limit)
        if not chosen:
            return ""
        for fact in chosen:
            fact.used += 1
        self._save()
        return "\n".join(f"- {f.text}" for f in chosen)

    # -- writing ------------------------------------------------------------ #

    @staticmethod
    def candidates(prompt: str) -> List[str]:
        """Sentences where the user stated something about themselves."""
        seen: Dict[str, None] = {}
        for match in CANDIDATE.finditer(prompt or ""):
            text = " ".join(match.group(1).split()).strip(" ,.;:，。；")
            if 6 <= len(text) <= 160:
                seen.setdefault(text, None)
            if len(seen) >= MAX_CANDIDATES:
                break
        return list(seen)

    @staticmethod
    def question(candidate: str) -> Noul:
        return Noul(
            instructions=(
                "Is this a durable fact about this person or how they want work "
                "done — something that will still be true and useful next week? "
                "Answer no if it only describes the task in front of them, or if "
                f"it is a one-off instruction. — \"{candidate}\""
            )
        )

    def add(self, text: str, source: str = "stated") -> bool:
        text = " ".join((text or "").split()).strip()
        if not text or not self.enabled:
            return False
        lowered = text.lower()
        if any(f.text.lower() == lowered for f in self.facts):
            return False
        self.facts.append(Fact(text=text, source=source))
        self.facts = self.facts[-MAX_FACTS:]
        self._save()
        return True

    def forget(self, text: str) -> bool:
        before = len(self.facts)
        self.facts = [f for f in self.facts if f.text != text]
        if len(self.facts) != before:
            self._save()
            return True
        return False

    def clear(self) -> None:
        self.facts = []
        self._save()

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "path": str(self.path) if self.path else None,
            "count": len(self.facts),
            "facts": [f.to_dict() for f in sorted(self.facts, key=lambda f: -f.created)],
        }


RULE_FILES = ("AGENTS.md", "JEVIA.md", "agents.md")
MAX_RULES = 6000


def house_rules(workspace: Optional[Path]) -> str:
    """Standing instructions the user keeps in the workspace, if any.

    The same convention other harnesses use: a markdown file at the root of the
    project, read at the start of every run, that says how this person or team
    wants work done. It is the user's file; nothing here writes to it.
    """
    if not workspace:
        return ""
    for name in RULE_FILES:
        path = Path(workspace) / name
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")[:MAX_RULES].strip()
            except OSError:
                return ""
    return ""
