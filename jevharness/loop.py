"""The agent loop: a turn is steps, a step is one request and the tools it runs.

Until now this harness could only write. It planned once, sent the plan to a
model, and printed whatever came back — so any task that needed a look at a
result before deciding the next move was out of reach: read a file to find out
which file to read, run something and fix what broke, check the draft against
the source. Those are most real tasks.

A **step** is one model request plus the tools it asks for. A **turn** holds
steps until nothing is owed: the model stops asking for tools, or a budget
runs out. Offering no tools makes the loop a single request, which is exactly
what generation used to be — one path, not two.

What stays JEVia's own: Jev still sized the model before the loop began, still
judges every action with a side effect before it runs, and the tools' own hard
rules still apply after that. A calibrated yes is permission to consider an
action, never permission to leave the workspace.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from .errors import ProviderError
from .events import Cancelled, call_usage, short_reason
from .providers import Call, ToolCall, Usage
from .session import ASSISTANT, TOOL, Log

# A step that asks for nothing is the end of a turn. These bound the rest.
MAX_STEPS = 12
MAX_TOOL_CALLS = 40
MAX_RESULT_CHARS = 20_000
REPEAT_LIMIT = 3          # the same refused call, sent again, is not going to work


@dataclass
class Budget:
    """What one turn may spend before it has to stop and say so."""

    steps: int = MAX_STEPS
    tool_calls: int = MAX_TOOL_CALLS
    max_tokens: Optional[int] = None          # per request, for the answer
    context_tokens: int = 60_000              # when to summarise the older turns

    def describe(self, steps: int, calls: int) -> str:
        return f"{steps}/{self.steps} steps, {calls}/{self.tool_calls} tool calls"


@dataclass
class Outcome:
    """How a turn ended, and what it produced."""

    text: str = ""
    steps: int = 0
    calls: List[Call] = field(default_factory=list)
    tools_run: int = 0
    stop_reason: str = "answered"   # answered | step_budget | tool_budget | failed | cancelled
    failure: str = ""
    partial: bool = False

    @property
    def cost(self) -> float:
        return sum(c.usage.cost for c in self.calls)

    def to_dict(self) -> dict:
        return {"steps": self.steps, "tools_run": self.tools_run,
                "stop_reason": self.stop_reason, "failure": self.failure,
                "partial": self.partial, "chars": len(self.text)}


class Gate:
    """Who may run what. Jev advises; the rules decide.

    ``judge`` is the calibrated question asked of a batch of pending actions —
    the same one the harness has always asked. ``allow`` is the hard rule that
    runs afterwards and cannot be talked round: a tool that is not enabled, or
    a path outside the workspace, is refused whatever the probability said.
    """

    def __init__(self, registry, judge: Optional[Callable[[Sequence[tuple]], tuple]] = None,
                 ask: Optional[Callable[[str, ToolCall], Optional[bool]]] = None) -> None:
        self.registry = registry
        self.judge = judge
        self.ask = ask

    def screen(self, calls: Sequence[ToolCall]) -> tuple:
        """Verdicts for one step's calls, plus the Jev call that judged them."""
        verdicts: Dict[str, dict] = {}
        risky: List[ToolCall] = []
        for call in calls:
            tool = self.registry.get(call.name) if self.registry else None
            if tool is None or tool not in self.registry.available():
                verdicts[call.id] = {"ok": False, "reason": "unknown_tool"}
            elif call.malformed:
                verdicts[call.id] = {"ok": False, "reason": "malformed_arguments"}
            elif tool.side_effects:
                risky.append(call)
            else:
                verdicts[call.id] = {"ok": True, "reason": "read_only"}
        judged = None
        if risky and self.judge is not None:
            results, judged = self.judge([(c.name, c.arguments) for c in risky])
            for call, verdict in zip(risky, results):
                # A judge answers (ok, probability) and may add its own reason.
                ok, probability = bool(verdict[0]), verdict[1]
                reason = verdict[2] if len(verdict) > 2 else (
                    "gate" if ok else "gate_refused")
                verdicts[call.id] = {"ok": ok, "probability": probability,
                                     "reason": reason}
        elif risky:
            for call in risky:
                verdicts[call.id] = {"ok": False, "reason": "no_gate"}
        # The user has the last word on anything the gate let through, when a
        # deployment wired an approval channel.
        if self.ask is not None:
            for call in calls:
                verdict = verdicts.get(call.id) or {}
                if not verdict.get("ok"):
                    continue
                tool = self.registry.get(call.name)
                if tool is not None and tool.side_effects:
                    answer = self.ask(call.name, call)
                    if answer is False:
                        verdicts[call.id] = {"ok": False, "reason": "user_refused"}
                    elif answer is True:
                        verdict["reason"] = "user_approved"
        return verdicts, judged


