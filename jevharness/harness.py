"""The assembled harness: credentials in, answers out.

This is the only module that knows how the pieces fit together, and the only
one that touches API keys. Keys arrive per request from the caller's browser
(or from .env during local development), are used, and are not written down.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from .config import Config, Thresholds, load_config
from .errors import ConfigError, SchemaError
from . import roles
from .agent import Subtask
from .events import RunEvents
from .executor import Executor, Result
from .ledger import Ledger
from .plan import Plan, Task
from .planner import Planner, PlanCache
from .providers import JevClient, LLMClient, Transport
from .questions import Noul, Question, parse_answer, question_from_dict
from .memory import Memory as UserMemory, house_rules
from . import vendors
from .skills import Library
from .roster import Credential, ModelSpec, Roster, default_models
from .tools import Registry, ToolContext, preset as tools_preset

DEFAULT_JEV_MODEL = "~typesafe/jev-latest"
# Decision-event records, one file per run, inside the workspace's own folder.
RUNS_DIR = Path(".jevia") / "runs"


@dataclass
class Settings:
    """Everything a single run needs, and nothing that outlives it."""

    roster: Roster
    jev_model: str = DEFAULT_JEV_MODEL
    jev_credential_ref: str = "default"
    compiler_model_id: Optional[str] = None
    thresholds: Thresholds = field(default_factory=Thresholds)
    escalate_uncertain: bool = True
    guard_output: bool = True
    research_enabled: bool = True
    skill: Optional[str] = None
    workspace: Optional[str] = None
    tools_enabled: bool = True
    # Which tools a run may call: read, write (the default) or full.
    tools: str = "write"
    memory_enabled: bool = True
    # "auto": Jev decides when the answer should be a working screen.
    app_mode: str = "auto"
    # Explicit user permission to run a step on a weaker model when none
    # reaches its required level. Off: such steps wait for review.
    allow_downgrade: bool = False
    # R01, behind a flag: extra routing dimensions (evidence complete,
    # contradictions). Off until it is shown to help.
    routing_extra: bool = False
    # The agent loop: a step is one request and the tools it calls. Off makes
    # every generation a single request, as it was before the loop existed.
    agent_loop: bool = True
    max_steps: int = 8
    # Stop and ask before anything with a side effect runs, and show the plan
    # before the work starts. Both off unless the user asks for them.
    approve_changes: bool = False
    plan_mode: bool = False
    # "thrift" buys the cheapest model that clears each rung; "best" buys the
    # strongest available. The judgement is the same either way.
    quality: str = "thrift"
    request_timeout: float = 90.0
    # Which language the planner model should write its descriptions in, so the
    # product does not mix two languages on one screen.
    language: str = "en"

    def validate(self) -> None:
        self.roster.validate()
        self.thresholds.validate()
        if not self.jev_model:
            raise ConfigError("jev_model is required")
        if self.jev_credential_ref not in self.roster.credentials:
            raise ConfigError(
                f"jev needs credential {self.jev_credential_ref!r}, which is not configured"
            )
        jev_vendor = vendors.get(self.roster.credentials[self.jev_credential_ref].vendor)
        if not jev_vendor.serves_jev:
            raise ConfigError(
                "Jev is only served by OpenRouter or TypeSafe — add a key for one of them"
            )
        for spec in self.roster.available():
            if not vendors.can_chat(self.roster.credentials[spec.credential_ref]):
                raise ConfigError(
                    f"model {spec.label or spec.model} is attached to a key that cannot chat"
                )
        if self.compiler_model_id and self.roster.get(self.compiler_model_id) is None:
            raise ConfigError(f"unknown compiler model {self.compiler_model_id!r}")


    # -- construction ------------------------------------------------------- #

    @classmethod
    def from_env(cls, config: Optional[Config] = None) -> "Settings":
        """Local-development convenience: one key from .env, default roster."""
        config = config or load_config()
        if not config.has_key:
            raise ConfigError(
                "no API key. Enter one in the web UI, or set OPENROUTER_API_KEY in .env"
            )
        credential = Credential(
            ref="default",
            api_key=config.api_key,
            base_url=vendors.normalise_base(vendors.get("openrouter"), config.base_url),
            label="OpenRouter (.env)",
            vendor="openrouter",
        )
        roster = Roster(
            models=default_models(),
            credentials={"default": credential},
        )
        settings = cls(
            roster=roster,
            jev_model=config.jev_model or DEFAULT_JEV_MODEL,
            thresholds=config.thresholds,
            guard_output=config.guard_llm_output,
        )
        settings.roster.round_up_below_certainty = (
            config.thresholds.capability_round_up_below
        )
        settings.validate()
        return settings

    @classmethod
    def from_request(cls, raw: Any, *, fallback: Optional[Config] = None) -> "Settings":
        """Build from a UI payload. The caller's own keys, used once."""
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise SchemaError("settings must be an object")

        roster = Roster.from_dict(raw.get("roster"))

        # If the caller sent no usable credential but the operator has one in
        # .env, fall back to it so a local clone works out of the box.
        if not roster.credentials and fallback is not None and fallback.has_key:
            roster.credentials["default"] = Credential(
                ref="default",
                api_key=fallback.api_key,
                base_url=vendors.normalise_base(vendors.get("openrouter"), fallback.base_url),
                label="OpenRouter (.env)",
                vendor="openrouter",
            )
        if not roster.credentials:
            raise ConfigError(
                "no API key. Enter one under Keys & models, or set "
                "OPENROUTER_API_KEY in .env"
            )

        default_ref = next(
            (ref for ref, c in roster.credentials.items() if vendors.can_chat(c)),
            next(iter(roster.credentials)),
        )
        if not roster.models:
            roster.models = default_models(default_ref)
        else:
            # A model may point at a credential the caller left blank. Repair
            # it rather than refusing the whole request over a UI detail.
            roster.models = [
                spec
                if spec.credential_ref in roster.credentials
                else replace(spec, credential_ref=default_ref)
                for spec in roster.models
            ]

        thresholds = Thresholds()
        for key, value in (raw.get("thresholds") or {}).items():
            if hasattr(thresholds, key):
                try:
                    setattr(thresholds, key, float(value))
                except (TypeError, ValueError) as exc:
                    raise SchemaError(f"threshold {key!r} must be a number") from exc
        thresholds.validate()
        roster.round_up_below_certainty = thresholds.capability_round_up_below

        settings = cls(
            roster=roster,
            jev_model=str(raw.get("jev_model") or "").strip() or _default_jev_model(roster, raw),
            jev_credential_ref=_jev_ref(raw, roster, default_ref),
            compiler_model_id=(
                str(raw["compiler_model_id"]).strip()
                if raw.get("compiler_model_id")
                else None
            ),
            thresholds=thresholds,
            escalate_uncertain=bool(raw.get("escalate_uncertain", True)),
            guard_output=bool(raw.get("guard_output", True)),
            research_enabled=bool(raw.get("research_enabled", True)),
            skill=str(raw["skill"]).strip() if raw.get("skill") else None,
            workspace=str(raw["workspace"]).strip() if raw.get("workspace") else None,
            tools_enabled=bool(raw.get("tools_enabled", True)),
            tools=str(raw.get("tools") or "write").strip().lower(),
            memory_enabled=bool(raw.get("memory_enabled", True)),
            app_mode=str(raw.get("app_mode") or "auto").strip().lower(),
            language=str(raw.get("language") or "en").strip().lower()[:5],
            allow_downgrade=bool(raw.get("allow_downgrade", False)),
            routing_extra=bool(raw.get("routing_extra", False)),
            agent_loop=bool(raw.get("agent_loop", True)),
            max_steps=max(1, min(int(raw.get("max_steps") or 8), 24)),
            approve_changes=bool(raw.get("approve_changes", False)),
            plan_mode=bool(raw.get("plan_mode", False)),
            quality=str(raw.get("quality") or "thrift").strip().lower(),
        )
        roster.allow_downgrade = settings.allow_downgrade
        roster.quality = settings.quality if settings.quality in ("thrift", "best") else "thrift"
        settings.validate()
        return settings


