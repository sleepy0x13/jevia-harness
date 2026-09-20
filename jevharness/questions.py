"""Jev's three question primitives, and the answers they return.

These mirror the System One API exactly (choice / score / noul), but as frozen
Python types so that a malformed decision fails here — locally, for free —
rather than after a round trip.

Every answer exposes a single ``certainty`` in [0, 1] so that policy code can
threshold any answer type without caring which one it is. That unification is
what lets the executor escalate uncertain decisions to the LLM generically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Union

from .errors import SchemaError

MAX_CARDINALITY = 255  # documented Jev limit


def _clean_instructions(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{where}: 'instructions' must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class Choice:
    """Pick one of an unordered set of options."""

    instructions: str
    options: Mapping[str, str]

    kind = "choice"

    def validate(self, name: str) -> None:
        _clean_instructions(self.instructions, name)
        if not isinstance(self.options, Mapping) or len(self.options) < 2:
            raise SchemaError(f"{name}: choice needs at least 2 options")
        if len(self.options) > MAX_CARDINALITY:
            raise SchemaError(f"{name}: choice exceeds {MAX_CARDINALITY} options")
        for key in self.options:
            if not isinstance(key, str) or not key.strip():
                raise SchemaError(f"{name}: option ids must be non-empty strings")

    def to_payload(self) -> dict:
        return {
            "type": "choice",
            "instructions": self.instructions.strip(),
            "criteria": {k: (v or None) for k, v in self.options.items()},
        }


@dataclass(frozen=True)
class Score:
    """Rate along an ordered spectrum you define level by level."""

    instructions: str
    levels: Sequence[str]

    kind = "score"

    def validate(self, name: str) -> None:
        _clean_instructions(self.instructions, name)
        if isinstance(self.levels, (str, bytes)) or not isinstance(self.levels, Sequence):
            raise SchemaError(f"{name}: score 'levels' must be a list")
        if len(self.levels) < 2:
            raise SchemaError(f"{name}: score needs at least 2 levels")
        if len(self.levels) > MAX_CARDINALITY:
            raise SchemaError(f"{name}: score exceeds {MAX_CARDINALITY} levels")
        for level in self.levels:
            if not isinstance(level, str) or not level.strip():
                raise SchemaError(f"{name}: score levels must be non-empty strings")

    def to_payload(self) -> dict:
        return {
            "type": "score",
            "instructions": self.instructions.strip(),
            "criteria": [str(level).strip() for level in self.levels],
        }


@dataclass(frozen=True)
class Noul:
    """A yes/no where the probability itself is the signal."""

    instructions: str
    criteria: Optional[Mapping[str, str]] = None

    kind = "noul"

    def validate(self, name: str) -> None:
        _clean_instructions(self.instructions, name)
        if self.criteria is not None and not isinstance(self.criteria, Mapping):
            raise SchemaError(f"{name}: noul 'criteria' must be an object when given")

    def to_payload(self) -> dict:
        payload: dict = {"type": "noul", "instructions": self.instructions.strip()}
        if self.criteria:
            payload["criteria"] = {str(k): str(v) for k, v in self.criteria.items()}
        return payload


Question = Union[Choice, Score, Noul]


# --------------------------------------------------------------------------- #
# Answers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChoiceAnswer:
    value: str
    probabilities: Mapping[str, float]
    confidence: float

    kind = "choice"

    @property
    def certainty(self) -> float:
        return self.confidence

    def describe(self) -> str:
        return f"{self.value} (p={self.probabilities.get(self.value, 0):.2f})"


@dataclass(frozen=True)
class ScoreAnswer:
    value: float
    legend: Mapping[str, str]
    probabilities: Mapping[str, float]
    confidence: float

    kind = "score"

    @property
    def certainty(self) -> float:
        return self.confidence

    @property
    def level(self) -> int:
        """Nearest discrete level index."""
        return int(round(self.value))

    @property
    def normalized(self) -> float:
        """Position in [0, 1] across the levels, useful for thresholds."""
        top = max(len(self.legend) - 1, 1)
        return min(max(self.value / top, 0.0), 1.0)

    def describe(self) -> str:
        label = self.legend.get(str(self.level), "")
        return f"{self.value:.2f}/{max(len(self.legend) - 1, 1)} — {label}"


@dataclass(frozen=True)
class NoulAnswer:
    probability: float

    kind = "noul"

    @property
    def value(self) -> bool:
        return self.probability >= 0.5

    @property
    def certainty(self) -> float:
        """Distance from a coin flip, rescaled to [0, 1].

        Jev returns no confidence field for noul; the probability carries it.
        0.5 -> 0.0 certainty, 0.0 or 1.0 -> 1.0 certainty.
        """
        return min(max(abs(self.probability - 0.5) * 2.0, 0.0), 1.0)

    def describe(self) -> str:
        return f"{'yes' if self.value else 'no'} (p={self.probability:.2f})"


Answer = Union[ChoiceAnswer, ScoreAnswer, NoulAnswer]


def parse_answer(name: str, raw: Any) -> Answer:
    """Turn one entry of Jev's ``answers`` map into a typed answer."""
    if not isinstance(raw, Mapping):
        raise SchemaError(f"answer '{name}' is not an object")
    kind = raw.get("type")
    try:
        if kind == "choice":
            return ChoiceAnswer(
                value=str(raw["choice"]),
                probabilities={str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()},
                confidence=float(raw.get("confidence", 0.0)),
            )
        if kind == "score":
            return ScoreAnswer(
                value=float(raw["score"]),
                legend={str(k): str(v) for k, v in (raw.get("legend") or {}).items()},
                probabilities={str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()},
                confidence=float(raw.get("confidence", 0.0)),
            )
        if kind == "noul":
            return NoulAnswer(probability=float(raw["noul"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaError(f"answer '{name}' is malformed for type {kind!r}") from exc
    raise SchemaError(f"answer '{name}' has unknown type {kind!r}")


def question_from_dict(name: str, spec: Any) -> Question:
    """Build a Question from plain JSON, as sent by the API or a cached plan."""
    if not isinstance(spec, Mapping):
        raise SchemaError(f"question '{name}' must be an object")
    kind = spec.get("type")
    instructions = spec.get("instructions", "")
    if kind == "choice":
        criteria = spec.get("criteria") or spec.get("options") or {}
        if not isinstance(criteria, Mapping):
            raise SchemaError(f"question '{name}': choice criteria must be an object")
        question: Question = Choice(
            instructions=instructions,
            options={str(k): str(v or "") for k, v in criteria.items()},
        )
    elif kind == "score":
        criteria = spec.get("criteria") or spec.get("levels") or []
        if isinstance(criteria, (str, bytes)) or not isinstance(criteria, Sequence):
            raise SchemaError(f"question '{name}': score criteria must be a list")
        question = Score(instructions=instructions, levels=[str(x) for x in criteria])
    elif kind == "noul":
        criteria = spec.get("criteria")
        question = Noul(
            instructions=instructions,
            criteria={str(k): str(v) for k, v in criteria.items()}
            if isinstance(criteria, Mapping)
            else None,
        )
    else:
        raise SchemaError(f"question '{name}': type must be choice, score or noul")
    question.validate(name)
    return question


def questions_to_payload(questions: Mapping[str, Question]) -> dict:
    if not questions:
        raise SchemaError("at least one question is required")
    payload = {}
    for name, question in questions.items():
        question.validate(name)
        payload[name] = question.to_payload()
    return payload
