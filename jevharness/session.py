"""The log a step is built from.

One rule holds this module together: **what the model sees is what the log
says**. Every message that reaches a request is an entry here first, so the
same context can be rebuilt after a reload, summarised when it grows too
large, or replayed into a transcript without guessing what was in the prompt.

The alternative — assembling a prompt from scattered fields at each step — is
what makes long conversations quietly lose things: a budget truncates the
history here, another truncates the material there, and nobody can say what
the model actually read. Here the budget is applied in one place, what it
shadows is recorded, and the summary that replaces it is an entry of its own.
"""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .providers import ToolCall, estimate_tokens

SYSTEM, USER, ASSISTANT, TOOL, SUMMARY = "system", "user", "assistant", "tool", "summary"
ROLES = (SYSTEM, USER, ASSISTANT, TOOL, SUMMARY)


@dataclass
class Entry:
    """One thing the model was told, or said, or was handed back."""

    role: str
    text: str = ""
    seq: int = 0
    # Assistant entries carry what the model asked to run; tool entries carry
    # which call they answer.
    calls: List[ToolCall] = field(default_factory=list)
    call_id: str = ""
    name: str = ""
    # True once a summary has taken this entry's place in the context. The
    # entry stays in the log: shadowed is not deleted.
    shadowed: bool = False
    tokens: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    def message(self) -> Optional[dict]:
        """This entry as a chat message, or None when it carries no message."""
        if self.role == TOOL:
            return {"role": "tool", "tool_call_id": self.call_id, "content": self.text}
        if self.role == ASSISTANT:
            message: dict = {"role": "assistant", "content": self.text or None}
            if self.calls:
                message["tool_calls"] = [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                    for c in self.calls
                ]
            return message
        if self.role == SUMMARY:
            return {"role": "user", "content": self.text}
        return {"role": self.role, "content": self.text}

    def to_dict(self) -> dict:
        return {"seq": self.seq, "role": self.role, "name": self.name,
                "chars": len(self.text), "tokens": self.tokens,
                "calls": [c.to_dict() for c in self.calls],
                "shadowed": self.shadowed, "meta": self.meta}


class Log:
    """An append-only conversation, and the messages derived from it."""

    def __init__(self, entries: Optional[Iterable[Entry]] = None) -> None:
        self.entries: List[Entry] = list(entries or [])
        self._seq = itertools.count(len(self.entries) + 1)

    def __len__(self) -> int:
        return len(self.entries)

    # -- writing ------------------------------------------------------------ #

    def append(self, role: str, text: str = "", *, calls: Sequence[ToolCall] = (),
               call_id: str = "", name: str = "", **meta: Any) -> Entry:
        if role not in ROLES:
            raise ValueError(f"unknown entry role {role!r}")
        entry = Entry(role=role, text=text or "", seq=next(self._seq), calls=list(calls),
                      call_id=call_id, name=name, meta=dict(meta))
        entry.tokens = estimate_tokens(entry.text) + sum(
            estimate_tokens(json.dumps(c.arguments, ensure_ascii=False)) + 8 for c in entry.calls)
        self.entries.append(entry)
        return entry

    def system(self, text: str) -> Entry:
        """Set the standing instructions, replacing any earlier ones.

        A system entry is not appended twice: a second one would leave the
        model reading two sets of instructions and following the older.
        """
        for entry in self.entries:
            if entry.role == SYSTEM:
                entry.shadowed = True
        return self.append(SYSTEM, text)

    def extend(self, messages: Sequence[Mapping[str, Any]]) -> None:
        """Seed from plain chat messages, as a conversation's history arrives."""
        for message in messages:
            role = str(message.get("role") or USER)
            if role == SYSTEM:
                self.system(str(message.get("content") or ""))
            elif role in (USER, ASSISTANT):
                self.append(role, str(message.get("content") or ""))

    # -- reading ------------------------------------------------------------ #

    def live(self) -> List[Entry]:
        return [e for e in self.entries if not e.shadowed]

    def messages(self) -> List[dict]:
        """Exactly what the next request will carry."""
        out: List[dict] = []
        for entry in self.live():
            message = entry.message()
            if message is None:
                continue
            if message.get("content") is None and not message.get("tool_calls"):
                continue
            out.append(message)
        return out

    def tokens(self) -> int:
        return sum(e.tokens for e in self.live())

    def text_of(self, role: str) -> str:
        return "\n\n".join(e.text for e in self.live() if e.role == role and e.text)

    def answered(self, call_id: str) -> bool:
        return any(e.role == TOOL and e.call_id == call_id for e in self.entries)

    # -- compaction ---------------------------------------------------------- #

    def compactable(self, keep_last: int = 6) -> List[Entry]:
        """The older exchanges a summary may replace.

        The standing instructions and the most recent turns are never
        summarised: the first is the contract, the last is what the model is
        in the middle of.
        """
        live = [e for e in self.live() if e.role != SYSTEM]
        older = live[:-keep_last] if keep_last else live
        # A tool result is only intelligible next to the call that asked for
        # it, so a summary never splits one from the other.
        while older and older[-1].role == ASSISTANT and older[-1].calls:
            older = older[:-1]
        return older

    def compact(self, summary: str, replacing: Sequence[Entry]) -> Optional[Entry]:
        """Shadow ``replacing`` and put one summary in its place."""
        replacing = [e for e in replacing if not e.shadowed]
        if not replacing or not summary.strip():
            return None
        for entry in replacing:
            entry.shadowed = True
        return self.append(SUMMARY, summary.strip(),
                           shadowed_seqs=[e.seq for e in replacing],
                           shadowed_tokens=sum(e.tokens for e in replacing))

    def to_dict(self) -> dict:
        return {"entries": [e.to_dict() for e in self.entries],
                "tokens": self.tokens(), "shadowed": sum(1 for e in self.entries if e.shadowed)}
