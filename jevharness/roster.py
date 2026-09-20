"""The model roster: many keys, many models, one cost-optimal choice per step.

Jev is asked one thing only — *how much capability does this step require* —
because calibrated judgement is what it is good at. Turning that judgement into
a model pick is arithmetic, so the harness does it deterministically: take the
cheapest model that clears the required capability. That split means the answer
is money-optimal by construction, and no model is ever asked to reason about
its own price list.

The bottom rung of the ladder is the important one: level 0 means *no text has
to be written at all*, because the typed decisions Jev just made already answer
the task. Choosing a model and deciding whether any LLM is needed are therefore
the same calibrated question, and it rides along inside the same Jev call as
the task's own decisions — so both come for free, in one round trip.
"""
from __future__ import annotations

import time

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ConfigError, SchemaError
from . import vendors
from .questions import Score, ScoreAnswer

# The capability ladder. Index is the level; the text is what Jev is shown, so
# it is written as an observable property of the work, not as model marketing.
CAPABILITY_LEVELS: Tuple[str, ...] = (
    "Nothing to write: the answer is a decision, not prose. A label, a rating, "
    "a yes or no, or a field copied straight out of the material is the whole "
    "deliverable, so no text has to be composed at all.",
    "Mechanical: copy, reformat, extract fields or fill a fixed template. "
    "No judgement is required and there is one obvious correct output.",
    "Routine: ordinary drafting, translation, summarising, or small code changes "
    "against a clear specification.",
    "Hard: multi-step reasoning, unfamiliar or tricky code, or weighing material "
    "that conflicts with itself.",
    "Frontier: novel design, subtle proofs, or a high-stakes irreversible "
    "judgement where a plausible-but-wrong answer is costly.",
)
MAX_CAPABILITY = len(CAPABILITY_LEVELS) - 1
# Level 0 is answered by Jev alone; no generation model is involved.
JEV_ONLY_LEVEL = 0
MIN_MODEL_CAPABILITY = 1

# Output tokens dominate the bill for generation, so weight them when ranking
# models of equal capability.
OUTPUT_WEIGHT = 3.0
UNPRICED = 1_000.0

CAPABILITY_DECISION = "__capability__"
RESERVED_PREFIX = "__"


@dataclass(frozen=True)
class Credential:
    """One API key. Keys live in the caller's browser or environment.

    The harness holds a credential only for the lifetime of a request unless
    the operator deliberately puts one in .env for local development.
    """

    ref: str
    api_key: str
    base_url: str = "https://openrouter.ai/api/v1"
    label: str = ""
    vendor: str = "openrouter"

    def validate(self) -> None:
        if not self.ref:
            raise ConfigError("credential needs a 'ref'")
        if not self.api_key:
            raise ConfigError(f"credential {self.ref!r} has no api key")

    def redacted(self) -> str:
        if len(self.api_key) <= 12:
            return "***"
        return f"{self.api_key[:11]}...{self.api_key[-4:]}"

    def to_public_dict(self) -> dict:
        return {
            "ref": self.ref,
            "label": self.label or self.ref,
            "vendor": self.vendor,
            "base_url": self.base_url,
            "key": self.redacted(),
        }