def _default_jev_model(roster: Roster, raw: Mapping[str, Any]) -> str:
    ref = _jev_ref(raw, roster, next(iter(roster.credentials), "default"))
    credential = roster.credentials.get(ref)
    vendor = vendors.get(credential.vendor) if credential else vendors.get(vendors.DEFAULT)
    return vendor.jev_model or DEFAULT_JEV_MODEL


def _jev_ref(raw: Mapping[str, Any], roster: Roster, default_ref: str) -> str:
    """The credential Jev should use: the one named, else the first that serves it."""
    named = str(raw.get("jev_credential_ref") or "").strip()
    if named in roster.credentials and vendors.get(roster.credentials[named].vendor).serves_jev:
        return named
    return next(
        (ref for ref, c in roster.credentials.items() if vendors.get(c.vendor).serves_jev),
        named if named in roster.credentials else default_ref,
    )


def parse_task(raw: Any) -> Task:
    if not isinstance(raw, Mapping):
        raise SchemaError("task must be an object")
    questions_raw = raw.get("questions")
    questions: Optional[Dict[str, Question]] = None
    if questions_raw:
        if not isinstance(questions_raw, Mapping):
            raise SchemaError("task 'questions' must be an object")
        questions = {
            name: question_from_dict(name, spec) for name, spec in questions_raw.items()
        }
    history = []
    for item in raw.get("history") or []:
        if isinstance(item, Mapping) and item.get("role") in ("user", "assistant"):
            text = str(item.get("content") or "").strip()
            if text:
                history.append((item["role"], text))
    targets = None
    if isinstance(raw.get("targets"), Mapping) and questions:
        targets = {str(k)[:80]: str(v)[:160] for k, v in raw["targets"].items() if k in questions}
    task = Task(
        prompt=str(raw.get("prompt") or ""),
        state=raw.get("state") if raw.get("state") is not None else "",
        questions=questions,
        history=tuple(history[-40:]),
        targets=targets,
    )
    task.validate()
    return task


