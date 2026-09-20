"""The decision-event protocol: what the interface is told, and in what order.

A run already streams its old events (``plan``, ``subtask_delta``, ``done`` …).
This module adds one more kind, ``decision_event``, which records the moments
the interface has to be truthful about: a batch of questions going to Jev and
coming back, a rule turning a raw answer into an action, a model being chosen
or refused, a generation call not being made.

Three properties are the point of the design:

* **Started arrives first.** Every blocking call runs on a worker thread while
  the stream reads one queue, so ``batch.started`` reaches the browser while
  the provider is still thinking — not replayed after it answered.
* **One exit.** Events are put on the queue from any thread; ``seq`` is assigned
  only where they leave it, so the order the browser sees is the order in the
  record, and two runs never share a counter.
* **Nothing unvetted.** Payload fields are whitelisted per kind and every
  string is redacted and cut to size before it leaves the process.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

from .errors import HarnessError
from .redact import redact, scrub

EVENT_VERSION = 1
RULES_VERSION = "policy-2026-09-20"
ACTORS = ("jev", "policy", "llm", "tool")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$")
MAX_TEXT = 400
MAX_ITEMS = 200
MAX_LOGGED = 5000

# The payload fields each kind may carry. Anything else is dropped, so a
# careless caller cannot put a raw response or a header on the wire.
KIND_FIELDS: Dict[str, frozenset] = {k: frozenset(v) for k, v in {
    "run.started": ("reply_language", "ui_language", "app_mode", "operation", "rules_version"),
    "run.completed": ("status", "llm_calls", "jev_calls", "jev_questions", "tool_calls",
                      "cost", "generation", "needs_review"),
    "run.failed": ("reason", "code"),
    "run.cancel_requested": ("source",),
    "run.cancelled": ("in_flight", "may_bill", "note"),
    "stage.entered": ("detail",),
    "batch.started": ("purpose", "questions", "count"),
    "batch.completed": ("purpose", "results", "missing", "count"),
    "batch.failed": ("purpose", "reason", "count", "questions"),
    "llm.started": ("purpose", "model", "model_id"),
    "llm.completed": ("purpose", "model", "input_tokens", "output_tokens", "chars"),
    "llm.failed": ("purpose", "model", "reason", "streamed_chars", "may_bill"),
    "evidence.filtered": ("round", "counts", "items", "review_policy", "batches", "items_total"),
    "evidence.checked": ("round", "result", "probability_yes", "threshold", "delivered",
                         "delivered_chars", "limit", "scope", "checked_set_id", "reason"),
    "evidence.stopped": ("stop_reason", "rounds", "gaps", "result"),
    "routing.assessed": ("title", "raw_score", "levels", "confidence", "probabilities",
                         "role", "role_source", "role_probability", "web_probability",
                         "done_probability", "extra"),
    "routing.selected": ("title", "required_capability", "raw_score", "adopted", "candidates",
                         "selected", "basis", "downgraded", "rules"),
    "routing.unavailable": ("title", "required_capability", "best_available", "candidates",
                            "options", "reason"),
    "generation.skipped": ("scope", "reason", "rule", "llm_calls_so_far", "title"),
    # The agent loop: a step is one request, a turn is steps until nothing is owed.
    "step.started": ("step", "tools_offered", "context_tokens"),
    "step.completed": ("step", "outcome", "tools", "chars", "detail"),
    "tool.called": ("tool", "call_id", "arguments", "allowed", "reason"),
    "tool.result": ("tool", "call_id", "ok", "detail", "chars", "ms"),
    "context.compacted": ("before_tokens", "after_tokens", "shadowed", "summary_chars"),
    "question.asked": ("question", "options", "kind", "call_id"),
    "question.answered": ("answer", "call_id", "source"),
    "plan.proposed": ("plan", "step_count"),
    "plan.decided": ("approved", "feedback"),
    "policy.applied": ("rule", "rule_version", "inputs", "action", "detail"),
    "policy.review_required": ("reason", "detail", "options", "distribution", "threshold",
                               "blocking"),
    # P1: outputs, app components and loops.
    "output.version": ("target", "version", "depends_on", "mode", "needs_sync", "title"),
    "component.slots": ("kind", "components", "layout", "side"),
    "component.filled": ("components", "example_data", "title"),
    "component.failed": ("reason",),
    "loop.iteration": ("iteration", "max_runs", "conditions", "outcome"),
}.items()}


class Cancelled(HarnessError):
    code = "cancelled"
    status = 409


def new_run_id() -> str:
    return "run-" + uuid.uuid4().hex[:16]


def valid_run_id(value: Any) -> Optional[str]:
    text = str(value or "")
    return text if RUN_ID.match(text) else None


def _short(value: Any, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip(value: Any, depth: int = 0) -> Any:
    """Bound every string and list so one event cannot carry a page."""
    if depth > 6:
        return None
    if isinstance(value, str):
        return _short(value)
    if isinstance(value, Mapping):
        return {str(k)[:80]: _clip(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [_clip(v, depth + 1) for v in list(value)[:MAX_ITEMS]]
    if isinstance(value, float):
        return round(value, 6)
    return value


# --------------------------------------------------------------------------- #
# Runs in flight, so a cancel request can find them.
# --------------------------------------------------------------------------- #

_ACTIVE: Dict[str, "RunEvents"] = {}
_ACTIVE_LOCK = threading.Lock()


def active(run_id: str) -> Optional["RunEvents"]:
    with _ACTIVE_LOCK:
        return _ACTIVE.get(run_id)


class RunEvents:
    """One run's event outlet, cancel flag and record."""

    def __init__(self, run_id: Optional[str] = None, *, operation_id: str = "initial",
                 secrets: Iterable[str] = (), log_dir: Optional[Path] = None,
                 workspace: Optional[str] = None) -> None:
        self.run_id = valid_run_id(run_id) or new_run_id()
        self.operation_id = operation_id
        self.attempt_id = "attempt-1"
        self.workspace = workspace
        self.secrets = tuple(s for s in secrets if s and len(s) >= 8)
        self.cancel = threading.Event()
        self.cancel_announced = False
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        # Questions waiting on a person, by question id.
        self._asked: Dict[str, List[Any]] = {}
        self._ids = itertools.count(1)
        self._batches = itertools.count(1)
        self._versions = itertools.count(1)
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._seq = 0
        self.state_version = "state-v0"
        self.log: List[dict] = []
        self.calls: Dict[str, dict] = {}
        # Counted as they are emitted (any thread), for the run's own summary.
        self.counts: Dict[str, int] = {}
        self.log_path = (log_dir / f"{self.run_id}.jsonl") if log_dir else None

    # -- lifecycle ---------------------------------------------------------- #

    def register(self) -> "RunEvents":
        with _ACTIVE_LOCK:
            _ACTIVE[self.run_id] = self
        return self

    def unregister(self) -> None:
        with _ACTIVE_LOCK:
            if _ACTIVE.get(self.run_id) is self:
                _ACTIVE.pop(self.run_id, None)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def next_batch(self, stage: str) -> str:
        with self._lock:
            return f"batch-{stage}-{next(self._batches)}"

    def bump_state(self) -> str:
        with self._lock:
            self.state_version = f"state-v{next(self._versions)}"
            return self.state_version

    # -- emitting ----------------------------------------------------------- #

    def emit(self, kind: str, actor: str, stage: str, payload: Optional[Mapping] = None, *,
             batch_id: Optional[str] = None, question_id: Optional[str] = None,
             subtask_id: Optional[str] = None, attempt_id: Optional[str] = None,
             evidence_set_id: Optional[str] = None, usage: Optional[Mapping] = None,
             operation_id: Optional[str] = None) -> dict:
        allowed = KIND_FIELDS.get(kind)
        if allowed is None:
            raise ValueError(f"unknown decision event kind {kind!r}")
        if actor not in ACTORS:
            raise ValueError(f"unknown actor {actor!r}")
        body = {k: v for k, v in (payload or {}).items() if k in allowed}
        with self._lock:
            number = next(self._ids)
            self.counts[kind] = self.counts.get(kind, 0) + 1
            if kind == "batch.started":
                self.counts["jev_questions"] = self.counts.get("jev_questions", 0) + int(body.get("count") or 0)
            if kind == "stage.entered" and actor == "tool":
                self.counts["tool"] = self.counts.get("tool", 0) + 1
        event = {
            "type": "decision_event",
            "event_version": EVENT_VERSION,
            "event_id": f"{self.run_id}-e{number}",
            "seq": None,
            "run_id": self.run_id,
            "operation_id": operation_id or self.operation_id,
            "attempt_id": attempt_id or self.attempt_id,
            "batch_id": batch_id,
            "question_id": question_id,
            "subtask_id": subtask_id,
            "kind": kind,
            "actor": actor,
            "stage": stage,
            "state_version": self.state_version,
            "evidence_set_id": evidence_set_id,
            "elapsed_ms": self.elapsed_ms(),
            "usage": _usage(usage),
            "payload": scrub(_clip(body), self.secrets),
        }
        self._queue.put(("event", event))
        return event

    def put(self, event: dict) -> None:
        """An old-style event, passed through the same exit."""
        self._queue.put(("event", event))

    def stamp(self, event: dict) -> dict:
        """Called only at the queue's exit: numbering happens in one place."""
        if event.get("type") != "decision_event":
            return event
        self._seq += 1
        event["seq"] = self._seq
        usage = event.get("usage")
        if usage and usage.get("call_id"):
            # A call is charged once, however many events mention it.
            self.calls.setdefault(usage["call_id"], usage)
        if len(self.log) < MAX_LOGGED:
            self.log.append(event)
            self._write(event)
        return event

    def _write(self, event: dict) -> None:
        if not self.log_path:
            return
        try:
            first = not self.log_path.exists()
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = redact(json.dumps(event, ensure_ascii=False), self.secrets)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if first:
                # A record of what was judged is the user's own business.
                os.chmod(self.log_path, 0o600)
                os.chmod(self.log_path.parent, 0o700)
        except OSError:
            self.log_path = None      # a full disk must not sink the run

    # -- the single exit ---------------------------------------------------- #

    def run(self, work: Callable[[], Iterator[dict]]) -> Iterator[dict]:
        """Iterate ``work()`` on a worker thread and yield everything, stamped.

        The worker's own events and those any observer puts meanwhile come
        out of one queue, in the order they were put. If the reader goes away
        the cancel flag is raised, so no new model or tool call is started for
        a stream nobody is reading.
        """
        def drive() -> None:
            try:
                for event in work():
                    self._queue.put(("event", event))
            except BaseException as exc:  # noqa: BLE001 - re-raised on the reading side
                self._queue.put(("error", exc))
            finally:
                self._queue.put(("end", None))

        threading.Thread(target=drive, daemon=True, name=f"run-{self.run_id}").start()
        error: Optional[BaseException] = None
        try:
            while True:
                kind, item = self._queue.get()
                if kind == "end":
                    break
                if kind == "error":
                    error = item
                    continue
                yield self.stamp(item)
        except GeneratorExit:
            self.cancel.set()
            raise
        if error is not None:
            raise error

    def counts_llm_open(self) -> int:
        """Language-model requests started and not yet answered or failed."""
        with self._lock:
            return (self.counts.get("llm.started", 0) - self.counts.get("llm.completed", 0)
                    - self.counts.get("llm.failed", 0))

    # -- cancelling --------------------------------------------------------- #

    def request_cancel(self, source: str = "user") -> None:
        if not self.cancel.is_set():
            self.cancel.set()
            self.emit("run.cancel_requested", "policy", "run", {"source": source})

    def check(self) -> None:
        """Raise before a new model or tool call once a cancel has been asked for."""
        if self.cancel.is_set():
            raise Cancelled("the run was stopped before its next step")

    # -- asking the person ---------------------------------------------------- #

    def ask(self, question: str, *, options: Sequence[str] = (), kind: str = "question",
            stage: str = "generation", subtask_id: Optional[str] = None,
            timeout: float = 600.0) -> Optional[str]:
        """Put a question to the user and wait for the answer.

        A run that guesses when it could have asked is the thing people
        complain about, so this blocks the step rather than picking for them.
        It returns None when nobody answers in time, and raises when the run is
        cancelled while waiting — a stopped run must not sit here.
        """
        with self._lock:
            number = len(self._asked) + 1
        question_id = f"q{number}"
        waiting: List[Any] = [threading.Event(), None]
        self._asked[question_id] = waiting
        self.emit("question.asked", "policy", stage,
                  {"question": question, "options": list(options)[:6], "kind": kind},
                  question_id=question_id, subtask_id=subtask_id)
        deadline = time.monotonic() + max(5.0, timeout)
        while not waiting[0].wait(0.2):
            if self.cancel.is_set():
                self._asked.pop(question_id, None)
                raise Cancelled("the run was stopped while it was waiting for an answer")
            if time.monotonic() > deadline:
                self._asked.pop(question_id, None)
                self.emit("question.answered", "policy", stage,
                          {"answer": "", "source": "timeout"}, question_id=question_id,
                          subtask_id=subtask_id)
                return None
        answer = waiting[1]
        self._asked.pop(question_id, None)
        self.emit("question.answered", "policy", stage,
                  {"answer": str(answer)[:400], "source": "user"}, question_id=question_id,
                  subtask_id=subtask_id)
        return answer

    def answer(self, question_id: str, text: str) -> bool:
        """Deliver an answer from the interface. False when nothing was waiting."""
        waiting = self._asked.get(str(question_id))
        if waiting is None:
            return False
        waiting[1] = str(text)
        waiting[0].set()
        return True

    @property
    def waiting(self) -> List[str]:
        return list(self._asked)

    # -- observers ---------------------------------------------------------- #

    def observer(self, stage: str, subtask_id: Optional[str] = None,
                 attempt_id: Optional[str] = None) -> "JevObserver":
        return JevObserver(self, stage, subtask_id, attempt_id)

    def llm_observer(self, stage: str, subtask_id: Optional[str] = None,
                     attempt_id: Optional[str] = None) -> "LLMObserver":
        return LLMObserver(self, stage, subtask_id, attempt_id)


