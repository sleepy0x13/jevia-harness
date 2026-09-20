"""Where the user's work actually lands.

The workspace is not decoration. Everything a run produces is written into it as
plain files the user owns: a readable markdown transcript per conversation, and
each step's output beside it. Nothing here is a database, and nothing needs this
program to read it back.

    <workspace>/chats/2026-09-19-triage-a1b2c3.md      the conversation
    <workspace>/outputs/2026-09-19-triage-a1b2c3/      one file per step
    <workspace>/.jevia/                                 memory, plans, schedule
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

CHATS = "chats"
OUTPUTS = "outputs"
MAX_SLUG = 40
MAX_STATE_ECHO = 2000

SAFE = re.compile(r"[^a-z0-9一-鿿]+")


def _id(chat_id: str) -> str:
    """A conversation id, reduced to something that can only be a file name.

    The id comes from the caller, so it never reaches the filesystem as it
    was sent: no separators, no dots, no traversal.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "", str(chat_id or ""))[:40].strip("-_")
    return cleaned or "session"


def slug(text: str) -> str:
    cleaned = SAFE.sub("-", (text or "").lower()).strip("-")
    return (cleaned[:MAX_SLUG].strip("-") or "task")


@dataclass
class Saved:
    chat_path: Path
    output_paths: List[Path]

    def to_dict(self, workspace: Optional[Path] = None) -> dict:
        def show(path: Path) -> str:
            if workspace:
                try:
                    return str(path.relative_to(workspace))
                except ValueError:
                    pass
            return str(path)

        return {
            "chat": show(self.chat_path),
            "outputs": [show(p) for p in self.output_paths],
            "chat_abs": str(self.chat_path),
        }


