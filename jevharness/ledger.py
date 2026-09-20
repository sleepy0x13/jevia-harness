"""Cost and latency accounting, plus an honest comparison.

Two kinds of number live here and they are kept apart on purpose:

* **measured** — what actually happened. Calls, tokens, dollars, milliseconds.
* **counterfactual** — what the same task would plausibly have cost sent
  straight to the best model in the roster. It is an estimate, it is labelled
  as one, and it is computed from measured token counts rather than invented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .providers import Call, Usage
from .roster import ModelSpec, Roster

# What an LLM typically spends answering one typed question it was not built
# for: it restates the material, reasons, then commits. Used only for the
# labelled counterfactual.
BASELINE_OUTPUT_TOKENS_PER_DECISION = 120


@dataclass
class Ledger:
    """Everything that was spent, and on what."""

    calls: List[Call] = field(default_factory=list)

    def add(self, call: Call) -> Call:
        self.calls.append(call)
        return call

    # -- measured ----------------------------------------------------------- #

    @property
    def total_cost(self) -> float:
        return sum(c.usage.cost for c in self.calls)

    @property
    def total_latency_ms(self) -> int:
        return sum(c.latency_ms for c in self.calls)

    def by_engine(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for call in self.calls:
            bucket = out.setdefault(
                call.engine,
                {"calls": 0, "latency_ms": 0, "usage": Usage()},
            )
            bucket["calls"] += 1
            bucket["latency_ms"] += call.latency_ms
            bucket["usage"].add(call.usage)
        return {
            engine: {
                "calls": b["calls"],
                "latency_ms": b["latency_ms"],
                "usage": b["usage"].to_dict(),
            }
            for engine, b in out.items()
        }

    def input_tokens(self) -> int:
        return sum(c.usage.input_tokens for c in self.calls)

    def output_tokens(self) -> int:
        return sum(c.usage.output_tokens for c in self.calls)

    def unique(self) -> List[Call]:
        """Each charged request once, however often it was recorded."""
        seen: Dict[str, Call] = {}
        for call in self.calls:
            seen.setdefault(call.call_id, call)
        return list(seen.values())

    def cost_summary(self) -> dict:
        """Known spend, and how many requests have no known cost.

        An unknown cost is never counted as zero: the total is a floor, and
        the number of unknowns says how far below the truth it may be.
        """
        calls = self.unique()
        known = [c for c in calls if c.usage.known]
        sources: Dict[str, int] = {}
        for call in calls:
            sources[call.usage.source] = sources.get(call.usage.source, 0) + 1
        return {
            "known_usd": round(sum(c.usage.cost for c in known), 8),
            "unknown_calls": len(calls) - len(known),
            "sources": sources,
            "free_at_configured_rate": sum(
                1 for c in known if c.usage.cost == 0 and c.engine == "llm"),
        }

    def to_dict(self) -> dict:
        return {
            "calls": [c.to_dict() for c in self.calls],
            "total_cost": round(self.total_cost, 8),
            "total_latency_ms": self.total_latency_ms,
            "by_engine": self.by_engine(),
            "cost": self.cost_summary(),
        }


def _cost(spec: ModelSpec, input_tokens: int, output_tokens: int) -> float:
    return (
        spec.price_in * input_tokens / 1_000_000.0
        + spec.price_out * output_tokens / 1_000_000.0
    )


CHEAPER, DEARER, EVEN = "cheaper", "dearer", "even"


@dataclass
class Comparison:
    """One labelled counterfactual: the same task, sent to the best model.

    This is allowed to report that the harness cost *more*. On the first task of
    a new shape it often does, because compiling the plan is an LLM call — and
    then that plan is cached and every later task of the shape is cheap. Hiding
    that would make the number a slogan instead of a measurement.
    """

    baseline_model: Optional[str]
    baseline_cost: float
    actual_cost: float
    plan_cost: float
    decisions_answered: int
    jev_calls: int
    llm_calls: int
    llm_calls_avoided: int
    generation_skipped: bool
    plan_reused: bool

    @property
    def saved(self) -> float:
        return self.baseline_cost - self.actual_cost

    @property
    def ratio(self) -> Optional[float]:
        if self.actual_cost <= 0:
            return None
        return self.baseline_cost / self.actual_cost

    @property
    def verdict(self) -> str:
        if self.actual_cost <= 0:
            return CHEAPER
        if self.baseline_cost > self.actual_cost * 1.05:
            return CHEAPER
        if self.actual_cost > self.baseline_cost * 1.05:
            return DEARER
        return EVEN

    @property
    def steady_state_cost(self) -> float:
        """What this same task costs once the plan is cached."""
        return max(self.actual_cost - self.plan_cost, 0.0)

    def note(self) -> str:
        if self.verdict == DEARER and self.plan_cost > 0:
            return (
                "Dearer this once: compiling the plan was an LLM call. The plan "
                "is now cached, so the next task of this shape costs about "
                f"{_fmt(self.steady_state_cost)}."
            )
        if self.verdict == DEARER:
            return "Dearer on this task: the work genuinely needed the model."
        if self.verdict == EVEN:
            return "About the same as going straight to the model."
        if self.plan_reused:
            return "Cheaper, on a plan that was already paid for."
        return "Cheaper even including the planning call."

    def to_dict(self) -> dict:
        return {
            "baseline_model": self.baseline_model,
            "baseline_cost": round(self.baseline_cost, 8),
            "actual_cost": round(self.actual_cost, 8),
            "plan_cost": round(self.plan_cost, 8),
            "steady_state_cost": round(self.steady_state_cost, 8),
            "estimated_saving": round(self.saved, 8),
            "ratio": round(self.ratio, 1) if self.ratio else None,
            "verdict": self.verdict,
            "note": self.note(),
            "decisions_answered": self.decisions_answered,
            "jev_calls": self.jev_calls,
            "llm_calls": self.llm_calls,
            "llm_calls_avoided": self.llm_calls_avoided,
            "generation_skipped": self.generation_skipped,
            "plan_reused": self.plan_reused,
            # A modelled alternative, not a measurement. The interface keeps it
            # folded away with its assumptions; it is never a headline.
            "counterfactual": True,
            "assumptions": [
                "the strongest configured model would have answered every typed decision "
                f"as its own call, writing about {BASELINE_OUTPUT_TOKENS_PER_DECISION} tokens each",
                "input tokens are the ones this run actually used",
                "configured list prices; no retries, caching or failures",
            ],
            "basis": (
                "Estimate. Prices the measured input tokens, plus the measured "
                "generation output (or a typical answer per decision where none "
                "was generated), against the strongest model in the roster — "
                "what the task would cost with no harness in front of it."
            ),
        }


def _fmt(value: float) -> str:
    if value <= 0:
        return "$0"
    if value < 0.000001:
        return "<$0.000001"
    return f"${value:.6f}"


def compare(
    ledger: Ledger,
    roster: Roster,
    *,
    decisions_answered: int,
    generation_skipped: bool,
    plan_reused: bool,
) -> Comparison:
    """Price the same work against the strongest available model."""
    candidates = roster.available()
    best = max(candidates, key=lambda m: (m.capability, m.blended_price)) if candidates else None

    jev_calls = sum(1 for c in ledger.calls if c.engine == "jev")
    llm_calls = sum(1 for c in ledger.calls if c.engine == "llm")

    # Without a harness, each typed decision is its own LLM round trip, and the
    # generation is one more.
    baseline_llm_calls = decisions_answered + (0 if generation_skipped else 1)
    avoided = max(baseline_llm_calls - llm_calls, 0)

    baseline_cost = 0.0
    if best is not None:
        measured_in = max(ledger.input_tokens(), 1)
        measured_out = ledger.output_tokens()
        baseline_out = (
            decisions_answered * BASELINE_OUTPUT_TOKENS_PER_DECISION + measured_out
        )
        baseline_cost = _cost(best, measured_in, baseline_out)

    return Comparison(
        baseline_model=(best.label or best.model) if best else None,
        baseline_cost=baseline_cost,
        actual_cost=ledger.total_cost,
        plan_cost=sum(c.usage.cost for c in ledger.calls if c.purpose == "compile"),
        decisions_answered=decisions_answered,
        jev_calls=jev_calls,
        llm_calls=llm_calls,
        llm_calls_avoided=avoided,
        generation_skipped=generation_skipped,
        plan_reused=plan_reused,
    )