def _usage(usage: Optional[Mapping]) -> Optional[dict]:
    if not usage:
        return None
    cost = usage.get("cost_usd")
    return {
        "call_id": str(usage.get("call_id") or ""),
        "cost_usd": None if cost is None else round(float(cost), 8),
        "cost_source": str(usage.get("cost_source") or "unknown"),
        "price_table": str(usage.get("price_table") or ""),
    }


def call_usage(call: Any) -> Optional[dict]:
    """The usage block for one provider call: its id, cost and where the cost came from."""
    if call is None:
        return None
    usage = getattr(call, "usage", None)
    source = getattr(usage, "source", "unknown") if usage else "unknown"
    cost = getattr(usage, "cost", None) if usage else None
    return {
        "call_id": getattr(call, "call_id", ""),
        "cost_usd": None if source == "unknown" else cost,
        "cost_source": source,
        "price_table": getattr(usage, "price_table", "") if usage else "",
    }


# --------------------------------------------------------------------------- #
# What a Jev batch looked like, and what came back
# --------------------------------------------------------------------------- #

def _question_row(qid: str, spec: Mapping, target: Optional[str]) -> dict:
    kind = str(spec.get("type") or "")
    row = {"question_id": qid, "type": kind, "label": _short(spec.get("instructions"), 200)}
    if target:
        row["target"] = _short(target, 160)
    criteria = spec.get("criteria")
    if kind == "score" and isinstance(criteria, (list, tuple)):
        row["levels"] = [_short(c, 80) for c in criteria[:16]]
    elif kind == "choice" and isinstance(criteria, Mapping):
        row["options"] = {str(k)[:60]: _short(v or k, 80) for k, v in list(criteria.items())[:40]}
    return row