@dataclass(frozen=True)
class ModelSpec:
    """One generation model the user has made available."""

    id: str
    model: str
    capability: int
    price_in: float = 0.0          # USD per million input tokens
    price_out: float = 0.0         # USD per million output tokens
    label: str = ""
    strengths: str = ""
    credential_ref: str = "default"
    context_length: Optional[int] = None
    enabled: bool = True
    # False when no list price could be found for this model. It stays
    # selectable, but never wins "cheapest" by default on a price of zero.
    priced: bool = True
    # Chain-of-thought is off by default; turn it on for a model that is
    # genuinely better with it and worth the tokens.
    thinking: bool = False

    def validate(self) -> None:
        if not self.id:
            raise SchemaError("model needs an 'id'")
        if self.id.startswith(RESERVED_PREFIX):
            raise SchemaError(f"model id {self.id!r} may not start with {RESERVED_PREFIX!r}")
        if not self.model:
            raise SchemaError(f"model {self.id!r} needs a provider model slug")
        if (
            not isinstance(self.capability, int)
            or not MIN_MODEL_CAPABILITY <= self.capability <= MAX_CAPABILITY
        ):
            raise SchemaError(
                f"model {self.id!r}: capability must be an int in "
                f"{MIN_MODEL_CAPABILITY}..{MAX_CAPABILITY} "
                f"(level {JEV_ONLY_LEVEL} means no model is needed)"
            )
        for name in ("price_in", "price_out"):
            if float(getattr(self, name)) < 0:
                raise SchemaError(f"model {self.id!r}: {name} must be >= 0")

    @property
    def blended_price(self) -> float:
        """Ranking price for generation work, weighted toward output tokens.

        An unknown price ranks as dear, not free: a model whose cost nobody
        could find must not be chosen as the cheap option by accident.
        """
        if not self.priced:
            return UNPRICED
        return float(self.price_in or 0) + OUTPUT_WEIGHT * float(self.price_out or 0)

    @property
    def is_free(self) -> bool:
        return self.priced and self.blended_price <= 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "model": self.model,
            "capability": self.capability,
            "price_in": self.price_in,
            "price_out": self.price_out,
            "label": self.label or self.model,
            "strengths": self.strengths,
            "credential_ref": self.credential_ref,
            "context_length": self.context_length,
            "enabled": self.enabled,
            "thinking": self.thinking,
            "priced": self.priced,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ModelSpec":
        if not isinstance(raw, Mapping):
            raise SchemaError("model entry must be an object")
        try:
            # A price nobody gave is unknown, not zero: a model must not
            # become "the cheapest" because its price field was left out.
            has_price = raw.get("price_in") is not None and raw.get("price_out") is not None
            spec = cls(
                id=str(raw.get("id") or raw.get("model") or "").strip(),
                model=str(raw.get("model") or "").strip(),
                capability=int(raw.get("capability", 1)),
                price_in=float(raw.get("price_in") or 0.0),
                price_out=float(raw.get("price_out") or 0.0),
                label=str(raw.get("label") or "").strip(),
                strengths=str(raw.get("strengths") or "").strip(),
                credential_ref=str(raw.get("credential_ref") or "default").strip(),
                context_length=int(raw["context_length"]) if raw.get("context_length") else None,
                enabled=bool(raw.get("enabled", True)),
                thinking=bool(raw.get("thinking", False)),
                priced=bool(raw.get("priced", True)) and has_price,
            )
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"model entry is malformed: {exc}") from exc
        spec.validate()
        return spec


@dataclass(frozen=True)
class Selection:
    """Why this model was chosen, or why none was needed. Shown in the trace.

    ``required`` is what the work needs and is never lowered to fit the
    roster. When nothing clears it the selection is ``blocked`` — unless the
    user allowed a downgrade, in which case the weaker model runs and the
    selection says ``downgraded``.
    """

    model: Optional[ModelSpec]
    required: int
    raw_required: float
    certainty: float
    rounded_up: bool
    considered: int
    cheaper_rejected: Sequence[str] = ()
    blocked: bool = False
    downgraded: bool = False
    # True when the low-certainty round-up was given up because no model
    # cleared it. The rung Jev actually gave is still met.
    relaxed: bool = False
    best_available: Optional[int] = None
    candidates: Sequence[dict] = ()

    @property
    def jev_only(self) -> bool:
        """True when Jev's answer is the deliverable and no LLM runs."""
        return self.model is None and not self.blocked

    @property
    def qualified(self) -> bool:
        return self.model is not None and self.model.capability >= self.required

    @property
    def selected_capability(self) -> Optional[int]:
        return self.model.capability if self.model else None

    def describe(self) -> str:
        rung = CAPABILITY_LEVELS[self.required].split(":")[0]
        if self.blocked:
            return (f"No qualified model — needs level {self.required} ({rung}); "
                    f"the best available is level {self.best_available}")
        if self.jev_only:
            reason = f"level {self.required} ({rung}) — no model needed"
        else:
            reason = f"needs level {self.required} ({rung})"
        if self.rounded_up and not self.relaxed:
            reason += ", rounded up on low certainty"
        if self.relaxed:
            reason += ", the low-certainty round-up was given up: no model clears it"
        if self.downgraded:
            reason += f", downgraded to level {self.model.capability} with permission"
        if self.jev_only:
            return f"Jev only — {reason}"
        return f"{self.model.label or self.model.model} — {reason}"

    def to_dict(self) -> dict:
        return {
            "model_id": self.model.id if self.model else None,
            "model": self.model.model if self.model else None,
            "label": (self.model.label or self.model.model) if self.model
            else ("" if self.blocked else "Jev only"),
            "jev_only": self.jev_only,
            "required": self.required,
            "required_capability": self.required,
            "selected_capability": self.selected_capability,
            "raw_required": round(self.raw_required, 3),
            "certainty": round(self.certainty, 3),
            "rounded_up": self.rounded_up,
            "considered": self.considered,
            "underpowered_but_cheaper": list(self.cheaper_rejected),
            "blocked": self.blocked,
            "downgraded": self.downgraded,
            "relaxed": self.relaxed,
            "best_available": self.best_available,
            "candidates": list(self.candidates),
            "price_known": bool(self.model and self.model.priced),
            "blended_price": round(self.model.blended_price, 4)
            if self.model and self.model.priced else None,
            "basis": "configured price table, weighted 1:3 input:output",
            "reason": self.describe(),
        }