class AgentLoop:
    """One turn: request, tools, request again, until nothing is owed."""

    def __init__(self, *, client, model: str, log: Log, tools=None, gate: Optional[Gate] = None,
                 budget: Optional[Budget] = None, events=None, stage: str = "generation",
                 subtask_id: Optional[str] = None, purpose: str = "generate",
                 temperature: float = 0.2, thinking: bool = False,
                 compactor: Optional[Callable[[Log, int], Optional[str]]] = None) -> None:
        self.client = client
        self.model = model
        self.log = log
        self.tools = tools
        self.gate = gate
        self.budget = budget or Budget()
        self.events = events
        self.stage = stage
        self.subtask_id = subtask_id
        self.purpose = purpose
        self.temperature = temperature
        self.thinking = thinking
        self.compactor = compactor
        self._refused: Dict[str, int] = {}

    # -- the turn ----------------------------------------------------------- #

    def run(self) -> Iterator[dict]:
        """Yield {'delta'}, {'tool'}, {'result'} … and one final {'done', outcome}."""
        outcome = Outcome()
        schemas = self.tools.schemas() if self.tools is not None else []
        for step in range(1, self.budget.steps + 1):
            self._check()
            yield from self._compact_if_needed()
            outcome.steps = step
            self._emit("step.started", "llm", {"step": step, "tools_offered": len(schemas),
                                               "context_tokens": self.log.tokens()})
            text, call, calls, failure = yield from self._request(schemas, step)
            if call is not None:
                outcome.calls.append(call)
            if failure:
                outcome.text = (outcome.text + text) if text else outcome.text
                outcome.stop_reason, outcome.failure = "failed", failure
                outcome.partial = bool(outcome.text)
                self._emit("step.completed", "llm", {"step": step, "outcome": "failed",
                                                     "detail": failure})
                break
            if text:
                outcome.text = text
            self.log.append(ASSISTANT, text, calls=calls)
            if not calls:
                self._emit("step.completed", "llm", {"step": step, "outcome": "answered",
                                                     "chars": len(text)})
                break
            if outcome.tools_run + len(calls) > self.budget.tool_calls:
                outcome.stop_reason = "tool_budget"
                self._refuse_all(calls, "the run's tool budget is used up")
                self._emit("step.completed", "llm", {"step": step, "outcome": "tool_budget"})
                break
            ran, repeated = yield from self._run_tools(calls)
            outcome.tools_run += ran
            self._emit("step.completed", "llm", {"step": step, "outcome": "tools",
                                                 "tools": len(calls)})
            if repeated == len(calls) and max(self._refused.values()) >= REPEAT_LIMIT:
                outcome.stop_reason = "repeating"
                break
        else:
            outcome.stop_reason = "step_budget"
        yield {"done": True, "outcome": outcome}

    # -- one request -------------------------------------------------------- #

    def _request(self, schemas: Sequence[dict], step: int):
        """Stream one model request. Returns (text, call, tool_calls, failure)."""
        pieces: List[str] = []
        usage: dict = {}
        model_used, call_id, tool_calls = self.model, "", []
        started = time.perf_counter()
        try:
            for event in self.client.stream(
                    self.log.messages(), model=self.model, max_tokens=self.budget.max_tokens,
                    temperature=self.temperature, thinking=self.thinking, purpose=self.purpose,
                    tools=list(schemas) or None):
                if self.events is not None and self.events.cancel.is_set():
                    raise Cancelled("stopped while the model was writing")
                if "delta" in event:
                    pieces.append(event["delta"])
                    yield {"delta": event["delta"]}
                elif event.get("done"):
                    usage = event.get("usage") or {}
                    model_used = event.get("model") or model_used
                    call_id = event.get("call_id") or ""
                    tool_calls = [ToolCall.from_dict(c) for c in event.get("tool_calls") or []]
        except Cancelled:
            raise
        except ProviderError as exc:
            return "".join(pieces), None, [], short_reason(exc)
        call = Call(engine="llm", purpose=self.purpose, model=model_used,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    usage=Usage.from_dict(usage), calls=tool_calls)
        if call_id:
            call.call_id = call_id
        return "".join(pieces), call, tool_calls, ""

    # -- the tools a step asked for ----------------------------------------- #

    def _run_tools(self, calls: Sequence[ToolCall]):
        """Screen a step's calls and run what survives. Returns (ran, repeated).

        A call the gate has already refused is not judged twice. Paying Jev to
        give the same verdict teaches the model nothing and costs a round trip,
        so the repeat is answered straight away and counted.
        """
        fresh = [c for c in calls if _signature(c) not in self._refused]
        verdicts: Dict[str, dict] = {}
        judged = None
        if fresh:
            verdicts, judged = self.gate.screen(fresh) if self.gate else (
                {c.id: {"ok": False, "reason": "no_tools"} for c in fresh}, None)
        if judged is not None:
            self._emit("policy.applied", "policy", {
                "rule": "tools.gate_allow_at_or_above/v1", "rule_version": "v1",
                "inputs": {"asked": len(fresh)},
                "action": f"{sum(1 for v in verdicts.values() if v.get('ok'))} of {len(fresh)} allowed"},
                usage=call_usage(judged))
        repeated = 0
        for call in calls:
            if _signature(call) in self._refused:
                verdicts[call.id] = {"ok": False, "reason": "already_refused"}
                repeated += 1
        ran = 0
        for call in calls:
            self._check()
            verdict = verdicts.get(call.id) or {"ok": False, "reason": "unknown_tool"}
            self._emit("tool.called", "tool", {"tool": call.name, "call_id": call.id,
                                               "arguments": call.arguments,
                                               "allowed": bool(verdict.get("ok")),
                                               "reason": verdict.get("reason")})
            yield {"tool": {"name": call.name, "arguments": call.arguments,
                            "allowed": bool(verdict.get("ok")), "reason": verdict.get("reason")}}
            if not verdict.get("ok"):
                signature = _signature(call)
                self._refused[signature] = self._refused.get(signature, 0) + 1
                self._answer(call, False, _refusal(call, verdict),
                             detail=str(verdict.get("reason") or ""))
                continue
            started = time.perf_counter()
            try:
                result = self.tools.run(call.name, call.arguments)
                ok, output, detail = result.ok, result.output, result.detail
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed tool is a result, not a crash
                ok, output, detail = False, "", short_reason(exc)
            ran += 1
            text = (output or detail or ("done" if ok else "failed"))[:MAX_RESULT_CHARS]
            self._answer(call, ok, text, detail=detail,
                         ms=int((time.perf_counter() - started) * 1000))
        return ran, repeated

    def _answer(self, call: ToolCall, ok: bool, text: str, detail: str = "", ms: int = 0) -> None:
        """Hand a result back to the model, and record it once."""
        self.log.append(TOOL, text, call_id=call.id, name=call.name, ok=ok)
        self._emit("tool.result", "tool", {"tool": call.name, "call_id": call.id, "ok": ok,
                                           "detail": detail[:400], "chars": len(text), "ms": ms})

    def _refuse_all(self, calls: Sequence[ToolCall], why: str) -> None:
        for call in calls:
            self._answer(call, False, why)

    # -- keeping the context inside its budget ------------------------------- #

    def _compact_if_needed(self) -> Iterator[dict]:
        """Summarise the older exchanges once the context outgrows its budget."""
        if self.compactor is None or self.log.tokens() <= self.budget.context_tokens:
            return
        before = self.log.tokens()
        replacing = self.log.compactable()
        if not replacing:
            return
        summary = self.compactor(self.log, before)
        entry = self.log.compact(summary or "", replacing) if summary else None
        if entry is None:
            return
        self._emit("context.compacted", "policy", {
            "before_tokens": before, "after_tokens": self.log.tokens(),
            "shadowed": len(replacing), "summary_chars": len(entry.text)})
        yield {"compacted": {"before": before, "after": self.log.tokens(),
                             "shadowed": len(replacing)}}

    # -- plumbing ------------------------------------------------------------ #

    def _check(self) -> None:
        if self.events is not None:
            self.events.check()

    def _emit(self, kind: str, actor: str, payload: dict, usage: Optional[dict] = None) -> None:
        if self.events is not None:
            self.events.emit(kind, actor, self.stage, payload,
                             subtask_id=self.subtask_id, usage=usage)


