"""Configuration loading. No third-party dependencies."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Defaults chosen for cost: Jev decisions run ~$0.00002 per call, and the
# fallback LLM is one of the cheapest competent models on OpenRouter.
DEFAULT_JEV_MODEL = "~typesafe/jev-latest"
DEFAULT_LLM_MODEL = "qwen/qwen3.7-flash"
DEFAULT_COMPILER_MODEL = "qwen/qwen3.7-flash"


def load_dotenv(path=None) -> None:
    """Populate os.environ from a .env file. Existing env vars win."""
    path = path or PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Thresholds:
    """Every cutoff the harness applies, in one auditable place.

    The asymmetry is deliberate. Jev cannot emit prose, so wrongly skipping the
    LLM is an unrecoverable failure, while wrongly calling it merely costs
    money. Uncertainty therefore always resolves toward spending.
    """

    # A choice or score answer this unpeaked is not trusted on its own.
    decision_abstain_below: float = 0.55
    # A noul inside this band is a coin flip dressed as an answer.
    noul_uncertain_low: float = 0.35
    noul_uncertain_high: float = 0.65
    # Below this certainty on the capability rung, ask for one rung more model.
    capability_round_up_below: float = 0.50

    def validate(self) -> None:
        if not 0 <= self.noul_uncertain_low < self.noul_uncertain_high <= 1:
            raise ValueError(
                "noul_uncertain_low must be < noul_uncertain_high, both in [0,1]"
            )
        for name in ("decision_abstain_below", "capability_round_up_below"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0,1]")


@dataclass
class Config:
    api_key: str = ""
    jev_model: str = DEFAULT_JEV_MODEL
    llm_model: str = DEFAULT_LLM_MODEL
    compiler_model: str = DEFAULT_COMPILER_MODEL
    base_url: str = "https://openrouter.ai/api/v1"
    host: str = "127.0.0.1"
    port: int = 8765
    request_timeout: float = 60.0
    max_retries: int = 3
    thresholds: Thresholds = field(default_factory=Thresholds)
    # Tier 5: screen LLM output with Jev. Off by default (extra call).
    guard_llm_output: bool = False

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def redacted_key(self) -> str:
        if not self.api_key:
            return ""
        if len(self.api_key) <= 12:
            return "***"
        return f"{self.api_key[:11]}...{self.api_key[-4:]}"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def load_config() -> Config:
    load_dotenv()
    cfg = Config(
        api_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
        jev_model=os.environ.get("JEV_MODEL", "").strip() or DEFAULT_JEV_MODEL,
        llm_model=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_LLM_MODEL,
        compiler_model=(
            os.environ.get("COMPILER_MODEL", "").strip() or DEFAULT_COMPILER_MODEL
        ),
        host=os.environ.get("HOST", "").strip() or "127.0.0.1",
        port=_env_int("PORT", 8765),
        guard_llm_output=_env_bool("GUARD_LLM_OUTPUT", False),
    )
    cfg.thresholds.validate()
    return cfg
