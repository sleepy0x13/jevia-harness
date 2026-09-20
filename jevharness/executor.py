"""Running a plan: one Jev call, then only the generation that survives it.

The order of operations is the whole optimisation:

1. Every typed question in the plan — plus the capability question that sizes
   the model — goes out in **one** Jev call. N judgements, one round trip.
2. The gate and the capability rung are then pure arithmetic over calibrated
   numbers, so skipping the LLM costs nothing to decide.
3. If generation survives, it runs on the cheapest model that clears the rung,
   and it is handed Jev's answers as established facts so it does not spend
   output tokens re-deriving them.
4. Anything Jev was genuinely unsure about is escalated rather than guessed.

``stream`` is the real implementation and ``run`` consumes it, so the two can
never drift apart.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from . import roles
from .agent import (GATE_ALLOW, MAX_STATE_CHARS, Evidence, Researcher, Routing, Subtask, assess,
                    gate_actions, route)
from .config import Thresholds
from .errors import ConfigError, HarnessError, ProviderError
from .ledger import Comparison, Ledger, compare
from .appkit import (compose, extract_page, fill, find_modules, page_title, render, strip_page,
                     summary as app_summary, write_messages)
from .plan import (FROM_DECISIONS, FROM_GENERATION, SOURCE_CACHED, SOURCE_DECLARED,
                   SOURCE_DETERMINISTIC, Generation, Plan, Research, Task)
from .planner import Planner
from .policy import (Review, assembly_messages, compaction_messages, detect_language, escalation_messages,
                     facts_block, generation_messages, parse_terms, review,
                     search_terms_messages, subtask_messages, today)
from .events import RULES_VERSION, Cancelled, RunEvents
from .loop import AgentLoop, Budget, Gate, Outcome
from .session import Log
from .providers import Call, JevClient, LLMClient, Usage
from .questions import Answer, Noul, ScoreAnswer, parse_answer
from .roster import CAPABILITY_DECISION, ModelSpec, Roster, Selection, bench

from concurrent.futures import ThreadPoolExecutor, as_completed

LLMFactory = Callable[[ModelSpec], LLMClient]

FREE_PLAN_SOURCES = (SOURCE_DECLARED, SOURCE_DETERMINISTIC, SOURCE_CACHED)

GUARD_QUESTIONS = {
    "on_task": Noul(
        instructions="Does this output actually do what the task asked for?"
    ),
    "refused": Noul(
        instructions="Does this output refuse, deflect, or apologise instead of "
        "delivering the work?"
    ),
}


@dataclass
class Step:
    """One line of the audit trail.

    ``code`` and ``data`` are the API; ``detail`` is a plain-English rendering
    for callers that want one. The web UI writes its own sentence from the code
    so that the product speaks one language rather than two.
    """

    name: str
    detail: str
    engine: str = "harness"
    cost: float = 0.0
    latency_ms: int = 0
    code: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "detail": self.detail,
            "engine": self.engine,
            "cost": round(self.cost, 8),
            "latency_ms": self.latency_ms,
            "code": self.code or self.name.replace(" ", "_"),
            "data": self.data,
        }


@dataclass
class Result:
    """Everything a caller or the UI needs, including why it happened."""

    task: Task
    plan: Plan
    output: str = ""
    decisions: Dict[str, Answer] = field(default_factory=dict)
    review: Review = field(default_factory=Review)
    selection: Optional[Selection] = None
    steps: List[Step] = field(default_factory=list)
    ledger: Ledger = field(default_factory=Ledger)
    comparison: Optional[Comparison] = None
    generation_skipped: bool = False
    skip_reason: str = ""
    skip_code: str = ""
    skip_data: dict = field(default_factory=dict)
    escalated: bool = False
    guard: Dict[str, Answer] = field(default_factory=dict)
    cache: Dict[str, Any] = field(default_factory=dict)
    evidence: Optional[Evidence] = None
    skill: Optional[dict] = None
    routing: Optional[Routing] = None
    subtasks: List[Subtask] = field(default_factory=list)
    app: Optional[dict] = None
    elapsed_ms: int = 0
    run_id: str = ""
    # Why the run could not simply finish: no qualified model, a failed Jev
    # request, a stopped run, or an answer cut off mid-stream.
    needs_review: List[dict] = field(default_factory=list)
    cancelled: bool = False
    partial: bool = False
    blocked: bool = False
    # What the loop did: steps taken, tools run, why it stopped.
    turns: List[dict] = field(default_factory=list)
    todo: List[dict] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.blocked or self.needs_review:
            return "needs_review"
        if self.partial:
            return "partial"
        return "completed"

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "needs_review": self.needs_review,
            "turns": self.turns,
            "todo": self.todo,
            "cancelled": self.cancelled,
            "partial": self.partial,
            "blocked": self.blocked,
            "output": self.output,
            "plan": self.plan.to_dict(),
            "decisions": {
                name: {
                    "type": answer.kind,
                    "summary": answer.describe(),
                    "certainty": round(answer.certainty, 4),
                    **_answer_payload(answer),
                }
                for name, answer in self.decisions.items()
            },
            "review": self.review.to_dict(),
            "selection": self.selection.to_dict() if self.selection else None,
            "generation_skipped": self.generation_skipped,
            "skip_reason": self.skip_reason,
            "skip_code": self.skip_code,
            "skip_data": self.skip_data,
            "escalated": self.escalated,
            "guard": {n: a.describe() for n, a in self.guard.items()},
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "skill": self.skill,
            "cache": self.cache,
            "subtasks": [t.to_dict() for t in self.subtasks],
            "routing": {"independent": self.routing.independent,
                        "needs_assembly": self.routing.needs_assembly}
            if self.routing else None,
            "app": self.app,
            "steps": [s.to_dict() for s in self.steps],
            "ledger": self.ledger.to_dict(),
            "comparison": self.comparison.to_dict() if self.comparison else None,
            "elapsed_ms": self.elapsed_ms,
        }


def _answer_payload(answer: Answer) -> dict:
    if answer.kind == "choice":
        return {
            "value": answer.value,
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
        }
    if answer.kind == "score":
        return {
            "value": answer.value,
            "level": answer.level,
            "legend": dict(answer.legend),
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
        }
    return {"value": answer.value, "probability": answer.probability}


class Executor:
    """Owns no configuration of its own; everything is injected."""

    def __init__(
        self,
        jev: JevClient,
        llm_factory: LLMFactory,
        planner: Planner,
        roster: Roster,
        thresholds: Thresholds,
        *,
        escalate_uncertain: bool = True,
        guard_output: bool = False,
        research_enabled: bool = True,
        language: str = "en",
        tools=None,
        library=None,
        forced_skill: Optional[str] = None,
        memory=None,
        rules: str = "",
        app_mode: str = "auto",
        events: Optional[RunEvents] = None,
        routing_extra: bool = False,
        agent_loop: bool = True,
        max_steps: int = 8,
        context_tokens: int = 60_000,
        approve_changes: bool = False,
        plan_mode: bool = False,
    ) -> None:
        # One outlet per run: every event, old and new, leaves through it.
        self.events = events or RunEvents()
        self.routing_extra = routing_extra
        # A step is one request and the tools it calls; a turn holds steps
        # until nothing is owed. One step with no tools is the old behaviour.
        self.agent_loop = agent_loop
        self.max_steps = max(1, int(max_steps))
        self.context_tokens = max(4_000, int(context_tokens))
        # Ask before anything with a side effect runs, and show the plan before
        # the work starts. Both are off unless the user turned them on.
        self.approve_changes = approve_changes
        self.plan_mode = plan_mode
        # The run in flight, so work handed to a delegate is billed to it.
        self._current: Optional[Result] = None
        self.jev = jev
        self.llm_factory = llm_factory
        self.planner = planner
        self.roster = roster
        self.thresholds = thresholds
        self.escalate_uncertain = escalate_uncertain
        self.guard_output = guard_output
        self.research_enabled = research_enabled
        self.language = language
        self.reply_language = language
        self.tools = tools
        self.library = library
        self.forced_skill = forced_skill
        self.memory = memory
        self.rules = rules
        # "auto" lets Jev decide whether the deliverable is a screen; "always"
        # and "never" are the composer's explicit switch.
        self.app_mode = app_mode if app_mode in ("auto", "always", "never") else "auto"
        self.researcher = Researcher(jev, thresholds)

    # -- public ------------------------------------------------------------- #

    def run(self, task: Task) -> Result:
        """Execute to completion, discarding the incremental events."""
        result: Optional[Result] = None
        for event in self.stream(task):
            if event.get("type") == "done":
                result = event["result_object"]
        if result is None:  # pragma: no cover - stream always ends in done
            raise HarnessError("execution produced no result")
        return result

    def stream(self, task: Task) -> Iterator[dict]:
        """Yield progress events; the last one is ``done``.

        The work runs on a thread of its own and everything it reports comes
        out of one queue, so a ``batch.started`` is on the wire while the
        request it announces is still waiting for an answer.
        """
        task.validate()
        self.events.register()
        try:
            yield from self.events.run(lambda: self._guarded(task))
        finally:
            self.events.unregister()

    def _guarded(self, task: Task) -> Iterator[dict]:
        box: Dict[str, Any] = {"started": time.perf_counter()}
        ev = self.events
        ev.emit("run.started", "policy", "run",
                {"ui_language": self.language, "app_mode": self.app_mode,
                 "operation": ev.operation_id, "rules_version": RULES_VERSION})
        # Every language-model client this run makes reports to this run.
        self.planner.llm = _observed(self.planner.llm, ev.llm_observer("plan"))
        if self.planner.backup:
            client, model = self.planner.backup
            self.planner.backup = (_observed(client, ev.llm_observer("plan")), model)
        try:
            yield from self._stream(task, box)
        except Cancelled:
            result = box.get("result") or Result(task=task, plan=_app_plan(task, False))
            result.cancelled = True
            ev.emit("run.cancelled", "policy", "run",
                    {"in_flight": bool(box.get("in_flight")), "may_bill": ev.counts_llm_open() > 0,
                     "note": "no new model or tool call was started after the request"})
            yield self._finish(result, box.get("plan") or result.plan, box["started"])
        except Exception as exc:
            from .events import short_reason

            ev.emit("run.failed", "policy", "run",
                    {"reason": short_reason(exc), "code": getattr(exc, "code", "internal_error")})
            raise

    def _stream(self, task: Task, box: Dict[str, Any]) -> Iterator[dict]:
        started = box["started"]
        ev = self.events
        # Everything written back to the reader goes in the language they asked
        # in; the interface toggle only decides what the buttons say.
        self.reply_language = detect_language(task.prompt, self.language)
        if not self.roster.available():
            raise ConfigError(
                "no generation models are enabled — add at least one model"
            )

        # 1. Before anything is planned: does this need looking up, and which
        # house instructions apply? Both are typed questions, so both ride in
        # one Jev call, and the answer to the second shapes the plan itself.
        state = task.state_text
        conversation = task.conversation()
        skill = self.library.get(self.forced_skill) if self.library else None
        needs_research: Optional[bool] = None
        assess_call = None
        wants_app = False
        multi_step: Optional[bool] = None
        # Runs for any task that is not a declared typed schema. Without a
        # library it still answers the research question, which is the half the
        # run cannot do without.
        if not task.questions:
            observer = ev.observer("assess")
            assessment = assess(
                self.jev, task.prompt,
                (f"CONVERSATION SO FAR\n{conversation}\n\n" if conversation else "") + state,
                None if skill is not None else self.library,
                self.memory,
                ask_app=self.app_mode == "auto",
                observer=observer,
            )
            needs_research, picked, assess_call = (assessment.needs_research, assessment.skill,
                                                   assessment.call)
            wants_app, multi_step = assessment.wants_app, assessment.multi_step
            self._assess_policies(assessment, observer.last_batch)
            if picked and skill is None:
                skill = self.library.get(picked)
        self.planner.skill = skill
        # Several stages means a real plan: steps, each sized and staffed.
        self.planner.must_compile = bool(multi_step)
        # The plan's own words — step titles, question wording — are shown to
        # the reader too, so they follow the same rule.
        self.planner.language = self.reply_language
        if skill is not None:
            result_skill = {"id": skill.id, "name": skill.name, "source": skill.source}
        else:
            result_skill = None

        if assess_call:
            # Say at once what Jev concluded, so the wait for a plan is not blank.
            yield {"type": "assessed", "needs_research": bool(needs_research), "multi_step": bool(multi_step),
                   "app": bool(wants_app), "skill": skill.name if skill else None,
                   "skill_id": skill.id if skill else None}

        # 2. Plan. Free unless this task shape has never been seen. An app has
        # a fixed plan of its own: research if needed, then compose.
        building_app = not task.questions and (
            self.app_mode == "always" or (self.app_mode == "auto" and wants_app))
        if building_app:
            plan, compile_call = _app_plan(task, bool(needs_research and self.research_enabled)), None
        else:
            plan, compile_call = self.planner.plan(task)
        result = Result(task=task, plan=plan, run_id=ev.run_id)
        box["result"], box["plan"] = result, plan
        self._current = result
        result.skill = result_skill
        ev.emit("policy.applied", "llm" if compile_call else "policy", "plan",
                {"rule": f"plan.{plan.source}", "rule_version": "v1",
                 "inputs": {"source": plan.source, "cache_key_version": _cache_version(self.planner),
                            "steps": len(plan.steps), "decisions": len(plan.decisions)},
                 "action": plan.strategy_code or plan.strategy},
                usage=_usage_of(compile_call))
        if assess_call is None and not task.questions:
            result.needs_review.append({"reason": "jev_failed", "stage": "assess",
                                        "detail": "the first judgement did not come back"})
        if assess_call:
            result.ledger.add(assess_call)
            result.steps.append(
                Step("assess",
                     ("research needed" if needs_research else "material is enough")
                     + (f"; {skill.name}" if skill else "") + ("; app" if building_app else ""),
                     engine="jev", cost=assess_call.usage.cost,
                     latency_ms=assess_call.latency_ms, code="assess",
                     data={"needs_research": bool(needs_research),
                           "skill": skill.name if skill else None,
                           "app": building_app, "multi_step": bool(multi_step)})
            )
        if compile_call:
            result.ledger.add(compile_call)
            result.steps.append(
                Step(
                    "plan compiled",
                    f"{len(plan.decisions)} decision(s) extracted; cached for this task shape",
                    engine="llm",
                    cost=compile_call.usage.cost,
                    latency_ms=compile_call.latency_ms,
                    code="plan_compiled",
                    data={"decisions": len(plan.decisions)},
                )
            )
        else:
            result.steps.append(
                Step("plan", f"{plan.strategy} — {plan.source}, no planning call",
                     code="plan_free", data={"source": plan.source})
            )
        if (needs_research and self.research_enabled and plan.research is None
                and plan.may_use_llm and not building_app):
            plan = _with_research(plan, Research(rounds=2, top_k=3))
            result.plan = plan

        yield {"type": "plan", "plan": plan.to_dict(), "skill": result_skill}

        # Plan mode: show the plan and wait, so a wrong reading of the task
        # costs a sentence instead of a whole run.
        if self.plan_mode and not task.questions:
            for attempt in range(3):
                ev.emit("plan.proposed", "policy", "plan",
                        {"plan": _plan_lines(plan), "step_count": len(plan.steps)})
                # The run already streams from a worker thread, so the question
                # is on the wire before this blocks on the answer.
                answer = ev.ask(_plan_lines(plan), options=["Run it", "Change it"],
                                kind="plan", stage="plan")
                approved = answer is None or _approved(answer)
                ev.emit("plan.decided", "policy", "plan",
                        {"approved": approved, "feedback": "" if approved else str(answer)[:400]})
                if approved:
                    break
                self.planner.feedback = str(answer)
                plan, recompiled = self.planner.plan(task)
                result.plan, box["plan"] = plan, plan
                if recompiled:
                    result.ledger.add(recompiled)
                yield {"type": "plan", "plan": plan.to_dict(), "skill": result_skill}

        if plan.needs_research and self.research_enabled:
            events: List[dict] = []
            writer, written = self._search_terms(task)
            evidence, calls = self.researcher.run(
                task.prompt, plan.research, emit=events.append, queries=writer,
                events=ev,
            )
            calls = written + calls
            for call in calls:
                result.ledger.add(call)
            result.evidence = evidence
            found = evidence.as_text()
            if found:
                state = (state + "\n\n" if state.strip() else "") + found
            result.steps.append(
                Step(
                    "research",
                    f"{evidence.considered_hits} results, {len(evidence.sections)} of "
                    f"{evidence.considered_sections} sections kept",
                    engine="jev",
                    cost=sum(c.usage.cost for c in calls),
                    latency_ms=sum(c.latency_ms for c in calls),
                    code="research",
                    data={"hits": evidence.considered_hits,
                          "sections": evidence.considered_sections,
                          "kept": len(evidence.sections),
                          "rounds": evidence.rounds,
                          "sources": len(evidence.sources()),
                          "sufficiency": evidence.sufficiency,
                          "stop_reason": evidence.stop_reason},
                )
            )
            for event in events:
                yield event
            yield {"type": "evidence", "evidence": evidence.to_dict()}

        if building_app:
            yield from self._build_app(task, result, state)
            yield self._finish(result, plan, started)
            return

        # 3. Route every subtask in one call: how hard, who does it, and which
        # of them the material already covers.
        if plan.steps and plan.may_use_llm:
            observer = ev.observer("routing")
            routing, call = route(
                self.jev, self.roster, plan.steps, task.prompt,
                result.evidence or Evidence(), observer=observer,
                extra_dimensions=self.routing_extra,
            )
            result.routing = routing
            result.subtasks = routing.subtasks
            self._routing_events(routing, observer.last_batch, result)
            if call:
                result.ledger.add(call)
                models = {t.model_label for t in routing.subtasks if t.model_label}
                result.steps.append(
                    Step("route",
                         f"{len(routing.subtasks)} steps across {len(models)} model(s)",
                         engine="jev", cost=call.usage.cost, latency_ms=call.latency_ms,
                         code="route",
                         data={"steps": len(routing.subtasks), "models": len(models),
                               "bespoke": sum(1 for t in routing.subtasks if t.persona),
                               "skipped": sum(1 for t in routing.subtasks if t.already_done),
                               "web": sum(1 for t in routing.subtasks if t.needs_web),
                               "independent": routing.independent})
                )
            yield {"type": "subtasks", "subtasks": [t.to_dict() for t in routing.subtasks],
                   "routing": routing.to_dict()}

        # 4. One Jev call for every decision, plus the model sizing.
        capability_answer = self._run_decisions(task, plan, result, state)
        if result.decisions:
            yield {
                "type": "decisions",
                "decisions": result.to_dict()["decisions"],
                "review": result.review.to_dict(),
            }

        # 3. Decide whether any prose is owed, and by whom.
        selection = self._choose(plan, result, capability_answer)
        result.selection = selection
        yield {
            "type": "selection",
            "selection": selection.to_dict() if selection else None,
            "generation_skipped": result.generation_skipped,
            "skip_reason": result.skip_reason,
        }

        # 4. Do the work: one pass, or one pass per subtask on its own model.
        if result.blocked:
            result.output = self._render_decisions(result)
        elif result.generation_skipped or selection is None or selection.jev_only:
            result.output = self._render_decisions(result)
        elif result.subtasks:
            for event in self._run_subtasks(task, plan, result, state):
                yield event
        else:
            for event in self._generate(task, plan, result, selection, state):
                if event.get("type") == "delta":
                    yield event
                # non-delta events are internal bookkeeping

        # A reply that is really a web page is run, not read: nobody asked
        # for the code, they asked for the thing.
        page = extract_page(result.output) if not result.app else ""
        if page:
            self._adopt_page(result, page, task.prompt)
            yield {"type": "app", "app": result.app}

        # 5. Settle anything Jev flagged as a coin flip.
        if self.escalate_uncertain and result.review.uncertain and plan.answer_from == FROM_DECISIONS \
                and not result.blocked:
            self._escalate(task, plan, result)
            yield {"type": "escalated", "note": result.steps[-1].detail}

        # 6. Check the work, and have one more go if it missed. Jev does the
        # checking, so verifying every answer costs about two hundredths of a
        # cent; only a failed check costs a second generation.
        if self.guard_output and result.output and not result.generation_skipped and not result.app:
            self._guard(result)
            # When the web gave nothing, an answer that says so is the right
            # answer; pushing for another go is how a model starts inventing.
            came_back_empty = bool(result.evidence and result.evidence.queries
                                   and result.evidence.empty)
            if self._missed(result) and not result.subtasks and result.selection \
                    and result.selection.model is not None and not came_back_empty:
                self._retry(task, plan, result, state)
                yield {"type": "retried", "output": result.output}

        yield self._finish(result, plan, started)

    def _finish(self, result: Result, plan: Plan, started: float) -> dict:
        # Anything this run started in the background stops with it; nothing
        # is left running once nobody is watching its output.
        if self.tools is not None:
            self.tools.context.jobs.stop_all()
        result.cache = self.planner.cache.stats()
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        result.run_id = self.events.run_id
        result.comparison = compare(
            result.ledger,
            self.roster,
            decisions_answered=len(result.decisions),
            generation_skipped=result.generation_skipped,
            plan_reused=plan.source in FREE_PLAN_SOURCES,
        )
        counts = self.events.counts
        known = [c for c in result.ledger.calls if c.usage.known]
        self.events.emit("run.completed", "policy", "run", {
            "status": result.status,
            # Every language-model request started, answered or not: a failed
            # one may still be billed.
            "llm_calls": counts.get("llm.started", 0),
            "jev_calls": counts.get("batch.started", 0),
            "jev_questions": counts.get("jev_questions", 0),
            "tool_calls": counts.get("tool", 0),
            "generation": "skipped" if result.generation_skipped else
            "blocked" if result.blocked else "ran",
            "needs_review": [r.get("reason") for r in result.needs_review],
            "cost": {"known_usd": round(sum(c.usage.cost for c in known), 8),
                     "unknown_calls": len(result.ledger.calls) - len(known)
                     + counts.get("llm.failed", 0) + counts.get("batch.failed", 0)},
        })
        return {"type": "done", "result": result.to_dict(), "result_object": result}

    # -- what the events say ------------------------------------------------ #

    def _assess_policies(self, assessment, batch_id: Optional[str]) -> None:
        """Each adoption from the first batch, with the number and the rule."""
        ev = self.events
        if assessment.failed:
            ev.emit("policy.review_required", "policy", "assess",
                    {"reason": "jev_failed", "blocking": False,
                     "detail": "the first judgement failed; the run continues without it"},
                    batch_id=batch_id)
            return
        answers = assessment.answers or {}
        from .agent import APP_ABOVE, MULTI_STEP, NEEDS_RESEARCH, PICK_SKILL, WANTS_APP

        def p_yes(name: str) -> Optional[float]:
            raw = answers.get(name)
            return float(raw.get("noul")) if isinstance(raw, dict) and raw.get("noul") is not None else None

        rows = [
            (NEEDS_RESEARCH, "assess.research_at_or_above/v1", 0.5,
             "search the web" if assessment.needs_research else "use the material given"),
            (MULTI_STEP, "assess.multi_step_at_or_above/v1", 0.5,
             "compile a plan with steps" if assessment.multi_step else "no forced split"),
            (WANTS_APP, "assess.app_at_or_above/v1", APP_ABOVE,
             "build an app" if assessment.wants_app else "answer in text"),
        ]
        for name, rule, threshold, action in rows:
            if name not in answers:
                continue
            ev.emit("policy.applied", "policy", "assess",
                    {"rule": rule, "rule_version": RULES_VERSION,
                     "inputs": {"probability_yes": p_yes(name), "threshold": threshold},
                     "action": action}, batch_id=batch_id, question_id=name)
        if PICK_SKILL in answers:
            ev.emit("policy.applied", "policy", "assess",
                    {"rule": "assess.skill_confidence_at_or_above/v1", "rule_version": RULES_VERSION,
                     "inputs": {"threshold": 0.5,
                                "confidence": (answers[PICK_SKILL] or {}).get("confidence")},
                     "action": f"apply {assessment.skill}" if assessment.skill else "no skill"},
                    batch_id=batch_id, question_id=PICK_SKILL)

    def _routing_events(self, routing: Routing, batch_id: Optional[str], result: Result) -> None:
        """Jev's reading of each step, then what the price table did with it.

        All steps got their answers from one call, so they are reported
        together, and each only on its own row.
        """
        ev = self.events
        for task in routing.subtasks:
            a = task.assessment or {}
            if not a.get("jev_failed"):
                ev.emit("routing.assessed", "jev", "routing", {"title": task.title, **a},
                        batch_id=batch_id, subtask_id=task.id)
            sel = task.selection or {}
            if task.blocked:
                ev.emit("routing.unavailable", "policy", "routing",
                        {"title": task.title, "required_capability": sel.get("required_capability"),
                         "best_available": sel.get("best_available"),
                         "candidates": sel.get("candidates"),
                         "options": ["add_model", "allow_downgrade"],
                         "reason": "no configured model reaches the required level"},
                        batch_id=batch_id, subtask_id=task.id)
                ev.emit("policy.review_required", "policy", "routing",
                        {"reason": "no_qualified_model", "blocking": True,
                         "options": ["add_model", "allow_downgrade"],
                         "detail": f"needs level {sel.get('required_capability')}; "
                                   f"best available is level {sel.get('best_available')}"},
                        batch_id=batch_id, subtask_id=task.id)
                result.needs_review.append({"reason": "no_qualified_model", "subtask_id": task.id,
                                            "required": sel.get("required_capability"),
                                            "best_available": sel.get("best_available")})
            else:
                ev.emit("routing.selected", "policy", "routing",
                        {"title": task.title, "required_capability": sel.get("required_capability"),
                         "raw_score": sel.get("raw_required"),
                         "adopted": sel.get("required_capability"),
                         "candidates": sel.get("candidates"),
                         "selected": {"model_id": sel.get("model_id"), "label": sel.get("label"),
                                      "capability": sel.get("selected_capability")},
                         "basis": sel.get("basis"), "downgraded": sel.get("downgraded"),
                         "rules": sel.get("rules")},
                        batch_id=batch_id, subtask_id=task.id)
            if a.get("jev_failed"):
                ev.emit("policy.review_required", "policy", "routing",
                        {"reason": "jev_failed", "blocking": False,
                         "detail": "routing was not answered; the cheapest model was used"},
                        batch_id=batch_id, subtask_id=task.id)

    # -- stages ------------------------------------------------------------- #

    def _search_terms(self, task: Task, focus: str = ""):
        """A search-term writer for the researcher, on the cheapest model.

        Returns the writer and the list its calls are recorded in, so they
        land in the ledger like every other call.
        """
        calls: List[Call] = []

        def write(question: str, evidence: Evidence, round_index: int) -> List[str]:
            try:
                spec = self.roster.select_for(1).model
                text, call = self._client(spec, "research").complete(
                    search_terms_messages(
                        question, conversation=task.conversation(1500),
                        searched=evidence.queries if round_index else (), kept=evidence.kept),
                    purpose="search_terms", model=spec.model, temperature=0.2, max_tokens=160)
                calls.append(call)
                return parse_terms(text)
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001 - fall back to searching the task itself
                return [] if round_index else [f"{question} {focus}".strip()]

        return write, calls

    def _write_app(self, task: Task, result: Result, state: str) -> Iterator[dict]:
        """A game or a tool with logic of its own: written whole, never shown as code."""
        selection = self.roster.select_for(APP_CODE_LEVEL)
        result.selection = selection
        yield {"type": "selection", "selection": selection.to_dict(),
               "generation_skipped": False, "skip_reason": ""}
        if not self._selected(selection, result, "app", "a working app"):
            return
        spec = selection.model
        chunks: List[str] = []
        told = 0
        modules: List[str] = []
        text, call = "", None
        for event in self._stream_on(spec, write_messages(task.prompt, state, task.conversation()),
                                     "write_app", 16000, result, temperature=0.4, stage="app"):
            if "switch" in event:
                spec = event["spec"]
                selection = result.selection = replace(selection, model=spec)
                yield {"type": "selection", "selection": selection.to_dict(),
                       "generation_skipped": False, "skip_reason": "", "switch": event["switch"]}
            elif "delta" in event:
                chunks.append(event["delta"])
                size = sum(len(c) for c in chunks)
                if size - told >= 600:           # progress and part names, never the code
                    told = size
                    fresh = find_modules("".join(chunks), modules)
                    for name in fresh:
                        modules.append(name)
                        yield {"type": "app_module", "name": name, "index": len(modules) - 1}
                    yield {"type": "app_progress", "chars": size, "model": spec.label or spec.model}
            elif event.get("done"):
                text, call = event["text"], event["call"]
                if event.get("failed"):
                    result.partial = True
        result.ledger.add(call)
        result.steps.append(Step("write app", spec.label or spec.model, engine="llm",
                                 cost=call.usage.cost, latency_ms=call.latency_ms, code="app_write",
                                 data={"model": spec.label or spec.model, "chars": len(text)}))
        page = extract_page(text) or (text.strip() if "<" in text else "")
        if not page or result.partial:
            # A page cut off mid-write is not a working app; keep the text.
            result.output = text
            self.events.emit("component.failed", "policy", "app",
                             {"reason": "the page was cut off before it was complete"
                              if result.partial else "the reply contained no page"})
            return
        self._adopt_page(result, page, task.prompt)
        self.events.emit("component.filled", "policy", "app",
                         {"components": [], "example_data": False, "title": result.app["title"]})
        yield {"type": "app", "app": result.app}

    def _adopt_page(self, result: Result, page: str, prompt: str) -> None:
        """A written page becomes the run's app, and leaves the answer's text."""
        title = page_title(page, prompt.strip().splitlines()[0][:40] if prompt.strip() else "App")
        result.app = {"title": title, "html": page, "composition": {"kind": "code", "components": []}}
        rest = strip_page(result.output or "", page)
        result.output = (rest + "\n\n" if rest else "") + f"**{title}** · app.html"

    def _build_app(self, task: Task, result: Result, state: str) -> Iterator[dict]:
        """Jev picks the pieces and where they go; a cheap model fills the words."""
        composition, calls = compose(self.jev, task.prompt, state,
                                     observer=self.events.observer("app"))
        self.events.emit("component.slots", "jev", "app",
                         {"kind": composition.kind, "components": composition.components,
                          "layout": composition.layout, "side": composition.side})
        if composition.kind == "code":
            result.ledger.add(calls[0])
            result.steps.append(Step("compose", "code of its own", engine="jev", cost=calls[0].usage.cost,
                                     latency_ms=calls[0].latency_ms, code="compose",
                                     data={"components": [], "kind": "code", "layout": "", "side": []}))
            yield {"type": "composed", "composition": composition.to_dict()}
            yield from self._write_app(task, result, state)
            return
        for call, code in zip(calls, ("compose", "layout")):
            result.ledger.add(call)
            result.steps.append(
                Step(code, ", ".join(composition.side if code == "layout" else composition.components),
                     engine="jev", cost=call.usage.cost, latency_ms=call.latency_ms, code=code,
                     data={"components": composition.components, "layout": composition.layout,
                           "side": composition.side})
            )
        yield {"type": "composed", "composition": composition.to_dict()}

        # Filling labels from a fixed shape is the easiest writing there is.
        selection = self.roster.select_for(APP_FILL_LEVEL)
        result.selection = selection
        yield {"type": "selection", "selection": selection.to_dict(),
               "generation_skipped": False, "skip_reason": ""}
        if not self._selected(selection, result, "app", "the app's words"):
            return
        (spec, call), used = self._on_some_model(
            selection.model,
            lambda m: fill(self._client(m, "app"), task.prompt, composition, state,
                           model=m.model, conversation=task.conversation()),
            result)
        if used is not selection.model:
            selection = result.selection = replace(selection, model=used)
        result.ledger.add(call)
        result.steps.append(
            Step("fill", selection.model.label, engine="llm", cost=call.usage.cost,
                 latency_ms=call.latency_ms, code="app_fill",
                 data={"model": selection.model.label,
                       "components": len(spec["components"])})
        )
        if not spec["components"]:
            # The fill did not match the component schema: say so, and do not
            # present an empty frame as a finished page.
            self.events.emit("component.failed", "policy", "app",
                             {"reason": "the filled content did not match the component schema"})
            result.needs_review.append({"reason": "component_schema", "stage": "app"})
            result.output = ""
            return
        example = not state.strip()
        result.app = {"title": spec["title"], "html": render(spec, composition),
                      "composition": composition.to_dict(), "example_data": example,
                      "dropped": spec.get("dropped") or []}
        result.output = app_summary(spec, composition)
        self.events.emit("component.filled", "llm", "app",
                         {"components": [c["type"] for c in spec["components"]],
                          "example_data": example, "title": spec["title"]})
        yield {"type": "app", "app": result.app}

    def _run_decisions(
        self, task: Task, plan: Plan, result: Result, state: Optional[str] = None
    ) -> Optional[ScoreAnswer]:
        """Send the whole batch in one call. Returns the capability answer."""
        questions: Dict[str, Any] = dict(plan.decisions)

        ask_capability = plan.may_use_llm and self.roster.needs_capability_question()
        if ask_capability:
            questions[CAPABILITY_DECISION] = self.roster.capability_question(
                plan.generation.instruction
            )
        if not questions:
            return None

        material = task.state if state is None else state
        history = task.conversation(3000)
        if history:
            material = f"CONVERSATION SO FAR\n{history}\n\nNOW\n{task.prompt}\n\n{material or ''}".strip()
        observer = self.events.observer("decisions")
        try:
            raw, call = self.jev.decide(
                material, {n: q.to_payload() for n, q in questions.items()}, observer=observer,
                targets={n: ("the model for this answer" if n == CAPABILITY_DECISION
                             else (task.targets or {}).get(n) or n.replace("_", " "))
                         for n in questions})
        except Cancelled:
            raise
        except ProviderError as exc:
            # A failed batch is not a "no". It stays a failure the reader can see.
            from .events import short_reason

            self.events.emit("policy.review_required", "policy", "decisions",
                             {"reason": "jev_failed", "blocking": False,
                              "detail": short_reason(exc)}, batch_id=observer.last_batch)
            result.needs_review.append({"reason": "jev_failed", "stage": "decisions",
                                        "detail": short_reason(exc)})
            result.steps.append(Step("jev batch", "failed", engine="jev", code="jev_failed",
                                     data={"reason": short_reason(exc)}))
            self._decisions_failed = True
            return None
        self._decision_batch = observer.last_batch
        result.ledger.add(call)

        capability: Optional[ScoreAnswer] = None
        answers: Dict[str, Answer] = {}
        for name in questions:
            if name not in raw:
                continue
            answer = parse_answer(name, raw[name])
            if name == CAPABILITY_DECISION:
                capability = answer if isinstance(answer, ScoreAnswer) else None
            else:
                answers[name] = answer

        result.decisions = answers
        result.review = review(answers, self.thresholds)
        for name, answer in result.review.uncertain.items():
            self.events.emit("policy.review_required", "policy", "decisions",
                             {"reason": "low_confidence", "blocking": False,
                              "distribution": _answer_payload(answer),
                              "threshold": _uncertain_rule(answer, self.thresholds),
                              "options": ["escalate"] if self.escalate_uncertain else []},
                             batch_id=observer.last_batch, question_id=name)
        detail = f"{len(answers)} decision(s)"
        if ask_capability:
            detail += " + model sizing"
        detail += " in one call"
        if result.review.uncertain:
            detail += f"; {len(result.review.uncertain)} uncertain"
        result.steps.append(
            Step("jev batch", detail, engine="jev",
                 cost=call.usage.cost, latency_ms=call.latency_ms,
                 code="jev_batch",
                 data={"decisions": len(answers), "sized_model": ask_capability,
                       "uncertain": len(result.review.uncertain)})
        )
        return capability

    def _choose(
        self, plan: Plan, result: Result, capability: Optional[ScoreAnswer]
    ) -> Optional[Selection]:
        """Apply the gate, then size the model."""
        ev = self.events
        batch = getattr(self, "_decision_batch", None)

        def skipped(reason: str, rule: str) -> None:
            ev.emit("generation.skipped", "policy", "generation",
                    {"scope": "run", "reason": reason, "rule": rule,
                     "llm_calls_so_far": ev.counts.get("llm.started", 0)}, batch_id=batch)

        if not plan.may_use_llm:
            result.generation_skipped = True
            result.skip_reason = "the plan answers from decisions alone"
            result.skip_code = "decisions_only"
            if getattr(self, "_decisions_failed", False):
                # Nothing came back, so nothing was delivered: not a skip.
                result.generation_skipped = False
                result.skip_code = ""
                return None
            skipped("the decisions are the deliverable", "plan.answer_from_decisions/v1")
            return None

        fired = plan.gate.should_skip_generation(result.decisions)
        if fired is not None and result.evidence and not result.evidence.empty:
            # The task sent us to look something up and we found it. Withholding
            # the answer now would be the one outcome nobody asked for.
            result.steps.append(
                Step("gate", f"gate ignored — research found evidence ({fired.describe()})",
                     code="gate_ignored", data=fired.to_dict())
            )
            ev.emit("policy.applied", "policy", "generation",
                    {"rule": "gate.ignored_when_research_found_evidence/v1",
                     "rule_version": RULES_VERSION, "inputs": fired.to_dict(),
                     "action": "generate anyway"}, batch_id=batch)
            fired = None
        if fired is not None:
            result.generation_skipped = True
            result.skip_reason = f"gate: {fired.describe()}"
            result.skip_code = "gate"
            result.skip_data = {"condition": fired.describe(), **fired.to_dict()}
            result.steps.append(
                Step("gate", f"generation skipped — {fired.describe()}",
                     code="gate", data=result.skip_data)
            )
            ev.emit("policy.applied", "policy", "generation",
                    {"rule": "gate.skip_generation_when/v1", "rule_version": RULES_VERSION,
                     "inputs": fired.to_dict(), "action": "skip generation"}, batch_id=batch)
            skipped(f"the plan's gate fired: {fired.describe()}", "gate.skip_generation_when/v1")
            return None

        if plan.generation.model_id:
            pinned = self.roster.get(plan.generation.model_id)
            if pinned is None or not pinned.enabled:
                raise ConfigError(
                    f"plan pins model {plan.generation.model_id!r}, which is not available"
                )
            selection = Selection(
                model=pinned,
                required=pinned.capability,
                raw_required=float(pinned.capability),
                certainty=1.0,
                rounded_up=False,
                considered=len(self.roster.available()),
            )
            result.steps.append(
                Step("model", f"pinned to {pinned.label or pinned.model}",
                     code="model_pinned", data={"model": pinned.label or pinned.model})
            )
            self._selected(selection, result, "generation", "the answer", rules=["plan.pinned_model/v1"])
            return selection

        selection = self.roster.select(capability)

        # Jev may report that no prose is owed. Honour that only when there are
        # decisions to serve as the answer; otherwise the caller would get
        # nothing at all, so generate on the cheapest model instead.
        if selection.jev_only and not result.decisions:
            fallback = min(self.roster.available(), key=lambda m: m.blended_price)
            selection = Selection(
                model=fallback,
                required=1,
                raw_required=selection.raw_required,
                certainty=selection.certainty,
                rounded_up=True,
                considered=selection.considered,
            )
            result.steps.append(
                Step(
                    "model",
                    "Jev judged no text was needed, but the plan has no decisions "
                    f"to answer with — generating on {fallback.label or fallback.model}",
                    code="model_forced",
                    data={"model": fallback.label or fallback.model},
                )
            )
            self._selected(selection, result, "generation", "the answer",
                           rules=["capability.level0_without_decisions_generates/v1"])
            return selection

        if selection.jev_only:
            result.generation_skipped = True
            result.skip_reason = "Jev: the decisions are the whole answer"
            result.skip_code = "jev_only"
            result.steps.append(
                Step("gate", f"generation retired — {selection.describe()}",
                     code="jev_only")
            )
            ev.emit("policy.applied", "policy", "generation",
                    {"rule": "capability.level0_no_generation/v1", "rule_version": RULES_VERSION,
                     "inputs": {"raw_score": selection.raw_required, "adopted": 0},
                     "action": "no generation call"}, batch_id=batch,
                    question_id=CAPABILITY_DECISION)
            skipped("the decisions already answer the task", "capability.level0_no_generation/v1")
        elif result.subtasks:
            # Each step was sized and staffed on its own; this pick only names
            # the model the whole is credited to, so it can neither block the
            # steps nor be reported as a routing decision of its own.
            if not selection.blocked:
                result.steps.append(
                    Step("model", selection.describe(), code="model",
                         data={"model": selection.model.label or selection.model.model,
                               "required": selection.required,
                               "rounded_up": selection.rounded_up})
                )
            return selection
        else:
            if capability is not None:
                # Jev's own reading of the whole answer, shown apart from the
                # rule that turned it into a model.
                ev.emit("routing.assessed", "jev", "generation",
                        {"title": "the answer", "raw_score": float(capability.value),
                         "levels": len(capability.legend) or None,
                         "confidence": float(capability.confidence),
                         "probabilities": dict(capability.probabilities)},
                        batch_id=batch, question_id=CAPABILITY_DECISION)
            rules = (["capability.no_answer_cheapest/v1"] if capability is None
                     else ["capability.round_to_nearest/v1"])
            if selection.rounded_up:
                rules.append("capability.round_up_below_certainty/v1")
            if selection.relaxed:
                rules.append("capability.round_up_relaxed/v1")
            if not self._selected(selection, result, "generation", "the answer", rules=rules):
                return selection
            result.steps.append(
                Step("model", selection.describe(), code="model",
                     data={"model": selection.model.label or selection.model.model,
                           "required": selection.required,
                           "rounded_up": selection.rounded_up})
            )
        return selection

    def _selected(self, selection: Selection, result: Result, stage: str, what: str, *,
                  rules: Sequence[str] = (), subtask_id: Optional[str] = None) -> bool:
        """Report a model choice — or that none qualifies. False means stop here."""
        ev = self.events
        info = selection.to_dict()
        if selection.blocked:
            result.blocked = True
            result.needs_review.append({"reason": "no_qualified_model", "stage": stage,
                                        "required": selection.required,
                                        "best_available": selection.best_available})
            ev.emit("routing.unavailable", "policy", stage,
                    {"title": what, "required_capability": selection.required,
                     "best_available": selection.best_available,
                     "candidates": info["candidates"], "options": ["add_model", "allow_downgrade"],
                     "reason": "no configured model reaches the required level"},
                    subtask_id=subtask_id)
            ev.emit("policy.review_required", "policy", stage,
                    {"reason": "no_qualified_model", "blocking": True,
                     "options": ["add_model", "allow_downgrade"],
                     "detail": f"needs level {selection.required}; best available is level "
                               f"{selection.best_available}"}, subtask_id=subtask_id)
            result.steps.append(Step("model", selection.describe(), code="no_qualified_model",
                                     data={"required": selection.required,
                                           "best": selection.best_available}))
            return False
        ev.emit("routing.selected", "policy", stage,
                {"title": what, "required_capability": selection.required,
                 "raw_score": selection.raw_required, "adopted": selection.required,
                 "candidates": info["candidates"],
                 "selected": {"model_id": info["model_id"], "label": info["label"],
                              "capability": info["selected_capability"]},
                 "basis": info["basis"], "downgraded": selection.downgraded,
                 "rules": list(rules)}, subtask_id=subtask_id,
                batch_id=getattr(self, "_decision_batch", None) if stage == "generation" else None)
        return True

    def _generate(
        self, task: Task, plan: Plan, result: Result, selection: Selection,
        state: Optional[str] = None
    ) -> Iterator[dict]:
        evidence = result.evidence
        if evidence is not None:
            self._delivery_check(evidence, MAX_EVIDENCE_CHARS, "generation", None)
        messages = generation_messages(
            plan.generation.instruction,
            task.state_text if evidence else (state if state is not None else task.state_text),
            result.decisions,
            result.review,
            include_state=plan.generation.include_state,
            evidence=evidence.as_text() if evidence else "",
            sources=evidence.sources() if evidence else (),
            open_items=[t.title for t in result.subtasks
                        if not t.already_done and not t.output],
            language=self.reply_language,
            profile=self.memory.block() if self.memory else "",
            conversation=task.conversation(),
            rules=self.rules,
            searched=evidence.queries if evidence else (),
            evidence_complete=(evidence.sufficiency == "sufficient") if evidence else None,
            tools=self._tool_names(),
        )
        log = Log()
        log.extend(messages)
        shown: List[str] = []
        outcome = None
        try:
            for event in self._turn(spec=selection.model, log=log, result=result, task=task,
                                    purpose="generate", max_tokens=plan.generation.max_tokens):
                if "delta" in event:
                    shown.append(event["delta"])
                    yield {"type": "delta", "text": event["delta"]}
                elif "tool" in event:
                    yield {"type": "tool", "tool": event["tool"]}
                elif "switch" in event:
                    selection = result.selection = replace(selection, model=event["spec"])
                    yield {"type": "selection", "selection": selection.to_dict(),
                           "generation_skipped": False, "skip_reason": "", "switch": event["switch"]}
                elif event.get("done"):
                    outcome = event["outcome"]
        except Cancelled:
            result.output = "".join(shown)
            raise
        result.output = outcome.text
        if outcome.stop_reason == "failed":
            result.partial = bool(outcome.text)
        call = outcome.calls[-1] if outcome.calls else Call(engine="llm", purpose="generate",
                                                            model=selection.model.model, latency_ms=0)
        spec = selection.model
        self.events.emit("output.version", "llm", "generation",
                         {"target": "answer", "version": 1, "mode": "partial" if result.partial else "written",
                          "depends_on": [x for x in ((evidence.checked_set_id if evidence else None),) if x]})
        latency = sum(c.latency_ms for c in outcome.calls)
        result.steps.append(
            Step(
                "generate",
                f"{selection.model.label or selection.model.model}, "
                f"{call.usage.output_tokens} output tokens"
                + (f", {outcome.steps} steps, {outcome.tools_run} tool call(s)"
                   if outcome.tools_run else ""),
                engine="llm",
                cost=sum(c.usage.cost for c in outcome.calls),
                latency_ms=latency,
                code="generate",
                data={"model": selection.model.label or selection.model.model,
                      "output_tokens": call.usage.output_tokens,
                      "steps": outcome.steps, "tools": outcome.tools_run},
            )
        )
        yield {"type": "generated"}

    def _run_subtasks(self, task: Task, plan: Plan, result: Result,
                      state: str) -> Iterator[dict]:
        """Run each step on the model Jev sized it for.

        Independent steps run at the same time, because the wall clock is the
        thing the user actually waits on. Steps the material already answers do
        not run at all.
        """
        pending = [t for t in result.subtasks if not t.already_done and not t.blocked]
        for skipped in (t for t in result.subtasks if t.already_done):
            skipped.status = "skipped"
            skipped.note = "already covered"
            self.events.emit("generation.skipped", "policy", "generation",
                             {"scope": "step", "title": skipped.title,
                              "reason": "the material already contains a finished version of this part",
                              "rule": "routing.already_done_at_or_above/v1",
                              "llm_calls_so_far": self.events.counts.get("llm.started", 0)},
                             subtask_id=skipped.id)
            yield {"type": "subtask", "subtask": skipped.to_dict()}
        for held in (t for t in result.subtasks if t.blocked and not t.already_done):
            held.status = "needs_review"
            held.note = "no qualified model"
            yield {"type": "subtask", "subtask": held.to_dict()}

        parallel = bool(result.routing and result.routing.independent) and len(pending) > 1
        # Workers report as they go — what they looked up, which model is
        # writing, and the words themselves — through one queue, so the reader
        # watches every step at once rather than waiting for each to finish.
        events: "queue.Queue[Optional[dict]]" = queue.Queue()

        def work(subtask: Subtask) -> None:
            try:
                self._one_subtask(subtask, task, result, state, events.put)
            except Cancelled:
                subtask.status = "cancelled"
                subtask.note = "stopped before it started" if not subtask.output else "stopped"
            except Exception as exc:  # noqa: BLE001 - one failed step must not sink the rest
                from .events import short_reason

                subtask.status = "failed"
                subtask.note = short_reason(exc)
            events.put({"type": "subtask", "subtask": subtask.to_dict()})

        def drive() -> None:
            try:
                if parallel:
                    for subtask in pending:
                        subtask.status = "running"
                    events.put({"type": "subtasks", "subtasks": [t.to_dict() for t in result.subtasks],
                                "routing": result.routing.to_dict() if result.routing else None})
                    with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
                        list(pool.map(work, pending))
                else:
                    for subtask in pending:
                        subtask.status = "running"
                        events.put({"type": "subtask", "subtask": subtask.to_dict()})
                        work(subtask)
            finally:
                events.put(None)

        threading.Thread(target=drive, daemon=True).start()
        while True:
            event = events.get()
            if event is None:
                break
            yield event

        # A stop request that arrived while the workers ran ends the run here:
        # nothing new is started, and what they wrote is kept.
        self.events.check()
        yield from self._assemble(task, plan, result)

    def _one_subtask(self, subtask: Subtask, task: Task, result: Result, state: str,
                     emit: Optional[Callable[[dict], None]] = None) -> None:
        emit = emit or (lambda event: None)

        def note(entry: dict) -> None:
            subtask.log.append(entry)
            emit({"type": "subtask_log", "index": subtask.index, "entry": entry})

        started = time.perf_counter()
        evidence = result.evidence
        local_sources: List[dict] = []
        local_text = ""
        subtask_searched: List[str] = []

        # A step may need its own lookup, filtered by Jev exactly as before.
        if subtask.needs_web and self.research_enabled:
            writer, written = self._search_terms(task, subtask.title)

            def relay(event: dict) -> None:
                if event.get("stage") == "search":
                    note({"code": "search", "q": event.get("query", "")})
                elif event.get("stage") == "fetch":
                    note({"code": "read", "n": len(event.get("urls") or [])})
                elif event.get("stage") == "filtered":
                    note({"code": "kept", "n": event.get("kept", 0)})

            found, calls = self.researcher.run(
                f"{task.prompt.strip()} — {subtask.title}", Research(rounds=2, top_k=2),
                queries=writer, emit=relay, events=self.events, subtask_id=subtask.id,
                deliver_limit=SUBTASK_EVIDENCE_CHARS,
            )
            calls = written + calls
            subtask_searched = found.queries
            for call in calls:
                result.ledger.add(call)
                subtask.cost += call.usage.cost
            local_text = found.as_text(SUBTASK_EVIDENCE_CHARS)
            local_sources = found.sources()
            subtask.sources = local_sources

        # A role's tools run before it writes, with arguments read out of the
        # task rather than invented: a path, a URL, an expression.
        role = roles.get(subtask.role)
        tool_text = self._run_role_tools(role, task, subtask, result)
        if tool_text:
            local_text = (local_text + "\n\n" + tool_text) if local_text else tool_text

        spec = self.roster.get(subtask.model_id) or min(
            self.roster.available(), key=lambda m: m.blended_price
        )
        subtask.model_label = spec.label or spec.model
        previous = [t for t in result.subtasks if t.index < subtask.index and t.output]
        if evidence is not None:
            self._delivery_check(evidence, SUBTASK_EVIDENCE_CHARS, "generation", subtask.id)
        messages = subtask_messages(
            task.prompt, subtask.title, state, result.decisions, result.review,
            evidence_text="\n\n".join(filter(None, [
                evidence.as_text(SUBTASK_EVIDENCE_CHARS) if evidence else "", local_text
            ])),
            sources=(evidence.sources() if evidence else []) + local_sources,
            previous=[(t.title, t.output) for t in previous],
            outline=[t.title for t in result.subtasks],
            position=subtask.index,
            language=self.reply_language,
            persona=self._persona(subtask),
            profile=self.memory.block() if self.memory else "",
            conversation=task.conversation(),
            rules=self.rules,
            searched=(evidence.queries if evidence else []) + subtask_searched,
            evidence_complete=(evidence.sufficiency == "sufficient") if evidence else None,
            tools=self._tool_names(),
        )
        note({"code": "write", "model": subtask.model_label})
        def switched(info: dict, substitute: ModelSpec) -> None:
            subtask.model_label = substitute.label or substitute.model
            subtask.model_id = substitute.id
            note({"code": "failover", **info})

        written: List[str] = []

        def delta(piece: str) -> None:
            written.append(piece)
            emit({"type": "subtask_delta", "index": subtask.index, "text": piece})

        log = Log()
        log.extend(messages)
        outcome = None
        try:
            for event in self._turn(spec=spec, log=log, result=result, task=task, purpose="subtask",
                                    max_tokens=1400, subtask_id=subtask.id):
                if "delta" in event:
                    delta(event["delta"])
                elif "tool" in event:
                    note({"code": "tool", "name": event["tool"]["name"],
                          "ok": event["tool"]["allowed"]})
                    emit({"type": "subtask", "subtask": subtask.to_dict()})
                elif "switch" in event:
                    switched(event["switch"], event["spec"])
                    spec = event["spec"]
                elif event.get("done"):
                    outcome = event["outcome"]
        except Cancelled:
            subtask.output = "".join(written).strip()
            raise
        call = outcome.calls[-1] if outcome.calls else Call(engine="llm", purpose="subtask",
                                                            model=spec.model, latency_ms=0)
        failed = outcome.failure if outcome.stop_reason == "failed" else ""
        text = outcome.text
        subtask.output = text.strip()
        subtask.cost += sum(c.usage.cost for c in outcome.calls)
        subtask.steps = outcome.steps
        subtask.tools_run = outcome.tools_run
        subtask.ms = int((time.perf_counter() - started) * 1000)
        subtask.status = "failed" if failed else "done"
        if failed:
            # Words the reader has already seen are kept, and not re-run.
            subtask.note = "stopped mid-output; the partial text is kept"
            result.partial = True
        subtask.version = 1
        self.events.emit("output.version", "llm", "generation",
                         {"target": subtask.id, "version": 1, "title": subtask.title,
                          "depends_on": [x for x in (result.evidence.checked_set_id if result.evidence else None,)
                                         if x], "mode": "partial" if failed else "written"},
                         subtask_id=subtask.id)
        result.steps.append(
            Step("subtask", f"{subtask.title} — {subtask.model_label}",
                 engine="llm", cost=call.usage.cost, latency_ms=subtask.ms,
                 code="subtask",
                 data={"title": subtask.title, "model": subtask.model_label,
                       "required": subtask.required, "web": subtask.needs_web,
                       "role": subtask.role, "steps": subtask.steps,
                       "tools": subtask.tools_run})
        )

    # -- the agent loop ------------------------------------------------------ #

    def _loop_tools(self):
        """The tools any step of this run may call, or None when it may call none.

        The boundary is the user's own setting — look only, read and write, or
        also run things — and every call still goes past the gate. A role is
        about what kind of worker this is, not about what it is allowed to
        touch, so it does not narrow this further: a writer that has been
        asked to save its draft should be able to.
        """
        if self.tools is None or not self.agent_loop:
            return None
        available = [t for t in self.tools.available() if t.schema()]
        if not available:
            return None
        context = replace(self.tools.context, ask=self._ask_user, delegate=self._delegate)
        return replace(self.tools, enabled=[t.name for t in available], context=context)

    def _delegate(self, task: str, context: str, difficulty: int) -> Tuple[str, str]:
        """Run one self-contained job on a model of its own, and return only its answer.

        The delegate gets no tools and no history: it is given the job and the
        context the caller chose to pass on, and its whole contribution is the
        text it sends back. That is what keeps the caller's own context clean.
        """
        from .tools import ToolError

        self.events.check()
        selection = self.roster.select_for(max(1, min(int(difficulty or 2), 4)))
        if selection.blocked or selection.model is None:
            raise ToolError(f"no configured model reaches level {selection.required}")
        spec = selection.model
        result = self._current or Result(task=Task(prompt=task), plan=_app_plan(Task(prompt=task), False))
        log = Log()
        log.system(
            "You are given one self-contained job by another model working on a larger task. "
            "Do it and reply with the result only: no preamble, no restatement, nothing about "
            f"how you went about it. Today is {today()}.")
        log.append("user", task + (f"\n\nCONTEXT\n{context}" if context.strip() else ""))
        text = ""
        for event in self._turn(spec=spec, log=log, result=result, task=result.task,
                                purpose="delegate", max_tokens=1200, stage="delegation"):
            if event.get("done"):
                text = event["outcome"].text
        return text.strip(), spec.label or spec.model

    def _ask_user(self, question: str, options: Sequence[str] = (), kind: str = "question"):
        return self.events.ask(question, options=options, kind=kind, stage="generation")

    def _tool_names(self) -> List[str]:
        tools = self._loop_tools()
        return [t.name for t in tools.available()] if tools is not None else []

    def _gate(self, task: Task, result: Result, tools) -> Gate:
        """Jev judges anything with a side effect; the tools' own rules still apply.

        With ``approve_changes`` on, anything that survives both still goes to
        the person before it runs. That is the order that matters: the cheap
        checks first, so a question only reaches them when it is worth asking.
        """
        def judge(actions):
            self.events.check()
            observer = self.events.observer("tools")
            verdicts, call = gate_actions(self.jev, task.prompt, list(actions), observer=observer)
            if call:
                result.ledger.add(call)
            return verdicts, call

        def ask(name: str, call) -> Optional[bool]:
            answer = self.events.ask(
                f"{name}: {_argument_line(call.arguments)}",
                options=["Run it", "Skip it"], kind="approval", stage="tools")
            if answer is None:
                return False
            return not answer.strip().lower().startswith(("skip", "no", "不", "拒"))

        return Gate(tools, judge=judge if tools is not None else None,
                    ask=ask if self.approve_changes else None)

    def _turn(self, *, spec: ModelSpec, log: Log, result: Result, task: Task,
              purpose: str, max_tokens: Optional[int], subtask_id: Optional[str] = None,
              stage: str = "generation", temperature: float = 0.2,
              attempt_id: Optional[str] = None) -> Iterator[dict]:
        """One turn on one model. Yields {'delta'}, {'tool'} … then {'done', outcome}.

        With no tools offered this is a single streamed request, which is what
        generation was before the loop existed.
        """
        tools = self._loop_tools()
        gate = self._gate(task, result, tools)
        budget = Budget(steps=self.max_steps if tools else 1, max_tokens=max_tokens,
                        context_tokens=self.context_tokens)
        tried: set = set()
        attempt = int((attempt_id or "attempt-1").rsplit("-", 1)[-1] or 1)
        outcome: Optional[Outcome] = None
        while True:
            loop = AgentLoop(
                client=self._client(spec, stage, subtask_id, f"attempt-{attempt}"),
                model=spec.model, log=log, tools=tools, gate=gate, budget=budget,
                events=self.events, stage=stage, subtask_id=subtask_id, purpose=purpose,
                temperature=temperature, thinking=spec.thinking,
                compactor=self._compactor(result),
            )
            for event in loop.run():
                if event.get("done"):
                    outcome = event["outcome"]
                    break
                yield event
            for call in outcome.calls:
                result.ledger.add(call)
            if outcome.stop_reason != "failed":
                break
            # A model that failed before writing a word is replaced and the
            # turn starts again; one that failed halfway is not retried, because
            # the reader has already seen its words.
            if outcome.text or outcome.tools_run:
                self.events.emit("policy.applied", "policy", stage,
                                 {"rule": "failover.not_after_output/v1", "rule_version": RULES_VERSION,
                                  "inputs": {"streamed_chars": len(outcome.text),
                                             "tools_run": outcome.tools_run},
                                  "action": "keep the partial text; no automatic retry",
                                  "detail": outcome.failure},
                                 subtask_id=subtask_id, attempt_id=f"attempt-{attempt}")
                break
            substitute = self._substitute(spec, tried, ProviderError(outcome.failure), result,
                                          stage=stage, subtask_id=subtask_id, attempt=attempt + 1)
            if substitute is None:
                break
            yield {"switch": {"from": spec.label or spec.model,
                              "to": substitute.label or substitute.model,
                              "reason": outcome.failure}, "spec": substitute}
            spec, attempt = substitute, attempt + 1
        if outcome.tools_run or outcome.steps > 1:
            result.turns.append({"subtask_id": subtask_id, **outcome.to_dict()})
        if self.tools is not None and self.tools.context.todo:
            result.todo = list(self.tools.context.todo)
        yield {"done": True, "outcome": outcome}

    def _compactor(self, result: Result):
        """Summarise the older exchanges with the cheapest model that can write.

        Without this a long turn ends by sending the model a context it cannot
        read; with it, the oldest exchanges become one paragraph and the run
        carries on. The summary is a log entry of its own, so what it replaced
        is still on the record.
        """
        def compact(log: Log, before: int) -> Optional[str]:
            try:
                spec = self.roster.select_for(1).model
                if spec is None:
                    return None
                text, call = self._client(spec, "generation").complete(
                    compaction_messages(log.messages(), self.reply_language),
                    purpose="compact", model=spec.model, temperature=0.0, max_tokens=700)
                result.ledger.add(call)
                return text.strip()
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001 - a failed summary is not a failed run
                return None

        return compact

    def _client(self, spec: ModelSpec, stage: str, subtask_id: Optional[str] = None,
                attempt_id: Optional[str] = None):
        """A client for ``spec`` whose every call is reported to this run."""
        return _observed(self.llm_factory(spec),
                         self.events.llm_observer(stage, subtask_id, attempt_id))

    def _stream_on(self, spec: ModelSpec, messages: list, purpose: str, max_tokens: Optional[int],
                   result: Result, temperature: float = 0.2, *, stage: str = "generation",
                   subtask_id: Optional[str] = None) -> Iterator[dict]:
        """Stream a generation, moving to another model if this one refuses.

        Yields {"delta"} pieces, {"switch"} when a model is replaced, and one
        final {"done", "text", "call", "spec"}. A model that fails before
        writing anything — a region lock, a withdrawn id, an outage — is
        benched and the next cheapest model that clears the same rung takes
        over, as a new attempt. One that fails halfway is not retried: the
        reader has already seen its words, so they are kept and the final
        event says ``failed``. A stop request ends the stream at the next
        piece; the provider may still bill for the request.
        """
        tried: set = set()
        attempt = 1
        while True:
            chunks: List[str] = []
            usage: dict = {}
            model_used = spec.model
            call_id = ""
            started = time.perf_counter()
            attempt_id = f"attempt-{attempt}"
            try:
                for event in self._client(spec, stage, subtask_id, attempt_id).stream(
                        messages, model=spec.model, max_tokens=max_tokens,
                        temperature=temperature, thinking=spec.thinking, purpose=purpose):
                    if self.events.cancel.is_set():
                        raise Cancelled("stopped while the model was writing")
                    if "delta" in event:
                        chunks.append(event["delta"])
                        yield {"delta": event["delta"]}
                    elif event.get("done"):
                        usage = event.get("usage") or {}
                        model_used = event.get("model") or model_used
                        call_id = event.get("call_id") or ""
            except ProviderError as exc:
                if chunks:
                    self.events.emit("policy.applied", "policy", stage,
                                     {"rule": "failover.not_after_output/v1", "rule_version": RULES_VERSION,
                                      "inputs": {"streamed_chars": sum(len(c) for c in chunks)},
                                      "action": "keep the partial text; no automatic retry",
                                      "detail": _reason(exc)},
                                     subtask_id=subtask_id, attempt_id=attempt_id)
                    call = Call(engine="llm", purpose=purpose, model=model_used,
                                latency_ms=int((time.perf_counter() - started) * 1000),
                                usage=Usage(source="unknown"))
                    yield {"done": True, "text": "".join(chunks), "call": call, "spec": spec,
                           "failed": _reason(exc)}
                    return
                substitute = self._substitute(spec, tried, exc, result, stage=stage,
                                              subtask_id=subtask_id, attempt=attempt + 1)
                if substitute is None:
                    raise
                yield {"switch": {"from": spec.label or spec.model, "to": substitute.label or substitute.model,
                                  "reason": _reason(exc)}, "spec": substitute}
                spec = substitute
                attempt += 1
                continue
            call = Call(engine="llm", purpose=purpose, model=model_used,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        usage=Usage.from_dict(usage))
            if call_id:
                call.call_id = call_id
            yield {"done": True, "text": "".join(chunks), "call": call, "spec": spec}
            return

    def _substitute(self, spec: ModelSpec, tried: set, exc: Exception, result: Result, *,
                    stage: str = "generation", subtask_id: Optional[str] = None,
                    attempt: int = 2) -> Optional[ModelSpec]:
        """The model to try when ``spec`` failed: cheapest that is at least as capable.

        Never a weaker one unless the user allowed downgrades — the rung the
        step needs does not drop because a model was unavailable.
        """
        bench(spec.model, _reason(exc))
        tried.add(spec.id)
        pool = [m for m in self.roster.available() if m.id not in tried and m.model != spec.model]
        able = [m for m in pool if m.capability >= spec.capability]
        pick = (min(able, key=lambda m: m.blended_price) if able
                else max(pool, key=lambda m: (m.capability, -m.blended_price))
                if pool and self.roster.allow_downgrade else None)
        if pick is not None:
            result.steps.append(Step(
                "failover", f"{spec.label or spec.model} → {pick.label or pick.model}", code="failover",
                data={"from": spec.label or spec.model, "to": pick.label or pick.model, "reason": _reason(exc)}))
            self.events.emit("policy.applied", "policy", stage,
                             {"rule": "failover.before_output/v1", "rule_version": RULES_VERSION,
                              "inputs": {"from": spec.label or spec.model, "capability": spec.capability,
                                         "reason": _reason(exc)},
                              "action": f"retry on {pick.label or pick.model}",
                              "detail": "downgraded with permission" if pick.capability < spec.capability else ""},
                             subtask_id=subtask_id, attempt_id=f"attempt-{attempt}")
        return pick

    def _on_some_model(self, spec: ModelSpec, attempt: Callable[[ModelSpec], Any], result: Result):
        """``attempt(spec)`` for a call that does not stream, with the same failover."""
        tried: set = set()
        while True:
            try:
                return attempt(spec), spec
            except ProviderError as exc:
                substitute = self._substitute(spec, tried, exc, result)
                if substitute is None:
                    raise
                spec = substitute

    def _stream_text(self, spec: ModelSpec, messages: list, purpose: str, max_tokens: int,
                     on_delta: Callable[[str], None], result: Result,
                     on_switch: Optional[Callable[[dict, ModelSpec], None]] = None, *,
                     subtask_id: Optional[str] = None) -> Tuple[str, Call, ModelSpec, str]:
        """Generate on ``spec`` (or its substitute), handing each piece to ``on_delta``.

        The last value is the failure reason when the model stopped mid-output.
        """
        for event in self._stream_on(spec, messages, purpose, max_tokens, result,
                                     subtask_id=subtask_id):
            if "delta" in event:
                on_delta(event["delta"])
            elif "switch" in event and on_switch:
                on_switch(event["switch"], event["spec"])
            elif event.get("done"):
                return event["text"], event["call"], event["spec"], event.get("failed", "")
        raise ProviderError("generation ended without a result")

    def _delivery_check(self, evidence: Evidence, limit: int, stage: str,
                        subtask_id: Optional[str]) -> None:
        """Say so when a step receives less evidence than was checked."""
        if evidence.checked_set_id is None or evidence.covers(limit):
            return
        self.events.emit("evidence.checked", "policy", stage,
                         {"result": "not_rechecked", "scope": "delivery", "limit": limit,
                          "checked_set_id": evidence.checked_set_id,
                          "delivered": len(evidence.delivered(limit)[0]),
                          "reason": "this step receives a subset of the checked evidence; "
                                    "its sufficiency was not checked again"},
                         subtask_id=subtask_id, evidence_set_id=evidence.snapshot_id(limit))

    # -- talking to one worker after the run -------------------------------- #

    def revise_subtask(self, task: Task, step: Subtask, message: str, *,
                       thread: Sequence[dict] = (), outline: Sequence[str] = (),
                       others: Sequence[Tuple[str, str]] = (),
                       answer_mode: str = "") -> Iterator[dict]:
        """A new operation on an old run: its own events, through the same outlet."""
        self.events.register()
        try:
            yield from self.events.run(lambda: self._revise(
                task, step, message, thread=thread, outline=outline, others=others,
                answer_mode=answer_mode))
        finally:
            self.events.unregister()

    def _revise(self, task: Task, step: Subtask, message: str, *,
                thread: Sequence[dict] = (), outline: Sequence[str] = (),
                others: Sequence[Tuple[str, str]] = (),
                answer_mode: str = "") -> Iterator[dict]:
        """One worker, spoken to directly: it rewrites its own part.

        It keeps its persona and its model, sees what the other workers wrote,
        and hears the whole conversation it has had with the user. Whether the
        request needs fresh facts is Jev's call, not a keyword match.
        """
        self.reply_language = detect_language(message, detect_language(task.prompt, self.language))
        spent: List[Call] = []
        found_text, sources, searched = "", list(step.sources or []), []
        needs = None
        if self.research_enabled:
            try:
                raw, call = self.jev.decide(
                    f"PART OF A LARGER PIECE\n{step.title}\n\nCURRENT TEXT\n{step.output[:4000]}"
                    f"\n\nTHE USER NOW ASKS\n{message}",
                    {"web": Noul(instructions=(
                        "Does doing what the user now asks need facts that are not in the "
                        "current text — figures, dates, examples, sources to look up?")).to_payload()},
                    purpose="revise_assess", observer=self.events.observer("revision", step.id),
                    targets={"web": step.title})
                spent.append(call)
                needs = getattr(parse_answer("web", raw["web"]), "probability", 0.0) >= 0.5 if raw.get("web") else None
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001
                needs = None
        if needs:
            writer, written = self._search_terms(task, f"{step.title} — {message}")
            logs: List[dict] = []
            found, calls = self.researcher.run(f"{message} ({step.title}; {task.prompt.strip()})",
                                               Research(rounds=2, top_k=2), queries=writer,
                                               emit=lambda e: logs.append(e), events=self.events,
                                               subtask_id=step.id,
                                               deliver_limit=SUBTASK_EVIDENCE_CHARS)
            spent += written + calls
            for event in logs:
                if event.get("stage") == "search":
                    yield {"type": "log", "entry": {"code": "search", "q": event.get("query", "")}}
                elif event.get("stage") == "filtered":
                    yield {"type": "log", "entry": {"code": "kept", "n": event.get("kept", 0)}}
            found_text, searched = found.as_text(SUBTASK_EVIDENCE_CHARS), found.queries
            sources += found.sources()

        spec = self.roster.get(step.model_id)
        if spec is None:
            selection = self.roster.select_for(max(step.required, 2))
            if selection.blocked:
                self.events.emit("routing.unavailable", "policy", "revision",
                                 {"title": step.title, "required_capability": selection.required,
                                  "best_available": selection.best_available,
                                  "candidates": selection.to_dict()["candidates"],
                                  "options": ["add_model", "allow_downgrade"],
                                  "reason": "no configured model reaches the required level"},
                                 subtask_id=step.id)
                yield {"type": "error", "code": "no_qualified_model",
                       "message": f"No qualified model: this part needs level {selection.required}."}
                return
            spec = selection.model
        yield {"type": "log", "entry": {"code": "write", "model": spec.label or spec.model}}
        messages = subtask_messages(
            task.prompt, step.title, task.state_text, {}, Review(),
            evidence_text=found_text, sources=sources, previous=list(others),
            outline=list(outline), position=step.index, language=self.reply_language,
            persona=self._persona(step), profile=self.memory.block() if self.memory else "",
            conversation=task.conversation(), rules=self.rules, searched=searched,
        )
        messages.append({"role": "assistant", "content": step.output})
        for turn in thread:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append({"role": turn["role"], "content": str(turn["content"])[:6000]})
        messages.append({"role": "user", "content": (
            f"{message.strip()}\n\nRewrite your part in full with this applied. Output only the "
            "new version of your part — no preamble, no heading, no note about what changed.")})

        pieces: List[str] = []
        stream = self._client(spec, "revision", step.id).stream(
            messages, model=spec.model, max_tokens=1800, thinking=spec.thinking, purpose="revise")
        usage: dict = {}
        call_id = ""
        started = time.perf_counter()
        for event in stream:
            if self.events.cancel.is_set():
                raise Cancelled("stopped while the worker was writing")
            if "delta" in event:
                pieces.append(event["delta"])
                yield {"type": "delta", "text": event["delta"]}
            elif event.get("done"):
                usage = event.get("usage") or {}
                call_id = event.get("call_id") or ""
        call = Call(engine="llm", purpose="revise", model=spec.model,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    usage=Usage.from_dict(usage))
        if call_id:
            call.call_id = call_id
        spent.append(call)
        version = (step.version or 1) + 1
        # A plain join is patched in place; a rewritten whole now lags behind
        # its parts and says so until someone syncs it.
        needs_sync = answer_mode == "rewritten"
        self.events.emit("output.version", "llm", "revision",
                         {"target": step.id, "version": version, "title": step.title,
                          "mode": "revised", "depends_on": [], "needs_sync": False},
                         subtask_id=step.id)
        if needs_sync:
            self.events.emit("output.version", "policy", "revision",
                             {"target": "answer", "version": None, "mode": "stale",
                              "depends_on": [f"{step.id}@v{version}"], "needs_sync": True})
        yield {"type": "done", "output": _unhead("".join(pieces).strip()), "sources": sources,
               "model": spec.label or spec.model, "cost": round(sum(c.usage.cost for c in spent), 8),
               "calls": [c.to_dict() for c in spent], "version": version, "needs_sync": needs_sync,
               "operation_id": self.events.operation_id}

    # -- bringing a rewritten whole back in line with its parts ------------- #

    def sync_answer(self, task: Task, parts: Sequence[dict]) -> Iterator[dict]:
        """Rewrite the assembled answer from its current parts, and nothing else.

        Only the assembly step runs: no re-planning, no re-routing, no worker
        writes its part again. The new whole records which part versions it
        was built from.
        """
        self.events.register()
        try:
            yield from self.events.run(lambda: self._sync(task, parts))
        finally:
            self.events.unregister()

    def _sync(self, task: Task, parts: Sequence[dict]) -> Iterator[dict]:
        self.reply_language = detect_language(task.prompt, self.language)
        done = [p for p in parts if str(p.get("output") or "").strip()]
        if len(done) < 2:
            yield {"type": "error", "code": "nothing_to_sync",
                   "message": "a sync needs at least two written parts"}
            return
        need = max(int(p.get("required") or 2) for p in done)
        selection = self.roster.select_for(need)
        holder = Result(task=task, plan=_app_plan(task, False))
        if not self._selected(selection, holder, "assembly", "the whole answer"):
            yield {"type": "error", "code": "no_qualified_model",
                   "message": f"No qualified model: the rewrite needs level {selection.required}."}
            return
        messages = assembly_messages(task.prompt, [(str(p.get("title") or ""), str(p["output"]))
                                                   for p in done], language=self.reply_language)
        pieces: List[str] = []
        for event in self._stream_on(selection.model, messages, "assemble", 2000, holder,
                                     stage="assembly"):
            if "delta" in event:
                pieces.append(event["delta"])
                yield {"type": "delta", "text": event["delta"]}
            elif event.get("done"):
                call = event["call"]
        depends = [f"{p.get('id') or ''}@v{int(p.get('version') or 1)}" for p in done]
        version = int(max((p.get("answer_version") or 1) for p in done)) + 1
        self.events.emit("output.version", "llm", "assembly",
                         {"target": "answer", "version": version, "mode": "rewritten",
                          "depends_on": depends, "needs_sync": False})
        yield {"type": "done", "output": "".join(pieces).strip(), "version": version,
               "depends_on": depends, "cost": round(call.usage.cost, 8),
               "cost_source": call.usage.source}

    def _persona(self, subtask: Subtask) -> str:
        """The worker's job description, plus the house instructions if any.

        A persona the planner wrote for this step wins over the preset role:
        it was written knowing what this particular step is.
        """
        persona = subtask.persona.strip() or roles.get(subtask.role).persona
        skill = self.library.get(self.planner.skill.id) if (
            self.library and getattr(self.planner, "skill", None)
        ) else None
        if skill is not None:
            persona = f"{persona}\n\nHOUSE INSTRUCTIONS ({skill.name}):\n{skill.body}"
        return persona

    def _run_role_tools(self, role, task: Task, subtask: Subtask, result: Result) -> str:
        if self.tools is None or not role.tools:
            return ""
        prompt = f"{task.prompt}\n{subtask.title}"
        planned: List[Tuple[Any, dict]] = []
        for name in role.tools:
            tool = self.tools.get(name)
            if tool is None or tool not in self.tools.available():
                continue
            try:
                args = tool.extract(prompt, task.state_text)
            except Exception:  # noqa: BLE001
                continue
            if args is not None:
                planned.append((tool, args))

        # Everything with a side effect goes past Jev first, in one call.
        risky = [(t.name, args) for t, args in planned if t.side_effects]
        observer = self.events.observer("tools", subtask.id)
        verdicts, call = gate_actions(self.jev, task.prompt, risky, observer=observer)
        if call:
            result.ledger.add(call)
            allowed = sum(1 for ok, _ in verdicts if ok)
            result.steps.append(
                Step("gate", f"{allowed}/{len(risky)} actions allowed", engine="jev",
                     cost=call.usage.cost, latency_ms=call.latency_ms, code="tool_gate",
                     data={"allowed": allowed, "asked": len(risky)})
            )
        verdict_for = dict(zip(range(len(risky)), verdicts))

        blocks: List[str] = []
        risky_index = 0
        for tool, args in planned:
            if tool.side_effects:
                ok, probability = verdict_for.get(risky_index, (False, 0.0))
                risky_index += 1
                # Jev's yes is necessary, never sufficient: the tool's own hard
                # rules (workspace confinement, no private hosts) still apply
                # inside ``run``, and a no is final.
                self.events.emit("policy.applied", "policy", "tools",
                                 {"rule": "tools.gate_allow_at_or_above/v1", "rule_version": RULES_VERSION,
                                  "inputs": {"tool": tool.name, "probability_yes": probability,
                                             "threshold": GATE_ALLOW},
                                  "action": "run" if ok else "block"},
                                 batch_id=observer.last_batch, subtask_id=subtask.id)
                if not ok:
                    subtask.tools.append({"name": tool.name, "ok": False, "blocked": True,
                                          "detail": f"blocked by the gate (p={probability:.2f})"})
                    continue
            self.events.check()
            self.events.emit("stage.entered", "tool", "tools", {"detail": tool.name},
                             subtask_id=subtask.id)
            outcome = self.tools.run(tool.name, args)
            subtask.tools.append(outcome.to_dict())
            if outcome.ok and outcome.output.strip():
                blocks.append(f"[{tool.name}] {outcome.detail}\n{outcome.output}")
        return "\n\n".join(blocks)

    def _assemble(self, task: Task, plan: Plan, result: Result) -> Iterator[dict]:
        done = [t for t in result.subtasks if t.status == "done" and t.output]
        if not done:
            result.output = ""
            return
        if len(done) == 1:
            result.output = done[0].output
            yield {"type": "assembled", "mode": "single"}
            return
        if not (result.routing and result.routing.needs_assembly):
            # Sections that already stand on their own: stitching them with a
            # model would cost a call and change nothing.
            result.output = "\n\n".join(
                f"## {_heading(t.title)}\n\n{_unhead(t.output)}" for t in done
            )
            result.steps.append(
                Step("assemble", f"{len(done)} sections joined",
                     code="assemble_free", data={"sections": len(done)})
            )
            self.events.emit("output.version", "policy", "assembly",
                             {"target": "answer", "version": 1, "mode": "joined",
                              "depends_on": [f"{t.id}@v{t.version or 1}" for t in done]})
            yield {"type": "assembled", "mode": "joined"}
            return

        hardest = max(done, key=lambda t: t.required)
        spec = self.roster.get(hardest.model_id) or min(
            self.roster.available(), key=lambda m: m.blended_price
        )
        messages = assembly_messages(
            task.prompt, [(t.title, t.output) for t in done], language=self.reply_language
        )
        (text, call), spec = self._on_some_model(
            spec, lambda m: self._client(m, "assembly").complete(
                messages, purpose="assemble", model=m.model, thinking=m.thinking, max_tokens=2000),
            result)
        result.ledger.add(call)
        result.output = text.strip()
        self.events.emit("output.version", "llm", "assembly",
                         {"target": "answer", "version": 1, "mode": "rewritten",
                          "depends_on": [f"{t.id}@v{t.version or 1}" for t in done]})
        result.steps.append(
            Step("assemble", f"rewritten as one piece on {spec.label or spec.model}",
                 engine="llm", cost=call.usage.cost, latency_ms=call.latency_ms,
                 code="assemble", data={"model": spec.label or spec.model,
                                        "sections": len(done)})
        )
        yield {"type": "assembled", "mode": "rewritten"}

    def _escalate(self, task: Task, plan: Plan, result: Result) -> None:
        """Spend LLM money exactly where Jev admitted it was guessing."""
        # The strongest model available, cheapest among equals: this is the one
        # place the harness deliberately spends more, because Jev has said it
        # cannot call the decision.
        spec = max(self.roster.available(), key=lambda m: (m.capability, -m.blended_price))
        client = self._client(spec, "review", attempt_id="attempt-2")
        self.events.emit("policy.applied", "policy", "review",
                         {"rule": "review.escalate_low_confidence/v1", "rule_version": RULES_VERSION,
                          "inputs": {"questions": sorted(result.review.uncertain)},
                          "action": f"ask {spec.label or spec.model}",
                          "detail": "the original distributions are kept alongside the result"},
                         batch_id=getattr(self, "_decision_batch", None), attempt_id="attempt-2")
        messages = escalation_messages(
            task.prompt, task.state_text, result.review, plan.decisions,
            language=self.reply_language,
        )
        text, call = client.complete(
            messages,
            purpose="escalate",
            model=spec.model,
            max_tokens=700,
            thinking=spec.thinking,
        )
        result.ledger.add(call)
        result.escalated = True
        names = ", ".join(sorted(result.review.uncertain))
        result.output = (
            f"{result.output}\n\n— escalated to {spec.label or spec.model} "
            f"({names}) —\n{text.strip()}"
        )
        result.steps.append(
            Step(
                "escalate",
                f"{len(result.review.uncertain)} uncertain decision(s) sent to "
                f"{spec.label or spec.model}: {names}",
                engine="llm",
                cost=call.usage.cost,
                latency_ms=call.latency_ms,
                code="escalate",
                data={"model": spec.label or spec.model,
                      "count": len(result.review.uncertain), "names": names},
            )
        )

    def _guard(self, result: Result) -> None:
        """Screen the output with Jev instead of an LLM judge.

        The task travels with the output: "does this do what was asked" cannot
        be answered by something that was never told what was asked.
        """
        state = f"TASK\n{result.task.prompt.strip()}\n\nOUTPUT\n{result.output.strip()[:20000]}"
        observer = self.events.observer("guard")
        try:
            raw, call = self.jev.decide(
                state,
                {n: q.to_payload() for n, q in GUARD_QUESTIONS.items()},
                purpose="guard", observer=observer,
                targets={n: "the answer" for n in GUARD_QUESTIONS},
            )
        except Cancelled:
            raise
        except ProviderError:
            # An unchecked answer is still the answer; the check just did not happen.
            self._guard_batch = observer.last_batch
            return
        self._guard_batch = observer.last_batch
        result.ledger.add(call)
        result.guard = {
            name: parse_answer(name, raw[name]) for name in GUARD_QUESTIONS if name in raw
        }
        verdict = ", ".join(f"{n}={a.describe()}" for n, a in result.guard.items())
        result.steps.append(
            Step("guard", verdict or "no verdict", engine="jev",
                 cost=call.usage.cost, latency_ms=call.latency_ms,
                 code="guard",
                 data={n: round(getattr(a, "probability", a.certainty), 3)
                       for n, a in result.guard.items()})
        )

    @staticmethod
    def _missed(result: Result) -> bool:
        on_task = result.guard.get("on_task")
        refused = result.guard.get("refused")
        off = on_task is not None and getattr(on_task, "probability", 1.0) < 0.5
        dodged = refused is not None and getattr(refused, "probability", 0.0) >= 0.5
        return off or dodged

    def _retry(self, task: Task, plan: Plan, result: Result, state: str) -> None:
        spec = result.selection.model
        messages = generation_messages(
            plan.generation.instruction, state, result.decisions, result.review,
            include_state=plan.generation.include_state,
            evidence=result.evidence.as_text() if result.evidence else "",
            sources=result.evidence.sources() if result.evidence else (),
            language=self.reply_language,
            profile=self.memory.block() if self.memory else "",
            conversation=task.conversation(),
            rules=self.rules,
            searched=result.evidence.queries if result.evidence else (),
        )
        messages.append({"role": "assistant", "content": result.output})
        messages.append({"role": "user", "content": (
            "A reviewer judged that this does not do what was asked, or avoids "
            "doing it. Do the task itself now, fully, without apologising or "
            "commenting on the previous attempt."
        )})
        self.events.emit("policy.applied", "policy", "guard",
                         {"rule": "guard.retry_once_when_off_task/v1", "rule_version": RULES_VERSION,
                          "inputs": {n: getattr(a, "probability", None) for n, a in result.guard.items()},
                          "action": f"one more attempt on {spec.label or spec.model}"},
                         batch_id=getattr(self, "_guard_batch", None), attempt_id="attempt-2")
        text, call = self._client(spec, "generation", attempt_id="attempt-2").complete(
            messages, purpose="retry", model=spec.model, thinking=spec.thinking,
            max_tokens=plan.generation.max_tokens,
        )
        result.ledger.add(call)
        if text.strip():
            result.output = text.strip()
        result.steps.append(
            Step("retry", f"second attempt on {spec.label or spec.model}", engine="llm",
                 cost=call.usage.cost, latency_ms=call.latency_ms, code="retry",
                 data={"model": spec.label or spec.model})
        )

    # -- rendering ---------------------------------------------------------- #

    @staticmethod
    def _render_decisions(result: Result) -> str:
        if not result.decisions:
            return ""
        body = facts_block(result.decisions, mark_uncertain=result.review.uncertain)
        return body


_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_TRAILING_NOUN = re.compile(r"\s+(section|part|chapter)$", re.IGNORECASE)
_OWN_HEADING = re.compile(r"^\s*(?:#{1,6}\s*\S.*|\*\*[^*\n]{1,60}\*\*)\n+")


def _heading(title: str) -> str:
    """A step title, tidied into something that reads as a heading."""
    text = _TRAILING_NOUN.sub("", _LEADING_ARTICLE.sub("", title.strip()))
    return (text[:1].upper() + text[1:]) if text else title


def _unhead(text: str) -> str:
    """Drop a heading the model wrote anyway, so it is not printed twice."""
    return _OWN_HEADING.sub("", text, count=1).strip() or text.strip()


APP_FILL_LEVEL = 2
# The evidence budgets of the prompts that receive it. Sufficiency is checked
# against exactly these, so the check and the prompt see the same set.
MAX_EVIDENCE_CHARS = MAX_STATE_CHARS
SUBTASK_EVIDENCE_CHARS = 9000


class _Observed:
    """An LLM client whose calls report to one run. Nothing else changes."""

    def __init__(self, client, observer) -> None:
        self._client_obj, self._observer = client, observer

    def complete(self, messages, **kwargs):
        kwargs.setdefault("observer", self._observer)
        return self._client_obj.complete(messages, **kwargs)

    def stream(self, messages, **kwargs):
        kwargs.setdefault("observer", self._observer)
        return self._client_obj.stream(messages, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client_obj, name)


def _observed(client, observer):
    if client is None or isinstance(client, _Observed):
        return client
    return _Observed(client, observer)


def _cache_version(planner) -> str:
    from .planner import CACHE_VERSION

    return CACHE_VERSION


def _usage_of(call: Optional[Call]) -> Optional[dict]:
    from .events import call_usage

    return call_usage(call) if call is not None else None


def _uncertain_rule(answer: Answer, thresholds: Thresholds) -> dict:
    """The band an answer fell into, stated as the rule that put it there."""
    if answer.kind == "noul":
        return {"rule": "noul.uncertain_band/v1", "low": thresholds.noul_uncertain_low,
                "high": thresholds.noul_uncertain_high, "measure": "P(yes)"}
    return {"rule": "confidence.abstain_below/v1", "below": thresholds.decision_abstain_below,
            "measure": "confidence"}


def _plan_lines(plan: Plan) -> str:
    """The plan as a person reads it: what it will do, in order."""
    lines = [plan.strategy.strip() or "Answer the task"]
    if plan.research is not None:
        lines.append(f"· look things up ({plan.research.rounds} round(s))")
    for index, step in enumerate(plan.steps, 1):
        title = step if isinstance(step, str) else getattr(step, "title", "")
        lines.append(f"{index}. {title}")
    if plan.decisions:
        lines.append(f"· {len(plan.decisions)} typed judgement(s) for Jev")
    if plan.generation is not None and not plan.steps:
        lines.append(f"· write: {plan.generation.instruction.strip()[:160]}")
    return "\n".join(lines)


def _approved(answer: str) -> bool:
    text = (answer or "").strip().lower()
    return text.startswith(("run", "yes", "ok", "go", "好", "可以", "开始", "执行"))


def _argument_line(arguments: Mapping[str, Any]) -> str:
    """One readable line of arguments, for a question a person has to answer fast."""
    parts = []
    for key, value in list((arguments or {}).items())[:4]:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parts.append(f"{key}={text[:120]}")
    return " ".join(parts)[:300]


def _reason(exc: Exception) -> str:
    """The upstream's own words for a refusal, short enough to show."""
    detail = getattr(exc, "detail", None)
    text = str(detail or exc)
    match = re.search(r'"message"\s*:\s*"([^"]+)"', text)
    return (match.group(1) if match else str(exc))[:160]

APP_CODE_LEVEL = 3       # writing a working program is reasoning-and-code work


def _app_plan(task: Task, research: bool) -> Plan:
    return Plan(
        strategy="compose an app from the component catalogue",
        answer_from=FROM_GENERATION,
        generation=Generation(instruction=task.prompt.strip(), include_state=True),
        research=Research(rounds=1, top_k=3) if research else None,
        source=SOURCE_DETERMINISTIC,
        strategy_code="app",
    )


def _with_research(plan: Plan, spec: Research) -> Plan:
    """The same plan, with a research stage added."""
    return Plan(
        strategy=plan.strategy,
        answer_from=plan.answer_from,
        decisions=plan.decisions,
        gate=plan.gate,
        generation=plan.generation,
        guard=plan.guard,
        research=spec,
        steps=plan.steps,
        source=plan.source,
        strategy_code=plan.strategy_code,
        notes=plan.notes,
    )