class Store:
    """Writes a conversation and its outputs into the workspace."""

    def __init__(self, workspace: Optional[Path]) -> None:
        self.workspace = Path(workspace).expanduser() if workspace else None

    @property
    def enabled(self) -> bool:
        return self.workspace is not None

    # -- paths -------------------------------------------------------------- #

    def chat_path(self, chat_id: str, title: str, created: Optional[float] = None) -> Path:
        stamp = time.strftime("%Y-%m-%d", time.localtime(created or time.time()))
        return self.workspace / CHATS / f"{stamp}-{slug(title)}-{_id(chat_id)}.md"

    def output_dir(self, chat_id: str, title: str, created: Optional[float] = None) -> Path:
        stamp = time.strftime("%Y-%m-%d", time.localtime(created or time.time()))
        return self.workspace / OUTPUTS / f"{stamp}-{slug(title)}-{_id(chat_id)}"

    # -- writing ------------------------------------------------------------ #

    def save_turn(self, *, chat_id: str, title: str, prompt: str, state: str,
                  result: Mapping[str, Any], created: Optional[float] = None,
                  evidence: str = "") -> Optional[Saved]:
        """Append one exchange to the conversation file and write its outputs."""
        if not self.enabled:
            return None
        chat = self.chat_path(chat_id, title, created)
        outputs = self.output_dir(chat_id, title, created)
        try:
            chat.parent.mkdir(parents=True, exist_ok=True)
            fresh = not chat.exists()
            with chat.open("a", encoding="utf-8") as handle:
                if fresh:
                    handle.write(f"# {title.strip() or 'Untitled'}\n\n")
                handle.write(self._render(prompt, state, result))
        except OSError:
            return None

        written: List[Path] = []
        steps = [s for s in (result.get("subtasks") or []) if (s.get("output") or "").strip()]
        try:
            if steps:
                outputs.mkdir(parents=True, exist_ok=True)
                for index, step in enumerate(steps, 1):
                    path = outputs / f"{index:02d}-{slug(step.get('title') or 'step')}.md"
                    path.write_text(
                        f"# {step.get('title') or ''}\n\n{step.get('output') or ''}\n",
                        encoding="utf-8",
                    )
                    written.append(path)
            if evidence.strip():
                # What research kept, in full, so the answer can be checked
                # against it later without re-running anything.
                outputs.mkdir(parents=True, exist_ok=True)
                sources = (result.get("evidence") or {}).get("sources") or []
                listing = "\n".join(f"{i}. {s.get('title') or ''} — {s.get('url')}"
                                     for i, s in enumerate(sources, 1))
                path = outputs / "evidence.md"
                path.write_text(f"# Evidence\n\n{listing}\n\n---\n\n{evidence.strip()}\n",
                                encoding="utf-8")
                written.append(path)
            page = (result.get("app") or {}).get("html") or ""
            if page:
                # A working file the user can open, share or keep editing.
                outputs.mkdir(parents=True, exist_ok=True)
                path = outputs / "app.html"
                path.write_text(page, encoding="utf-8")
                written.append(path)
            final = (result.get("output") or "").strip()
            if final:
                outputs.mkdir(parents=True, exist_ok=True)
                path = outputs / "answer.md"
                path.write_text(final + "\n", encoding="utf-8")
                written.append(path)
        except OSError:
            pass
        return Saved(chat_path=chat, output_paths=written)

    # -- rendering ---------------------------------------------------------- #

    @staticmethod
    def _render(prompt: str, state: str, result: Mapping[str, Any]) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        lines = [f"## {stamp}", "", "**Task**", "", prompt.strip(), ""]

        if state.strip():
            body = state.strip()
            if len(body) > MAX_STATE_ECHO:
                body = body[:MAX_STATE_ECHO] + f"\n…[{len(state) - MAX_STATE_ECHO} more characters]"
            lines += ["<details><summary>Material</summary>", "", "```", body, "```", "",
                      "</details>", ""]

        decisions = result.get("decisions") or {}
        if decisions:
            lines += ["**Judgements**", ""]
            for name, answer in decisions.items():
                lines.append(f"- `{name}` — {answer.get('summary', '')}")
            lines.append("")

        steps = result.get("subtasks") or []
        if steps:
            lines += ["**Steps**", ""]
            for step in steps:
                mark = {"done": "x", "skipped": "-"}.get(step.get("status"), " ")
                who = step.get("model_label") or ""
                role = step.get("role") or ""
                lines.append(
                    f"- [{mark}] {step.get('title')}"
                    + (f" — {role}, {who}" if who else "")
                    + (f" (Jev: level {step.get('required')})" if step.get("required") is not None else "")
                    + (f" — failed: {step.get('note')}" if step.get("status") == "failed" and step.get("note") else "")
                )
            lines.append("")

        selection = result.get("selection") or {}
        if not steps and selection.get("label"):
            lines += [f"**Model** — {selection['label']}"
                      + (f" (Jev: level {selection.get('required')})" if selection.get("required") is not None else ""), ""]
        switches = [s for s in (result.get("steps") or []) if s.get("code") == "failover"]
        for switch in switches:
            data = switch.get("data") or {}
            lines.append(f"> {data.get('from')} was unavailable ({data.get('reason')}); used {data.get('to')} instead.")
        if switches:
            lines.append("")

        answer = (result.get("output") or "").strip()
        if answer:
            lines += ["**Answer**", "", answer, ""]

        evidence = result.get("evidence") or {}
        sources = evidence.get("sources") or []
        if sources:
            lines += ["**Sources**", ""]
            for index, source in enumerate(sources, 1):
                lines.append(f"{index}. [{source.get('title') or source.get('url')}]({source.get('url')})")
            lines.append("")

        cost = (result.get("ledger") or {}).get("cost") or {}
        if result.get("ledger"):
            unknown = int(cost.get("unknown_calls") or 0)
            lines.append(
                f"*{_money(cost.get('known_usd'))}"
                + (f" + {unknown} request(s) of unknown cost" if unknown else "")
                + f" · {result.get('elapsed_ms', 0)}ms"
                + (f" · run {result['run_id']}" if result.get("run_id") else "") + "*"
            )
            lines.append("")
        for item in result.get("needs_review") or []:
            lines.append(f"> Needs review: {item.get('reason')}"
                         + (f" (needs level {item.get('required')}, best available "
                            f"{item.get('best_available')})" if item.get("required") else ""))
        if result.get("needs_review"):
            lines.append("")
        lines.append("---")
        lines.append("")
        return "\n".join(lines)


def _money(value: Any) -> str:
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "$0"
    if value <= 0:
        return "$0"
    return f"${value:.6f}"