def result_row(qid: str, raw: Any) -> dict:
    """One answer exactly as Jev gave it, typed. Missing fields stay missing."""
    if not isinstance(raw, Mapping):
        return {"question_id": qid, "malformed": True}
    kind = raw.get("type")

    def number(key: str) -> Optional[float]:
        try:
            return None if raw.get(key) is None else float(raw[key])
        except (TypeError, ValueError):
            return None

    def dist(key: str) -> Optional[dict]:
        value = raw.get(key)
        if not isinstance(value, Mapping):
            return None
        out = {}
        for k, v in list(value.items())[:64]:
            try:
                out[str(k)[:60]] = float(v)
            except (TypeError, ValueError):
                continue
        return out

    if kind == "noul":
        return {"question_id": qid, "type": "noul", "probability_yes": number("noul"),
                "confidence": None}
    if kind == "choice":
        return {"question_id": qid, "type": "choice", "choice": str(raw.get("choice") or ""),
                "probabilities": dist("probabilities"), "confidence": number("confidence")}
    if kind == "score":
        legend = raw.get("legend") if isinstance(raw.get("legend"), Mapping) else {}
        return {"question_id": qid, "type": "score", "score": number("score"),
                "probabilities": dist("probabilities"), "confidence": number("confidence"),
                "legend": {str(k)[:8]: _short(v, 80) for k, v in list(legend.items())[:16]}}
    return {"question_id": qid, "malformed": True}