class Harness:
    """Assembles transports, clients, planner and executor for one settings set.

    The plan cache is shared across runs of the same Harness, which is where
    the repeat-task saving comes from. The server keeps one Harness per distinct
    settings fingerprint so cached plans survive between requests.
    """

    def __init__(self, settings: Settings, *, cache: Optional[PlanCache] = None) -> None:
        settings.validate()
        self.settings = settings
        self.cache = cache if cache is not None else PlanCache(
            workspace=Path(settings.workspace).expanduser() if settings.workspace else None
        )
        self._transports: Dict[str, Transport] = {}
        self._library: Optional[Library] = None
        self._memory: Optional[UserMemory] = None
        self._mcp = None

    # -- wiring ------------------------------------------------------------- #

    def _transport(self, ref: str) -> Transport:
        if ref not in self._transports:
            credential = self.settings.roster.credentials.get(ref)
            if credential is None:
                raise ConfigError(f"no credential {ref!r}")
            vendor = vendors.get(credential.vendor)
            self._transports[ref] = Transport(
                credential.base_url,
                credential.api_key,
                timeout=self.settings.request_timeout,
                headers=vendor.headers(credential.api_key),
            )
        return self._transports[ref]

    def _jev(self) -> JevClient:
        """Jev lives at its own endpoint, at whichever vendor serves it."""
        credential = self.settings.roster.credentials[self.settings.jev_credential_ref]
        vendor = vendors.get(credential.vendor)
        transport = Transport(
            vendor.decisions_root,
            credential.api_key,
            timeout=self.settings.request_timeout,
            headers=vendor.headers(credential.api_key),
        )
        return JevClient(transport, self.settings.jev_model, path=vendor.decisions_path)

    def _llm_for(self, spec: ModelSpec) -> LLMClient:
        credential = self.settings.roster.credentials[spec.credential_ref]
        vendor = vendors.get(credential.vendor)
        return LLMClient(
            self._transport(spec.credential_ref),
            spec.model,
            dialect=vendor.dialect,
            stream_usage=vendor.stream_usage,
            price=(spec.price_in, spec.price_out) if spec.priced else None,
        )

    def _compiler_spec(self) -> Optional[ModelSpec]:
        """Cheapest model competent enough to emit valid plan JSON."""
        if self.settings.compiler_model_id:
            return self.settings.roster.get(self.settings.compiler_model_id)
        candidates = [m for m in self.settings.roster.available() if m.capability >= 2]
        if not candidates:
            candidates = self.settings.roster.available()
        if not candidates:
            return None
        return min(candidates, key=lambda m: m.blended_price)

    def _stronger_than(self, spec: Optional[ModelSpec]) -> Optional[ModelSpec]:
        """The cheapest model above ``spec``'s rung, to re-plan when it fails."""
        if spec is None:
            return None
        above = [m for m in self.settings.roster.available()
                 if m.capability > spec.capability and m.model != spec.model]
        return min(above, key=lambda m: m.blended_price) if above else None

    def library(self) -> Library:
        """Built-in skills plus whatever the workspace adds."""
        if self._library is None:
            self._library = Library(
                Path(self.settings.workspace).expanduser() if self.settings.workspace else None
            )
        return self._library

    def memory(self) -> UserMemory:
        if self._memory is None:
            self._memory = UserMemory(
                Path(self.settings.workspace).expanduser()
                if (self.settings.workspace and self.settings.memory_enabled) else None
            )
        return self._memory

    def _fleet(self):
        """MCP servers configured in this workspace, started once per harness."""
        from . import mcp

        if self._mcp is None:
            self._mcp = mcp.Fleet.shared(
                Path(self.settings.workspace).expanduser() if self.settings.workspace else None)
        return self._mcp

    def _tools(self) -> Optional[Registry]:
        if not self.settings.tools_enabled:
            return None
        context = ToolContext.from_env(
            Path(self.settings.workspace).expanduser() if self.settings.workspace else None
        )
        if not context.workspace.is_dir():
            # A workspace that is not there is not an error; the file tools
            # simply have nowhere to look, and everything else still runs.
            return None
        return Registry.builtin(enabled=tools_preset(self.settings.tools), context=context,
                                extra=self._fleet().tools())

    def secrets(self) -> List[str]:
        """Every key this harness holds, so records can be scrubbed of them."""
        return [c.api_key for c in self.settings.roster.credentials.values() if c.api_key]

    def run_events(self, run_id: Optional[str] = None, *, operation_id: str = "initial") -> RunEvents:
        """The outlet for one run, recording to the workspace when there is one."""
        log_dir = None
        if self.settings.workspace:
            log_dir = Path(self.settings.workspace).expanduser() / RUNS_DIR
        return RunEvents(run_id, operation_id=operation_id, secrets=self.secrets(),
                         log_dir=log_dir, workspace=self.settings.workspace)

    def _executor(self, events: Optional[RunEvents] = None) -> Executor:
        compiler = self._compiler_spec()
        stronger = self._stronger_than(compiler)
        planner = Planner(
            llm=self._llm_for(compiler) if compiler else None,
            cache=self.cache,
            compiler_model=compiler.model if compiler else None,
            language=self.settings.language,
            backup=(self._llm_for(stronger), stronger.model) if stronger else None,
        )
        return Executor(
            jev=self._jev(),
            llm_factory=self._llm_for,
            planner=planner,
            roster=self.settings.roster,
            thresholds=self.settings.thresholds,
            escalate_uncertain=self.settings.escalate_uncertain,
            guard_output=self.settings.guard_output,
            research_enabled=self.settings.research_enabled,
            language=self.settings.language,
            tools=self._tools(),
            library=self.library(),
            forced_skill=self.settings.skill,
            memory=self.memory() if self.settings.memory_enabled else None,
            app_mode=self.settings.app_mode,
            events=events,
            routing_extra=self.settings.routing_extra,
            agent_loop=self.settings.agent_loop,
            max_steps=self.settings.max_steps,
            approve_changes=self.settings.approve_changes,
            plan_mode=self.settings.plan_mode,
            rules=house_rules(
                Path(self.settings.workspace).expanduser() if self.settings.workspace else None
            ),
        )

    # -- public ------------------------------------------------------------- #

    def run(self, task: Task) -> Result:
        return self._executor().run(task)

    def stream(self, task: Task, events: Optional[RunEvents] = None) -> Iterator[dict]:
        return self._executor(events).stream(task)

    def sync(self, task: Task, parts: Any, events: Optional[RunEvents] = None) -> Iterator[dict]:
        """Rewrite the whole from its current parts — the only step a sync needs."""
        return self._executor(events).sync_answer(task, parts)

    def revise(self, task: Task, step: Mapping[str, Any], message: str, *,
               thread: Any = (), outline: Any = (), others: Any = (),
               events: Optional[RunEvents] = None, answer_mode: str = "") -> Iterator[dict]:
        """Talk to one worker from a finished run; it rewrites its own part."""
        if not str(message or "").strip():
            raise SchemaError("say what the worker should change")
        worker = Subtask(
            title=str(step.get("title") or "")[:200], index=int(step.get("index") or 0),
            role=str(step.get("role") or roles.DEFAULT_ROLE), persona=str(step.get("persona") or "")[:2000],
            required=int(step.get("required") or 2), model_id=str(step.get("model_id") or ""),
            output=str(step.get("output") or "")[:12000],
            sources=[s for s in (step.get("sources") or []) if isinstance(s, Mapping)][:12],
            id=str(step.get("id") or "")[:40], version=int(step.get("version") or 1),
        )
        thread = [t for t in (thread or []) if isinstance(t, Mapping)][-12:]
        outline = [str(x)[:200] for x in (outline or [])][:12]
        others = [(str(o[0])[:200], str(o[1])[:3000]) for o in (others or [])
                  if isinstance(o, (list, tuple)) and len(o) == 2][:10]
        return self._executor(events).revise_subtask(task, worker, str(message)[:4000], thread=thread,
                                                     outline=outline, others=others,
                                                     answer_mode=answer_mode)

    def plan_only(self, task: Task) -> Plan:
        """Produce the plan without answering anything. Free when recognised."""
        plan, call = self._executor().planner.plan(task)
        return plan

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Result:
        """Direct typed access: no planning, no LLM, one Jev call."""
        task = Task(prompt="direct typed decision", state=state, questions=questions)
        return self.run(task)

    def check_goal(self, goal: str, output: str) -> dict:
        """R03: countable conditions checked by code, the rest by one Jev question each."""
        from . import goals

        return goals.check(goals.parse(goal), output,
                           judge=lambda text, out: self.goal_met(text, out))

    def goal_met(self, goal: str, output: str) -> Optional[float]:
        """How likely it is that ``output`` meets ``goal``. One Jev question."""
        if not goal.strip() or not output.strip():
            return None
        question = Noul(instructions=(
            "Does the work below fully meet this goal, with nothing left to do? "
            f"Goal: {goal.strip()}"
        ))
        try:
            answers, _ = self._jev().decide(output[:20000], {"met": question.to_payload()},
                                             purpose="loop")
        except Exception:  # noqa: BLE001 - an unanswered check means keep going
            return None
        answer = answers.get("met")
        if not answer:
            return None
        return float(getattr(parse_answer("met", answer), "probability", 0.0))

    def describe(self) -> dict:
        compiler = self._compiler_spec()
        return {
            "jev_model": self.settings.jev_model,
            "compiler_model": compiler.model if compiler else None,
            "roster": self.settings.roster.to_public_dict(),
            "thresholds": {
                "decision_abstain_below": self.settings.thresholds.decision_abstain_below,
                "noul_uncertain_low": self.settings.thresholds.noul_uncertain_low,
                "noul_uncertain_high": self.settings.thresholds.noul_uncertain_high,
                "capability_round_up_below": self.settings.thresholds.capability_round_up_below,
            },
            "escalate_uncertain": self.settings.escalate_uncertain,
            "guard_output": self.settings.guard_output,
            "research_enabled": self.settings.research_enabled,
            "skill": self.settings.skill,
            "workspace": self.settings.workspace,
            "skills": self.library().catalogue(),
            "memory": self.memory().to_dict(),
            "plan_cache": self.cache.stats(),
            "tools": self._tools().to_public_dict() if self._tools() else None,
            "tools_preset": self.settings.tools,
            "mcp": self._fleet().status() if self.settings.workspace else [],
            "agent_loop": self.settings.agent_loop,
            "quality": self.settings.quality,
            "language": self.settings.language,
        }
