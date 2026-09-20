"""Pure functions that turn calibrated numbers into decisions about spending.

Nothing here performs I/O, so every rule the harness follows can be tested
without a network or a key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .config import Thresholds
from .questions import Answer

# What the writing engines are told to answer in. Research drags in pages in
# whatever language they happen to be written in, and without an explicit
# instruction a model will often answer in the language of its sources rather
# than the language the reader chose.
LANGUAGE_NAMES = {"zh": "Simplified Chinese", "en": "English"}


CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
LATIN_WORD = re.compile(r"[A-Za-z]+")


def detect_language(text: str, fallback: str = "en") -> str:
    """Which language the person wrote in.

    The interface language says what the buttons say. It does not say what the
    reader wants to read: somebody may run the English interface and ask in
    Chinese, and answering that in English is simply the wrong answer. So the
    reply follows the question.
    """
    text = text or ""
    cjk = len(CJK.findall(text))
    words = len(LATIN_WORD.findall(text))
    # Compare like with like: a CJK character carries roughly half a word, and
    # a mostly-English sentence with a Chinese clause in it is still English.
    if cjk and cjk / 2 >= words:
        return "zh"
    if words:
        return "en"
    return "zh" if cjk else fallback


def language_rule(language: str) -> str:
    name = LANGUAGE_NAMES.get((language or "en").lower()[:2])
    if not name:
        return ""
    return (
        f" Write your entire answer in {name}, whatever language the material "
        "or the research happens to be in. Leave quoted names, code and URLs "
        "as they are."
    )


def is_uncertain(answer: Answer, thresholds: Thresholds) -> bool:
    """True when this answer is too close to a coin flip to act on alone."""
    if answer.kind == "noul":
        return thresholds.noul_uncertain_low <= answer.probability <= thresholds.noul_uncertain_high
    return answer.certainty < thresholds.decision_abstain_below


@dataclass(frozen=True)
class Review:
    """The split of one Jev batch into answers we trust and answers we don't."""

    accepted: Dict[str, Answer] = field(default_factory=dict)
    uncertain: Dict[str, Answer] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.uncertain

    def to_dict(self) -> dict:
        return {
            "accepted": sorted(self.accepted),
            "uncertain": sorted(self.uncertain),
        }


def review(answers: Mapping[str, Answer], thresholds: Thresholds) -> Review:
    accepted: Dict[str, Answer] = {}
    uncertain: Dict[str, Answer] = {}
    for name, answer in answers.items():
        (uncertain if is_uncertain(answer, thresholds) else accepted)[name] = answer
    return Review(accepted=accepted, uncertain=uncertain)


def facts_block(answers: Mapping[str, Answer], *, mark_uncertain: Mapping[str, Answer] = None) -> str:
    """Render decisions as compact typed facts for an LLM prompt.

    This is a cost lever, not a formatting detail: handing the model the
    judgements already made stops it re-deriving them in tokens you pay for.
    """
    mark_uncertain = mark_uncertain or {}
    lines: List[str] = []
    for name in sorted(answers):
        answer = answers[name]
        flag = "  [uncertain — verify this one]" if name in mark_uncertain else ""
        lines.append(f"- {name}: {answer.describe()}{flag}")
    return "\n".join(lines)