class JevObserver:
    """Reports one Jev batch: started before the request, then one final state."""

    def __init__(self, events: RunEvents, stage: str, subtask_id: Optional[str],
                 attempt_id: Optional[str]) -> None:
        self.events, self.stage = events, stage
        self.subtask_id, self.attempt_id = subtask_id, attempt_id
        self.targets: Dict[str, str] = {}
        self.last_batch: Optional[str] = None

    def started(self, purpose: str, questions: Mapping[str, Mapping],
                targets: Optional[Mapping[str, str]] = None) -> str:
        self.events.check()
        batch_id = self.events.next_batch(self.stage)
        self.last_batch = batch_id
        targets = dict(targets or self.targets)
        rows = [_question_row(q, spec, targets.get(q)) for q, spec in questions.items()]
        self.events.emit("batch.started", "jev", self.stage,
                         {"purpose": purpose, "questions": rows, "count": len(rows)},
                         batch_id=batch_id, subtask_id=self.subtask_id,
                         attempt_id=self.attempt_id)
        return batch_id

    def completed(self, batch_id: str, questions: Mapping[str, Any], answers: Mapping[str, Any],
                  call: Any, purpose: str) -> None:
        results = [result_row(q, answers[q]) for q in questions if q in answers]
        missing = [q for q in questions if q not in answers]
        self.events.emit("batch.completed", "jev", self.stage,
                         {"purpose": purpose, "results": results, "missing": missing,
                          "count": len(questions)},
                         batch_id=batch_id, subtask_id=self.subtask_id,
                         attempt_id=self.attempt_id, usage=call_usage(call))

    def failed(self, batch_id: str, questions: Mapping[str, Any], exc: BaseException,
               purpose: str) -> None:
        self.events.emit("batch.failed", "jev", self.stage,
                         {"purpose": purpose, "reason": short_reason(exc),
                          "count": len(questions), "questions": list(questions)[:MAX_ITEMS]},
                         batch_id=batch_id, subtask_id=self.subtask_id,
                         attempt_id=self.attempt_id,
                         # A request that failed may still have been billed; say
                         # we do not know rather than that it was free.
                         usage={"call_id": f"{batch_id}-call", "cost_usd": None,
                                "cost_source": "unknown"})


