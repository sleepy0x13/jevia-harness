"""A loop's goal, split into what code can check and what needs judgement.

"Under 280 characters" is not a question for a model: it is a count, and a
count has one right answer. So a goal is read into conditions first. The
countable ones — length limits, required phrases — are checked here, by rules
stated below. Whatever is left is a semantic condition and goes to Jev as one
question. The loop shows every condition with who checked it, and "limit
reached" is never reported as "goal met".

Counting rules (stated, because a limit without one is not checkable):

* **characters** — Unicode code points of the output, whitespace excluded. A
  Chinese character, a Latin letter and a punctuation mark each count as one;
  spaces and line breaks do not.
* **words** — runs of Latin letters or digits (joined by an apostrophe or a
  hyphen) count as one word each; every CJK character counts as one word.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional

COUNT_RULE_CHARS = "unicode code points, whitespace excluded"
COUNT_RULE_WORDS = "latin word runs + one per CJK character"
MET_ABOVE = 0.75

_N = r"(\d[\d,]*)"
_CHARS = r"(?:characters?|chars?|个字符|字符|个字|字)"
_WORDS = r"(?:words?|个词|词)"

_PATTERNS = [
    ("max_chars", re.compile(rf"(?:under|below|at most|no more than|fewer than|less than|within|max(?:imum)?|up to)\s+{_N}\s*{_CHARS}", re.I)),
    ("max_chars", re.compile(rf"{_N}\s*{_CHARS}\s*(?:以内|之内|以下)")),
    ("max_chars", re.compile(rf"(?:不超过|不多于|少于|最多|控制在)\s*{_N}\s*{_CHARS}")),
    ("min_chars", re.compile(rf"(?:at least|no fewer than|more than|over|min(?:imum)?)\s+{_N}\s*{_CHARS}", re.I)),
    ("min_chars", re.compile(rf"(?:至少|不少于|超过|多于)\s*{_N}\s*{_CHARS}")),
    ("max_words", re.compile(rf"(?:under|below|at most|no more than|fewer than|less than|within|up to)\s+{_N}\s*{_WORDS}", re.I)),
    ("min_words", re.compile(rf"(?:at least|no fewer than|more than|over)\s+{_N}\s*{_WORDS}", re.I)),
    ("contains", re.compile(r"(?:includes?|contains?|mentions?|must (?:say|include))\s+[\"“'‘]([^\"”'’]{1,80})[\"”'’]", re.I)),
    ("contains", re.compile(r"(?:包含|包括|提到|出现)\s*[\"“'‘「]([^\"”'’」]{1,80})[\"”'’」]")),
]


@dataclass
class Condition:
    kind: str                 # max_chars | min_chars | max_words | min_words | contains | semantic
    text: str                 # the words of the goal this came from
    value: object = None

    @property
    def checker(self) -> str:
        return "jev" if self.kind == "semantic" else "code"


def count_chars(text: str) -> int:
    return sum(1 for ch in (text or "") if not ch.isspace())


def count_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*|[぀-ヿ一-鿿]", text or ""))


def parse(goal: str) -> List[Condition]:
    """The goal's conditions: the countable ones, then one semantic remainder."""
    goal = (goal or "").strip()
    found: List[Condition] = []
    rest = goal
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(goal):
            raw = match.group(1)
            value: object = raw if kind == "contains" else int(raw.replace(",", ""))
            found.append(Condition(kind, match.group(0).strip(), value))
            rest = rest.replace(match.group(0), " ")
    # What is left is a condition only if it still says something.
    rest = re.sub(r"(?:\s|[,，.。;；、]|\band\b|和|及|并且|且)+", " ", rest, flags=re.I).strip()
    if len(re.sub(r"\W", "", rest)) >= 3:
        found.append(Condition("semantic", goal if not found else rest))
    return found


def check(conditions: List[Condition], output: str,
          judge: Optional[Callable[[str, str], Optional[float]]] = None) -> dict:
    """Every condition's result, who checked it, and whether all are met.

    ``judge(condition_text, output)`` is Jev's probability for a semantic
    condition; None means it could not be checked, which is not a pass.
    """
    rows = []
    for cond in conditions:
        row = {"kind": cond.kind, "text": cond.text, "checker": cond.checker, "met": None}
        if cond.kind in ("max_chars", "min_chars"):
            measured = count_chars(output)
            row.update(measured=measured, limit=cond.value, rule=COUNT_RULE_CHARS,
                       met=measured <= cond.value if cond.kind == "max_chars" else measured >= cond.value)
        elif cond.kind in ("max_words", "min_words"):
            measured = count_words(output)
            row.update(measured=measured, limit=cond.value, rule=COUNT_RULE_WORDS,
                       met=measured <= cond.value if cond.kind == "max_words" else measured >= cond.value)
        elif cond.kind == "contains":
            row.update(met=str(cond.value).lower() in (output or "").lower(), rule="case-insensitive substring")
        else:
            probability = judge(cond.text, output) if judge else None
            row.update(probability=probability, threshold=MET_ABOVE,
                       met=None if probability is None else probability >= MET_ABOVE)
        rows.append(row)
    met = bool(rows) and all(r["met"] is True for r in rows)
    # A single number for older records: code checks count as 0 or 1.
    scores = [(1.0 if r["met"] else 0.0) if r["checker"] == "code" else r.get("probability")
              for r in rows]
    known = [s for s in scores if s is not None]
    return {"conditions": rows, "met": met,
            "score": min(known) if known and len(known) == len(scores) else None}
