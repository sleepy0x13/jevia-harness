"""The task model and the plan the harness executes.

A plan is a short linear pipeline, and each arrow is a place to save money:

    decisions  ->  gate  ->  generation  ->  guard
    (1 Jev call)   (free)    (LLM, maybe)    (Jev, optional)

* ``decisions`` batches *every* independent typed question into a single Jev
  call. Ten questions cost one round trip, not ten LLM calls.
* ``gate`` is pure Python over Jev's calibrated answers: a plan can name the
  exact conditions under which generation is pointless. Model choice itself is
  not the gate's business — the roster settles that from Jev's capability rung.
* ``generation`` runs only when the task genuinely needs new text, and it
  receives the decisions as compact typed facts instead of re-deriving them.
* ``guard`` optionally screens the output with Jev rather than an LLM judge.

A full DAG would be more general and much harder to keep correct. This shape
covers batching, gating, tier routing, context trimming and screening, which is
where the real time and money go.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import SchemaError
from .questions import Answer, Question, question_from_dict

# How much conversation travels with a message, and how it is compacted.
HISTORY_BUDGET = 6000      # characters in total
HISTORY_VERBATIM = 4       # most recent messages kept whole
HISTORY_TRIM = 500         # older answers cut to this

# Words that change the wording of a request without changing its shape.
FILLER = frozenset("""
a an the this that these those my our your their it its is are was were be been
please kindly just now then also and or but so if then for to of in on at by with
from about into over under again very really quite some any all each every
me you us them i we he she they do does did can could would should will shall
here there below above following attached given provided
""".split()) | set(
    # Every CJK particle, pronoun and measure word that survives a rewording.
    "的了吗呢吧啊把被给和与及或在是有这那些个请帮我你他她它们一下上面下面如下"
    "条封份张台只件篇段句次遍点些位名段部本块道对双组批种样子过着就都也还要会能可以对于关于"
)


@dataclass(frozen=True)
class Task:
    """What the caller sent in.

    ``history`` is the conversation before this message, oldest first, as
    (role, text) pairs. Without it a follow-up like "make it shorter" reaches
    the model with no "it" to shorten.
    """

    prompt: str
    state: Any = ""
    questions: Optional[Mapping[str, Question]] = None
    history: Tuple[Tuple[str, str], ...] = ()
    # What each declared question is about, for display only: it is shown
    # beside the answer and never sent to Jev or a model.
    targets: Optional[Mapping[str, str]] = None

    def conversation(self, budget: int = HISTORY_BUDGET) -> str:
        """The history, compacted to fit a budget, without spending a call.

        The newest exchanges are kept whole. Older answers are cut to their
        opening, which is where a well-written answer puts its point. Whatever
        still does not fit is dropped from the far end, oldest first.
        """
        if not self.history:
            return ""
        rendered: List[str] = []
        used = 0
        for age, (role, text) in enumerate(reversed(self.history)):
            text = (text or "").strip()
            if not text:
                continue
            keep_whole = age < HISTORY_VERBATIM
            if not keep_whole and role == "assistant" and len(text) > HISTORY_TRIM:
                text = text[:HISTORY_TRIM].rstrip() + " …"
            block = f"{'USER' if role == 'user' else 'ASSISTANT'}: {text}"
            if used + len(block) > budget:
                if not rendered:
                    rendered.append(block[:budget].rstrip() + " …")
                break
            rendered.append(block)
            used += len(block) + 2
        return "\n\n".join(reversed(rendered))

    def validate(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise SchemaError("task 'prompt' must be a non-empty string")

    @property
    def state_text(self) -> str:
        if isinstance(self.state, str):
            return self.state
        if self.state is None:
            return ""
        import json

        return json.dumps(self.state, ensure_ascii=False, indent=2)

    def shape_key(self) -> str:
        """Identity of the task *shape*, ignoring the specific state.

        Deliberately blunt. Wording varies far more than task shape does:
        "Summarise this email", "please summarize the emails" and "summarise
        the email below" all want the same plan, and treating them as three
        tasks means paying to compile the same plan three times.

        So the key is built from content words only — lowercased, stripped of
        punctuation and numbers, with filler words and plurals removed, and
        sorted so word order does not matter. Two tasks that use the same
        content words get the same plan, which is the behaviour worth having.
        """
        kept = []
        for token in re.findall(r"[a-z]+|[\u4e00-\u9fff]", self.prompt.lower()):
            if token in FILLER:
                continue
            if len(token) > 3:
                # Spelling and number are not shape: summarise/summarize and
                # email/emails are the same request.
                if token.endswith("isation"):
                    token = token[:-7] + "ization"
                elif token.endswith("ise"):
                    token = token[:-3] + "ize"
                elif token.endswith("ises"):
                    token = token[:-4] + "ize"
                if token.endswith("s") and not token.endswith("ss"):
                    token = token[:-1]
            kept.append(token)
        skeleton = " ".join(sorted(set(kept))) or self.prompt.strip().lower()
        declared = "|".join(sorted(self.questions)) if self.questions else ""
        return hashlib.sha256(f"{skeleton}::{declared}".encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------- #
# Gate conditions: small, explicit, serializable. No eval, ever.
# --------------------------------------------------------------------------- #

YES, NO, IS, AT_LEAST, BELOW = "yes", "no", "is", "at_least", "below"
OPS = (YES, NO, IS, AT_LEAST, BELOW)


@dataclass(frozen=True)
class Condition:
    """One test against one decision answer."""

    decision: str
    op: str
    value: Any = None

    def validate(self) -> None:
        if not self.decision:
            raise SchemaError("condition needs a 'decision' name")
        if self.op not in OPS:
            raise SchemaError(f"condition op must be one of {OPS}, got {self.op!r}")
        if self.op == IS and not isinstance(self.value, str):
            raise SchemaError("condition op 'is' needs a string 'value'")
        if self.op in (AT_LEAST, BELOW) and not isinstance(self.value, (int, float)):
            raise SchemaError(f"condition op {self.op!r} needs a numeric 'value'")

    def holds(self, answers: Mapping[str, Answer]) -> bool:
        answer = answers.get(self.decision)
        if answer is None:
            return False
        if self.op in (YES, NO):
            truth = _truthiness(answer)
            if truth is None:
                return False
            return truth if self.op == YES else not truth
        if self.op == IS:
            return answer.kind == "choice" and answer.value == self.value
        if answer.kind != "score":
            return False
        if self.op == AT_LEAST:
            return answer.value >= float(self.value)
        return answer.value < float(self.value)

    def to_dict(self) -> dict:
        out = {"decision": self.decision, "op": self.op}
        if self.value is not None:
            out["value"] = self.value
        return out

    def describe(self) -> str:
        if self.op in (YES, NO):
            return f"{self.decision} is {self.op}"
        if self.op == IS:
            return f"{self.decision} == {self.value}"
        symbol = ">=" if self.op == AT_LEAST else "<"
        return f"{self.decision} {symbol} {self.value}"

    @classmethod
    def from_dict(cls, raw: Any) -> "Condition":
        if not isinstance(raw, Mapping):
            raise SchemaError("condition must be an object")
        cond = cls(
            decision=str(raw.get("decision", "")),
            op=str(raw.get("op", "")),
            value=raw.get("value"),
        )
        cond.validate()
        return cond


TRUTHY = {"yes", "true", "y", "1", "affirmative", "是", "需要", "对"}
FALSY = {"no", "false", "n", "0", "negative", "none", "否", "不需要", "不是"}


def _truthiness(answer: Answer) -> Optional[bool]:
    """Read a yes/no out of whichever primitive the plan happened to use.

    A noul carries a boolean directly. A planner will sometimes express the
    same question as a two-option choice, and a gate written against it must
    still fire — otherwise the expensive call it exists to prevent goes ahead
    anyway.
    """
    if answer.kind == "noul":
        return bool(answer.value)
    if answer.kind == "choice":
        value = str(answer.value).strip().lower()
        if value in TRUTHY:
            return True
        if value in FALSY:
            return False
    return None


@dataclass(frozen=True)
class Gate:
    """Free, deterministic control flow over Jev's answers.

    This is the plan's own veto on generation, expressed in the task's terms
    ("if the message is not a bug report, there is nothing to draft"). It is
    independent of, and checked before, the roster's capability rung.
    """

    skip_generation_when: Tuple[Condition, ...] = ()

    def should_skip_generation(self, answers: Mapping[str, Answer]) -> Optional[Condition]:
        return next((c for c in self.skip_generation_when if c.holds(answers)), None)

    def references(self) -> Sequence[str]:
        return [c.decision for c in self.skip_generation_when]

    def to_dict(self) -> dict:
        return {"skip_generation_when": [c.to_dict() for c in self.skip_generation_when]}

    @classmethod
    def from_dict(cls, raw: Any) -> "Gate":
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise SchemaError("gate must be an object")
        return cls(
            skip_generation_when=tuple(
                Condition.from_dict(c) for c in raw.get("skip_generation_when") or []
            )
        )


@dataclass(frozen=True)
class Generation:
    """The text-producing part of a task, if any survives the gate.

    ``model_id`` pins a roster model and skips capability selection; leave it
    unset to let Jev's capability rung choose the cheapest model that fits.
    """

    instruction: str
    include_state: bool = True
    max_tokens: Optional[int] = None
    model_id: Optional[str] = None

    def validate(self) -> None:
        if not self.instruction.strip():
            raise SchemaError("generation needs a non-empty 'instruction'")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise SchemaError("generation 'max_tokens' must be positive")

    def to_dict(self) -> dict:
        return {
            "instruction": self.instruction,
            "include_state": self.include_state,
            "max_tokens": self.max_tokens,
            "model_id": self.model_id,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Generation":
        if not isinstance(raw, Mapping):
            raise SchemaError("generation must be an object")
        gen = cls(
            instruction=str(raw.get("instruction", "")),
            include_state=bool(raw.get("include_state", True)),
            max_tokens=int(raw["max_tokens"]) if raw.get("max_tokens") else None,
            model_id=str(raw["model_id"]).strip() if raw.get("model_id") else None,
        )
        gen.validate()
        return gen


@dataclass(frozen=True)
class Research:
    """Go and look something up before deciding anything.

    ``query`` is optional: with nothing given, the task's own words are the
    first query. ``urls`` pins specific pages, which is what happens when the
    task simply contains a link.
    """

    rounds: int = 1
    query: str = ""
    urls: Tuple[str, ...] = ()
    top_k: int = 3

    def validate(self) -> None:
        if not 1 <= self.rounds <= 3:
            raise SchemaError("research 'rounds' must be 1..3")
        if not 1 <= self.top_k <= 8:
            raise SchemaError("research 'top_k' must be 1..8")

    def to_dict(self) -> dict:
        return {"rounds": self.rounds, "query": self.query,
                "urls": list(self.urls), "top_k": self.top_k}

    @classmethod
    def from_dict(cls, raw: Any) -> "Research":
        if not isinstance(raw, Mapping):
            raise SchemaError("research must be an object")
        urls = raw.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        spec = cls(
            rounds=int(raw.get("rounds") or 1),
            query=str(raw.get("query") or "").strip(),
            urls=tuple(str(u).strip() for u in urls if str(u).strip()),
            top_k=int(raw.get("top_k") or 3),
        )
        spec.validate()
        return spec


@dataclass(frozen=True)
class StepSpec:
    """One part of the deliverable, and who should produce it.

    ``role`` names one of the built-in workers. ``persona`` is a bespoke job
    description the planner wrote because none of them fitted — a subagent
    invented for this task. Writing it costs nothing extra: the planner was
    already compiling this plan, and the plan is cached.
    """

    title: str
    role: str = ""
    persona: str = ""

    def validate(self) -> None:
        if not self.title.strip():
            raise SchemaError("a step needs a title")
        if len(self.persona) > 1200:
            raise SchemaError("a step persona must be under 1200 characters")

    @property
    def staffed(self) -> bool:
        """True when the plan already said who does this."""
        return bool(self.role or self.persona)

    def to_dict(self) -> dict:
        out = {"title": self.title}
        if self.role:
            out["role"] = self.role
        if self.persona:
            out["persona"] = self.persona
        return out

    @classmethod
    def from_any(cls, raw: Any) -> "StepSpec":
        if isinstance(raw, str):
            spec = cls(title=raw.strip())
        elif isinstance(raw, Mapping):
            spec = cls(
                title=str(raw.get("title") or raw.get("name") or "").strip(),
                role=str(raw.get("role") or "").strip(),
                persona=str(raw.get("persona") or "").strip(),
            )
        else:
            raise SchemaError("a step must be a string or an object")
        spec.validate()
        return spec


# Where the user-facing answer comes from.
FROM_DECISIONS = "decisions"
FROM_GENERATION = "generation"

# How the plan was produced, cheapest first.
SOURCE_DECLARED = "declared"           # caller sent a typed schema: free, exact
SOURCE_DETERMINISTIC = "deterministic"  # recognized locally: free
SOURCE_CACHED = "cached"               # compiled earlier for this shape: free
SOURCE_COMPILED = "compiled"           # one LLM call, then cached
SOURCE_FALLBACK = "fallback"           # compilation failed: LLM only


@dataclass(frozen=True)
class Plan:
    """A complete, serializable execution plan.

    ``strategy`` is prose for humans and may be written by the planner model in
    whatever language the caller asked for. ``strategy_code`` is the stable
    identifier a UI can translate on its own; it is empty for compiled plans,
    where the description is task-specific rather than one of a fixed set.
    """

    strategy: str
    answer_from: str
    decisions: Dict[str, Question] = field(default_factory=dict)
    gate: Gate = field(default_factory=Gate)
    generation: Optional[Generation] = None
    guard: Dict[str, Question] = field(default_factory=dict)
    research: Optional[Research] = None
    # The subtasks this task breaks into. Empty means it is a single step.
    steps: Tuple[StepSpec, ...] = ()
    source: str = SOURCE_DETERMINISTIC
    notes: str = ""
    strategy_code: str = ""

    def validate(self) -> None:
        if self.answer_from not in (FROM_DECISIONS, FROM_GENERATION):
            raise SchemaError(f"answer_from must be {FROM_DECISIONS!r} or {FROM_GENERATION!r}")
        if self.answer_from == FROM_DECISIONS and not self.decisions:
            raise SchemaError("a decisions-answered plan needs at least one decision")
        if self.answer_from == FROM_GENERATION and self.generation is None:
            raise SchemaError("a generation-answered plan needs a generation step")
        for name, question in self.decisions.items():
            if name.startswith("__"):
                raise SchemaError(f"decision name {name!r} is reserved for the harness")
            question.validate(name)
        for name, question in self.guard.items():
            question.validate(f"guard.{name}")
        if self.generation is not None:
            self.generation.validate()
        unknown = [d for d in self.gate.references() if d not in self.decisions]
        if unknown:
            raise SchemaError(f"gate references unknown decisions: {sorted(set(unknown))}")
        if self.research is not None:
            self.research.validate()
        if len(self.steps) > 10:
            raise SchemaError("a plan may list at most 10 steps")
        for item in self.steps:
            item.validate()

    @property
    def needs_research(self) -> bool:
        return self.research is not None

    @property
    def uses_jev(self) -> bool:
        return bool(self.decisions) or bool(self.guard)

    @property
    def may_use_llm(self) -> bool:
        return self.generation is not None

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "strategy_code": self.strategy_code,
            "answer_from": self.answer_from,
            "source": self.source,
            "notes": self.notes,
            "decisions": {n: q.to_payload() for n, q in self.decisions.items()},
            "gate": self.gate.to_dict(),
            "generation": self.generation.to_dict() if self.generation else None,
            "guard": {n: q.to_payload() for n, q in self.guard.items()},
            "research": self.research.to_dict() if self.research else None,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, raw: Any, *, source: str = SOURCE_COMPILED) -> "Plan":
        if not isinstance(raw, Mapping):
            raise SchemaError("plan must be an object")
        decisions_raw = raw.get("decisions") or {}
        if not isinstance(decisions_raw, Mapping):
            raise SchemaError("plan 'decisions' must be an object")
        guard_raw = raw.get("guard") or {}
        if not isinstance(guard_raw, Mapping):
            raise SchemaError("plan 'guard' must be an object")
        generation_raw = raw.get("generation")
        research_raw = raw.get("research")
        steps_raw = raw.get("steps") or raw.get("todo") or []
        if isinstance(steps_raw, str):
            steps_raw = [steps_raw]
        if not isinstance(steps_raw, (list, tuple)):
            raise SchemaError("plan 'steps' must be a list")
        plan = cls(
            strategy=str(raw.get("strategy") or "compiled"),
            answer_from=str(raw.get("answer_from") or FROM_GENERATION),
            decisions={n: question_from_dict(n, s) for n, s in decisions_raw.items()},
            gate=Gate.from_dict(raw.get("gate")),
            generation=Generation.from_dict(generation_raw) if generation_raw else None,
            guard={n: question_from_dict(n, s) for n, s in guard_raw.items()},
            research=Research.from_dict(research_raw) if research_raw else None,
            steps=tuple(StepSpec.from_any(x) for x in steps_raw)[:10],
            source=str(raw.get("source") or source),
            notes=str(raw.get("notes") or ""),
            strategy_code=str(raw.get("strategy_code") or ""),
        )
        plan.validate()
        return plan