def escalation_messages(
    prompt: str,
    state: str,
    review_result: Review,
    questions: Mapping[str, object],
    language: str = "en",
) -> List[dict]:
    """Ask the LLM to settle only the decisions Jev was unsure about."""
    asked = []
    for name in sorted(review_result.uncertain):
        question = questions.get(name)
        instructions = getattr(question, "instructions", name)
        answer = review_result.uncertain[name]
        asked.append(f"- {name}: {instructions}\n  Jev's reading: {answer.describe()}")
    system = (
        "A fast classifier already judged this material but was not confident "
        "about the items below. Settle them. Be brief and decide; do not "
        "restate the material." + language_rule(language)
    )
    user = f"TASK:\n{prompt.strip()}\n\nUNRESOLVED:\n" + "\n".join(asked)
    if review_result.accepted:
        user += "\n\nALREADY SETTLED (treat as given):\n" + facts_block(review_result.accepted)
    if state.strip():
        user += f"\n\nMATERIAL:\n{state.strip()}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def generation_messages(
    instruction: str,
    state: str,
    answers: Mapping[str, Answer],
    review_result: Review,
    *,
    include_state: bool = True,
    evidence: str = "",
    sources: Sequence[Mapping[str, str]] = (),
    open_items: Sequence[str] = (),
    language: str = "en",
    profile: str = "",
    conversation: str = "",
    rules: str = "",
    searched: Sequence[str] = (),
    evidence_complete: Optional[bool] = None,
    tools: Sequence[str] = (),
) -> List[dict]:
    """The generation prompt.

    It carries Jev's typed facts so the model does not re-derive them, and only
    the research sections that survived filtering — never the raw pages.
    """
    system = (
        "You are the writing engine of a harness. A calibrated classifier has "
        "already made the judgements listed as ESTABLISHED; trust them and do "
        "not second-guess or restate them. Where RESEARCH is given, rely on it "
        "for facts and cite sources inline as [n]. Produce only what is asked "
        f"for. Today is {today()}." + language_rule(language) + tool_guidance(tools)
    )
    parts = []
    if rules.strip():
        parts.append(f"HOUSE RULES (from the workspace — always follow):\n{rules.strip()}")
    if conversation.strip():
        parts.append(
            "CONVERSATION SO FAR (the task below continues it; words like 'it' "
            f"or 'that' refer back to here):\n{conversation.strip()}"
        )
    parts.append(f"TASK:\n{instruction.strip()}")
    note = research_note(searched, bool(evidence.strip()), evidence_complete)
    if note:
        parts.append(note)
    if profile.strip():
        parts.append(
            "ABOUT THE PERSON YOU ARE WRITING FOR (follow unless this task says "
            f"otherwise):\n{profile.strip()}"
        )
    if answers:
        parts.append(
            "ESTABLISHED (already decided, do not re-derive):\n"
            + facts_block(answers, mark_uncertain=review_result.uncertain)
        )
    if open_items:
        parts.append(
            "STILL OPEN (cover these):\n"
            + "\n".join(f"- {item}" for item in open_items)
        )
    if evidence.strip():
        parts.append(f"RESEARCH (filtered extracts):\n{evidence.strip()}")
    if sources:
        parts.append(
            "SOURCES:\n"
            + "\n".join(f"[{i + 1}] {s.get('title') or ''} — {s.get('url')}"
                         for i, s in enumerate(sources))
        )
    if include_state and state.strip():
        parts.append(f"MATERIAL:\n{state.strip()}")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def subtask_messages(
    task: str,
    step: str,
    state: str,
    answers: Mapping[str, Answer],
    review_result: Review,
    *,
    evidence_text: str = "",
    sources: Sequence[Mapping[str, str]] = (),
    previous: Sequence[Tuple[str, str]] = (),
    outline: Sequence[str] = (),
    position: int = 0,
    language: str = "en",
    persona: str = "",
    profile: str = "",
    conversation: str = "",
    rules: str = "",
    searched: Sequence[str] = (),
    evidence_complete: Optional[bool] = None,
    tools: Sequence[str] = (),
) -> List[dict]:
    """One step of a larger task, written by the model sized for that step."""
    system = (
        "You are working on one step of a larger task. Write only that step, "
        "in full and ready to use, with no preamble and no restatement of the "
        "instruction. Do not write a heading or title — one is added for you, "
        "and writing your own produces it twice. Where RESEARCH is given, rely "
        "on it and cite sources inline as [n]. Do not repeat what earlier steps "
        "already said, and do not list the sources at the end; they are shown "
        f"separately. Today is {today()}." + language_rule(language) + tool_guidance(tools)
    )
    if persona:
        # A role's persona leads, so the model knows what kind of worker it is
        # before it is told the mechanics of the step.
        system = persona.strip() + " " + system
    parts = []
    if rules.strip():
        parts.append(f"HOUSE RULES (from the workspace — always follow):\n{rules.strip()}")
    if conversation.strip():
        parts.append(f"CONVERSATION SO FAR:\n{conversation.strip()}")
    parts.append(f"OVERALL TASK:\n{task.strip()}")
    if profile.strip():
        parts.append(
            "ABOUT THE PERSON YOU ARE WRITING FOR (things they have told you "
            f"before — follow them unless this task says otherwise):\n{profile.strip()}"
        )
    if outline:
        # Knowing the whole shape is what lets a step be written out of order
        # without straying into someone else's territory.
        listing = "\n".join(
            f"{i + 1}. {title}{'   <- yours' if i == position else ''}"
            for i, title in enumerate(outline)
        )
        parts.append(
            "THE FULL PLAN (other parts are handled separately — stay inside "
            f"yours and do not cover theirs):\n{listing}"
        )
    parts.append(f"YOUR STEP:\n{step.strip()}")
    note = research_note(searched, bool(evidence_text.strip()), evidence_complete)
    if note:
        parts.append(note)
    if previous:
        parts.append(
            "ALREADY WRITTEN (do not repeat):\n"
            + "\n\n".join(f"— {title} —\n{body[:1200]}" for title, body in previous)
        )
    if answers:
        parts.append(
            "ESTABLISHED (already decided):\n"
            + facts_block(answers, mark_uncertain=review_result.uncertain)
        )
    if evidence_text.strip():
        parts.append(f"RESEARCH:\n{evidence_text.strip()}")
    if sources:
        seen, lines = set(), []
        for source in sources:
            url = source.get("url")
            if url in seen:
                continue
            seen.add(url)
            lines.append(f"[{len(lines) + 1}] {source.get('title') or ''} — {url}")
        parts.append("SOURCES:\n" + "\n".join(lines))
    if state.strip():
        parts.append(f"MATERIAL:\n{state.strip()}")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def assembly_messages(task: str, pieces: Sequence[Tuple[str, str]],
                      language: str = "en") -> List[dict]:
    """Rewrite finished steps into one continuous answer."""
    system = (
        "You are joining finished pieces into one answer. Keep every fact and "
        "every citation. Remove repetition and seams. Do not add new claims, "
        "and do not comment on the process." + language_rule(language)
    )
    body = "\n\n".join(f"— {title} —\n{text}" for title, text in pieces)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"TASK:\n{task.strip()}\n\nPIECES:\n{body}"},
    ]