def candidate_rows(candidates: Sequence["ModelSpec"], required: int,
                   chosen: Optional["ModelSpec"]) -> List[dict]:
    """Every candidate, whether it cleared the rung, and its configured cost."""
    rows = []
    for m in sorted(candidates, key=_rank):
        rows.append({
            "model_id": m.id, "label": m.label or m.model, "capability": m.capability,
            "eligible": m.capability >= required,
            "price": "unknown" if not m.priced else ("free" if m.is_free else "priced"),
            "cost_index": round(m.blended_price, 4) if m.priced else None,
            "selected": chosen is not None and m.id == chosen.id,
        })
    return rows


# -- models that recently refused to answer --------------------------------- #

BENCH_SECONDS = 20 * 60
_benched: Dict[str, Tuple[float, str]] = {}


def bench(model: str, reason: str) -> None:
    """Keep a model that just failed out of selection for a while."""
    _benched[model] = (time.time(), reason[:200])


def unavailable(model: str) -> Optional[str]:
    """Why ``model`` is benched, or None when it may be chosen."""
    entry = _benched.get(model)
    if entry and time.time() - entry[0] < BENCH_SECONDS:
        return entry[1]
    _benched.pop(model, None)
    return None


@dataclass
class Roster:
    """The user's models and keys, plus the cost-optimal selector."""

    models: List[ModelSpec] = field(default_factory=list)
    credentials: Dict[str, Credential] = field(default_factory=dict)
    # Bump the requirement a rung when Jev is not sure, so quality fails safe.
    round_up_below_certainty: float = 0.5
    # Off unless the user says so: when nothing clears the rung, the step is
    # held for review instead of quietly running on a weaker model.
    allow_downgrade: bool = False
    # "thrift" spends the least that clears each rung — the harness's usual
    # bargain. "best" spends the most, for work where the answer matters more
    # than the bill. Neither changes what Jev judged; both are recorded.
    quality: str = "thrift"

    # -- construction ------------------------------------------------------- #

    def validate(self) -> None:
        seen = set()
        for spec in self.models:
            spec.validate()
            if spec.id in seen:
                raise SchemaError(f"duplicate model id {spec.id!r}")
            seen.add(spec.id)
        for cred in self.credentials.values():
            cred.validate()
        for spec in self.available():
            if spec.credential_ref not in self.credentials:
                raise ConfigError(
                    f"model {spec.id!r} needs credential {spec.credential_ref!r}, "
                    "which is not configured"
                )

    def available(self) -> List[ModelSpec]:
        enabled = [m for m in self.models if m.enabled]
        # Models that just failed (region locks, withdrawn ids, outages) sit out
        # for a while — unless nothing would be left, which is a louder failure.
        usable = [m for m in enabled if not unavailable(m.model)]
        return usable or enabled

    def credential_for(self, spec: ModelSpec) -> Credential:
        cred = self.credentials.get(spec.credential_ref)
        if cred is None:
            raise ConfigError(
                f"no credential {spec.credential_ref!r} for model {spec.id!r}"
            )
        return cred

    def get(self, model_id: str) -> Optional[ModelSpec]:
        return next((m for m in self.models if m.id == model_id), None)

    # -- the Jev side ------------------------------------------------------- #

    def capability_question(self, instruction: str) -> Score:
        """The one question Jev answers about effort, and about itself.

        Level 0 is a real answer, not a degenerate one: it says the typed
        decisions already in hand are the deliverable and no prose is owed.
        """
        return Score(
            instructions=(
                "How much capability does this step require? Judge the work "
                "itself, not its length, and say so honestly if the decisions "
                "already made are the entire answer. The step is: "
                f"{instruction.strip()}"
            ),
            levels=CAPABILITY_LEVELS,
        )

    def needs_capability_question(self) -> bool:
        """Always true, and deliberately so.

        Even with a single model, the question earns its keep: level 0 retires
        the generation call altogether, and it costs nothing extra because it
        travels in the Jev call the plan was already making.
        """
        return True

    # -- the deterministic side --------------------------------------------- #

    def select(self, answer: Optional[ScoreAnswer]) -> Selection:
        """Cheapest available model that clears the required capability."""
        candidates = self.available()

        if answer is None:
            # Nothing to go on. Do not skip the LLM on a guess: pick the
            # cheapest model and let generation proceed.
            if not candidates:
                raise ConfigError("no generation models are configured")
            cheapest = min(candidates, key=_rank)
            return Selection(
                model=cheapest,
                required=max(cheapest.capability, MIN_MODEL_CAPABILITY),
                raw_required=float(cheapest.capability),
                certainty=0.0,
                rounded_up=False,
                considered=len(candidates),
                best_available=max(m.capability for m in candidates),
                candidates=candidate_rows(candidates, cheapest.capability, cheapest),
            )

        raw = float(answer.value)
        required = int(round(raw))
        required = min(max(required, 0), MAX_CAPABILITY)
        rounded_up = False
        if answer.certainty < self.round_up_below_certainty and required < MAX_CAPABILITY:
            required += 1
            rounded_up = True

        if required <= JEV_ONLY_LEVEL:
            # Jev says the decisions are the deliverable. Retire the LLM call.
            return Selection(
                model=None,
                required=JEV_ONLY_LEVEL,
                raw_required=raw,
                certainty=answer.certainty,
                rounded_up=rounded_up,
                considered=len(candidates),
            )

        if not candidates:
            raise ConfigError("no generation models are configured")
        # The round-up is a safety margin, not the task's own requirement, so
        # the level Jev actually gave is the floor to fall back to.
        return self._pick(candidates, required, raw, answer.certainty, rounded_up,
                          floor=required - 1 if rounded_up else None)

    def select_for(self, required: int) -> Selection:
        """The cheapest model clearing an explicitly demanded rung."""
        candidates = self.available()
        if not candidates:
            raise ConfigError("no generation models are configured")
        required = min(max(int(required), MIN_MODEL_CAPABILITY), MAX_CAPABILITY)
        return self._pick(candidates, required, float(required), 1.0, False)

    def strongest(self) -> Optional[ModelSpec]:
        """The most capable model available, cheapest among equals."""
        candidates = self.available()
        return max(candidates, key=lambda m: (m.capability, -m.blended_price)) if candidates else None

    def _pick(self, candidates: List[ModelSpec], required: int, raw: float,
              certainty: float, rounded_up: bool, floor: Optional[int] = None) -> Selection:
        if self.quality == "best" and required > JEV_ONLY_LEVEL:
            # The user asked for the best answer rather than the cheapest one.
            # The requirement still stands; what changes is which side of it
            # the pick lands on.
            best = max(candidates, key=lambda m: (m.capability, -m.blended_price))
            return Selection(model=best, required=required, raw_required=raw, certainty=certainty,
                             rounded_up=rounded_up, considered=len(candidates),
                             best_available=best.capability,
                             candidates=candidate_rows(candidates, required, best))
        qualified = [m for m in candidates if m.capability >= required]
        best = max(m.capability for m in candidates)
        downgraded = relaxed = False
        if not qualified and floor is not None and floor < required:
            # Nothing clears the raised level. Give up the margin rather than
            # the run, and say so; Jev's own rung is still met.
            at_floor = [m for m in candidates if m.capability >= floor]
            if at_floor:
                qualified, required, relaxed = at_floor, floor, True
        if not qualified:
            if not self.allow_downgrade:
                # Nothing is strong enough. Say so; do not light up a weaker
                # model and call it the best choice.
                return Selection(model=None, required=required, raw_required=raw,
                                 certainty=certainty, rounded_up=rounded_up,
                                 considered=len(candidates), blocked=True,
                                 best_available=best,
                                 candidates=candidate_rows(candidates, required, None))
            qualified = [m for m in candidates if m.capability == best]
            downgraded = True
        chosen = min(qualified, key=_rank)
        rejected = [
            m.id
            for m in candidates
            if _rank(m) < _rank(chosen) and m.capability < required
        ]
        return Selection(
            model=chosen,
            required=required,
            raw_required=raw,
            certainty=certainty,
            rounded_up=rounded_up,
            considered=len(candidates),
            cheaper_rejected=rejected,
            downgraded=downgraded,
            relaxed=relaxed,
            best_available=best,
            candidates=candidate_rows(candidates, required, chosen),
        )

    # -- serialization ------------------------------------------------------ #

    def to_public_dict(self) -> dict:
        return {
            "models": [m.to_dict() for m in self.models],
            "credentials": [c.to_public_dict() for c in self.credentials.values()],
            "capability_levels": list(CAPABILITY_LEVELS),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Roster":
        raw = raw or {}
        if not isinstance(raw, Mapping):
            raise SchemaError("roster must be an object")
        creds: Dict[str, Credential] = {}
        for entry in raw.get("credentials") or []:
            if not isinstance(entry, Mapping):
                raise SchemaError("credential entry must be an object")
            vendor = vendors.get(str(entry.get("vendor") or vendors.DEFAULT).strip())
            cred = Credential(
                ref=str(entry.get("ref") or "default").strip(),
                api_key=str(entry.get("api_key") or "").strip(),
                base_url=vendors.normalise_base(vendor, str(entry.get("base_url") or "")),
                label=str(entry.get("label") or "").strip(),
                vendor=vendor.id,
            )
            # A blank row in the UI is not a credential. Dropping it here lets
            # the .env fallback apply instead of failing validation.
            if not cred.api_key:
                continue
            creds[cred.ref] = cred
        roster = cls(
            models=[ModelSpec.from_dict(m) for m in raw.get("models") or []],
            credentials=creds,
        )
        return roster


def _rank(spec: ModelSpec) -> Tuple[float, int, str]:
    """Cheapest first; break ties by lower capability then id for determinism."""
    return (spec.blended_price, spec.capability, spec.id)


def default_models(credential_ref: str = "default") -> List[ModelSpec]:
    """A starting roster of cheap OpenRouter models, spanning the ladder.

    Prices are USD per million tokens as listed by OpenRouter at the time of
    writing; the UI refreshes them from /api/v1/models when a key is present.
    """
    return [
        ModelSpec(
            id="flash-free",
            model="deepseek/deepseek-v4-flash-0731:free",
            capability=2,
            price_in=0.0,
            price_out=0.0,
            label="DeepSeek V4 Flash (free)",
            strengths="Free tier, long context, fine for routine drafting.",
            credential_ref=credential_ref,
            context_length=1048576,
        ),
        ModelSpec(
            id="qwen-flash",
            model="qwen/qwen3.7-flash",
            capability=3,
            price_in=0.03,
            price_out=0.13,
            label="Qwen3.7 Flash",
            strengths="Very cheap, handles multi-step reasoning and code.",
            credential_ref=credential_ref,
            context_length=1000000,
        ),
        ModelSpec(
            id="nemo",
            model="mistralai/mistral-nemo",
            capability=1,
            price_in=0.019,
            price_out=0.03,
            label="Mistral Nemo",
            strengths="Cheapest output tokens; mechanical reformatting only.",
            credential_ref=credential_ref,
            context_length=131072,
        ),
    ]