def _refusal(call: ToolCall, verdict: Mapping) -> str:
    """What the model is told when a call does not run. Plain, and actionable."""
    reason = verdict.get("reason")
    if reason == "unknown_tool":
        return f"There is no tool called {call.name!r} available in this run."
    if reason == "malformed_arguments":
        return (f"The arguments for {call.name} were not valid JSON, so nothing ran. "
                "Send them again as a JSON object.")
    if reason == "user_refused":
        return f"The user refused this {call.name} call. Do not try it again; work another way."
    if reason == "no_gate":
        return f"{call.name} changes things outside this conversation and could not be checked, so it did not run."
    if reason == "already_refused":
        return (f"This exact {call.name} call was refused earlier in this turn, so it was "
                "not sent to the gate again. Change it, or finish without it.")
    if reason == "gate_unavailable":
        return (f"{call.name} could not be checked, so it did not run. Finish the answer "
                "without it and say plainly that it did not run.")
    probability = verdict.get("probability")
    score = f" (p={probability:.2f})" if isinstance(probability, (int, float)) else ""
    if reason == "gate_unsafe":
        return (f"{call.name} did not run{score}: it reaches outside the workspace, sends "
                "the user's data away, or destroys material they supplied. Do not retry it "
                "in another form. Finish without it and say so.")
    return (f"{call.name} did not run{score}: the user's own instruction does not cover it. "
            "Sending the same call again gets the same answer. Either aim it at what the "
            "user actually named, or finish the answer without it and say plainly that it "
            "did not run.")


def _signature(call: ToolCall) -> str:
    """Identifies a call by what it would do, so a repeat of it is recognisable."""
    try:
        arguments = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
    except TypeError:
        arguments = repr(call.arguments)
    return f"{call.name}:{arguments}"
