"""P2 — interfaces and a record format only. Nothing here runs by default.

Three pieces of later work are named here so they have a place to land, and so
nothing in the product pretends they exist:

* **Replanning** when the state changes during a run (new evidence overturns a
  conclusion, a step fails for good). Today a run plans once; a revision is a
  new operation on one step, and a rewritten whole is marked Needs sync.
* **Calibration by domain**: capability levels and thresholds per kind of task,
  learned from recorded outcomes. Today they are configuration, labelled as
  uncalibrated.
* **Online comparison**: the same harness, tools and writing model, with only
  the decision engine swapped. Today there is a record format and an offline
  builder that reads recorded runs; there is no paid benchmark.

The comparison record below is deliberately plain so it can be filled from
`<workspace>/.jevia/runs/*.jsonl` without re-running anything. Unknown costs
stay None and are never averaged as zero.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Protocol

RECORD_VERSION = "jevia.eval/v1"


@dataclass
class RunRecord:
    """One run, summarised for comparison. Built from a recorded run."""

    run_id: str
    decision_engine: str                     # e.g. "jev", "cheap-llm", "rules-only"
    status: str                              # completed | needs_review | cancelled | failed | partial
    jev_calls: int = 0
    llm_calls: int = 0
    judgements: int = 0
    known_cost_usd: float = 0.0
    unknown_cost_calls: int = 0              # these make known_cost_usd a floor, not a total
    elapsed_ms: Optional[int] = None
    needs_review: List[str] = field(default_factory=list)
    # Filled by a person or a checker afterwards; never guessed here.
    usable: Optional[bool] = None
    critical_errors: Optional[int] = None
    rework_cost_usd: Optional[float] = None
    version: str = RECORD_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def record_from_events(events: Iterable[dict], decision_engine: str = "jev") -> Optional[RunRecord]:
    """Summarise one recorded run. Offline: reads events, calls nothing."""
    events = list(events)
    if not events:
        return None
    done = next((e for e in reversed(events) if e.get("kind") == "run.completed"), None)
    cancelled = any(e.get("kind") == "run.cancelled" for e in events)
    calls = {}
    for e in events:
        usage = e.get("usage") or {}
        if usage.get("call_id"):
            prev = calls.get(usage["call_id"])
            if prev is None or (prev.get("cost_usd") is None and usage.get("cost_usd") is not None):
                calls[usage["call_id"]] = usage
    known = [c for c in calls.values() if c.get("cost_usd") is not None and c.get("cost_source") != "unknown"]
    payload = (done or {}).get("payload") or {}
    return RunRecord(
        run_id=str(events[0].get("run_id") or ""),
        decision_engine=decision_engine,
        status="cancelled" if cancelled else str(payload.get("status") or "partial"),
        jev_calls=int(payload.get("jev_calls") or 0),
        llm_calls=int(payload.get("llm_calls") or 0),
        judgements=int(payload.get("jev_questions") or 0),
        known_cost_usd=round(sum(float(c["cost_usd"]) for c in known), 8),
        unknown_cost_calls=len(calls) - len(known),
        elapsed_ms=(done or {}).get("elapsed_ms"),
        needs_review=list(payload.get("needs_review") or []),
    )


def records_from_workspace(workspace: Path, decision_engine: str = "jev") -> List[RunRecord]:
    """Every recorded run in a workspace, summarised. Reads files only."""
    folder = Path(workspace) / ".jevia" / "runs"
    out: List[RunRecord] = []
    for path in sorted(folder.glob("*.jsonl")) if folder.is_dir() else []:
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        record = record_from_events([e for e in lines if e.get("operation_id") in (None, "initial")],
                                    decision_engine)
        if record:
            out.append(record)
    return out


# --------------------------------------------------------------------------- #
# Interfaces for later work. Not wired in; not implemented.
# --------------------------------------------------------------------------- #

class Replanner(Protocol):
    """TODO(P2): decide, mid-run, that the plan no longer fits the state.

    Must emit its own decision events (a new operation, a new attempt) and may
    only add or re-run steps whose inputs changed; it must never silently drop
    a step the user already saw.
    """

    def should_replan(self, run_events: List[dict]) -> Optional[str]: ...


class CapabilityCalibration(Protocol):
    """TODO(P2): per-domain capability levels and thresholds from outcomes.

    Until records show a benefit, thresholds stay configuration and the UI
    keeps calling them uncalibrated.
    """

    def thresholds_for(self, domain: str) -> dict: ...


class DecisionEngine(Protocol):
    """TODO(P2): a swappable decision engine for like-for-like comparison.

    Same batch shape as Jev (typed questions in, typed answers out), so the
    harness, tools and writing model stay fixed and only this varies.
    """

    def decide(self, state, questions: dict, *, purpose: str = "decide", observer=None,
               targets=None): ...