class LLMObserver:
    """Reports one language-model call: started, then completed or failed."""

    def __init__(self, events: RunEvents, stage: str, subtask_id: Optional[str],
                 attempt_id: Optional[str]) -> None:
        self.events, self.stage = events, stage
        self.subtask_id, self.attempt_id = subtask_id, attempt_id

    def started(self, purpose: str, model: str) -> None:
        self.events.check()
        self.events.emit("llm.started", "llm", self.stage, {"purpose": purpose, "model": model},
                         subtask_id=self.subtask_id, attempt_id=self.attempt_id)

    def completed(self, purpose: str, call: Any, chars: int) -> None:
        usage = getattr(call, "usage", None)
        self.events.emit("llm.completed", "llm", self.stage,
                         {"purpose": purpose, "model": getattr(call, "model", ""),
                          "input_tokens": getattr(usage, "input_tokens", 0),
                          "output_tokens": getattr(usage, "output_tokens", 0), "chars": chars},
                         subtask_id=self.subtask_id, attempt_id=self.attempt_id,
                         usage=call_usage(call))

    def failed(self, purpose: str, model: str, exc: BaseException, streamed: int,
               call_id: str) -> None:
        self.events.emit("llm.failed", "llm", self.stage,
                         {"purpose": purpose, "model": model, "reason": short_reason(exc),
                          "streamed_chars": streamed, "may_bill": True},
                         subtask_id=self.subtask_id, attempt_id=self.attempt_id,
                         usage={"call_id": call_id, "cost_usd": None, "cost_source": "unknown"})


def short_reason(exc: BaseException) -> str:
    """A failure in the provider's own words, short, with nothing secret in it.

    The raw error body is never passed on: it can echo headers back.
    """
    detail = getattr(exc, "detail", None)
    text = str(detail or exc)
    match = re.search(r'"message"\s*:\s*"([^"]+)"', text)
    return redact((match.group(1) if match else str(exc))[:160])