def today() -> str:
    """The date a prompt should assume, so "10.2" means this year's October."""
    import datetime

    return datetime.date.today().isoformat()


def search_terms_messages(question: str, *, conversation: str = "",
                          searched: Sequence[str] = (), kept: int = 0) -> List[dict]:
    """Ask a cheap model for what a person would actually type into a search box."""
    system = (
        "You write web search queries. Return only a JSON array of 2 or 3 strings. "
        "Each is a short keyword query, the way a person types into a search engine — "
        "never a sentence or a request. Name the concrete things; put the year on any "
        "date; if the subject is international, make one query English."
    )
    parts = [f"Today is {today()}.", f"TASK:\n{question.strip()}"]
    if conversation.strip():
        parts.insert(1, f"CONVERSATION SO FAR:\n{conversation.strip()[-1500:]}")
    if searched:
        parts.append(
            "ALREADY SEARCHED: " + " | ".join(searched)
            + f"\nThat found {kept} useful extract(s), not enough. Write different queries "
              "aimed at what is still missing — other wording, other sources, other languages."
        )
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


def parse_terms(text: str) -> List[str]:
    """The model's array, or its lines if it ignored the format."""
    import json

    text = (text or "").strip()
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            items = json.loads(text[start:end + 1])
            return [str(x).strip() for x in items if str(x).strip()][:3]
        except (ValueError, TypeError):
            pass
    lines = [re.sub(r'^[\s\-*\d.)"]+|"\s*,?$', "", line).strip() for line in text.splitlines()]
    return [line for line in lines if line][:3]


COMPACTION_SYSTEM = """\
You compress the older part of a working conversation so it can continue in a \
smaller context. Write a dense brief of what has happened so far: what was \
asked, what has been established as fact, which files or sources were touched \
and what they said, what has already been produced, and what is still \
outstanding. Keep names, paths, figures and decisions exactly. Drop pleasantries, \
repetition and anything already superseded. No preamble, no headings, no \
apology for summarising. This text replaces the messages it describes, so \
anything you leave out is gone."""


TOOL_GUIDANCE = """\

You can call tools, and their results come back to you before you answer. Use \
them rather than assuming: read the file before describing it, check the number \
before quoting it, look at what your change did. Work in small steps and let \
each result decide the next one. A tool that fails or is refused is information \
too — say what happened and find another way rather than repeating the call. \
When the job has several parts, keep todo_write up to date so nothing is \
quietly dropped. When you have what you need, stop calling tools and write the \
answer; the last thing you say is what the reader sees, so it has to stand on \
its own without the tool transcript."""


def tool_guidance(tools: Sequence[str]) -> str:
    """What the writing model is told when it may call tools."""
    return TOOL_GUIDANCE if tools else ""


def compaction_messages(messages: Sequence[Mapping[str, object]], language: str = "en") -> List[dict]:
    """Ask the cheapest model to fold the older exchanges into one brief."""
    transcript = []
    for message in messages:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role == "tool":
            content = f"(tool result) {content}"
        transcript.append(f"[{role}] {content[:4000]}")
    return [
        {"role": "system", "content": COMPACTION_SYSTEM + language_rule(language)},
        {"role": "user", "content": "\n\n".join(transcript)[:60_000]},
    ]


def research_note(searched: Sequence[str], found: bool,
                  complete: Optional[bool] = None) -> str:
    """What the writer is told about the lookup that already happened.

    Without it a model with no extracts says it cannot browse the web — which
    is false, and was the answer a user actually got — or worse, invents prices.
    """
    if not searched:
        return ""
    if found and complete is False:
        return ("WEB SEARCH: done for you (" + " | ".join(searched) + "). Rely on RESEARCH "
                "below — but it was not judged enough to answer fully. Say plainly what the "
                "sources do not cover; never fill the gap with invented figures.")
    if found:
        return "WEB SEARCH: done for you (" + " | ".join(searched) + "). Rely on RESEARCH below."
    return (
        "WEB SEARCH: already done for you (" + " | ".join(searched) + "), but none of it "
        "gave usable facts. You do have web search — never say you cannot browse. Say "
        "briefly that live figures did not come back, give no specific prices, times, "
        "scores or availability as fact, and tell the reader where to check."
    )
