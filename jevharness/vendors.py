"""Who the harness can talk to, and how.

Every chat vendor here speaks the OpenAI chat-completions dialect at some root
URL, which is the one thing that makes supporting many of them tractable. What
differs is small and listed per vendor: where the root is, how the key is sent,
whether usage comes back on a stream, and which prefix its models carry in
OpenRouter's public catalogue — used to price them, because most vendors' own
model lists say nothing about price.

Jev is not a chat model. It is reached through OpenRouter, or directly through
TypeSafe, and only those two vendors have a ``decisions`` endpoint.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class Vendor:
    id: str
    name: str
    base_url: str                  # OpenAI-compatible root; "" when chat is not offered
    keys_url: str = ""
    key_hint: str = "sk-"
    catalogue_prefix: str = ""     # OpenRouter id prefix, for pricing
    dialect: str = "openai"        # "openai" | "openrouter"
    stream_usage: bool = False     # accepts stream_options.include_usage
    auth: str = "bearer"           # "bearer" | "anthropic"
    decisions_root: str = ""       # where Jev lives, if it lives here
    decisions_path: str = ""
    jev_model: str = ""

    @property
    def chats(self) -> bool:
        return bool(self.base_url)

    @property
    def serves_jev(self) -> bool:
        return bool(self.decisions_root)

    def headers(self, api_key: str) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {api_key}"}
        if self.auth == "anthropic":
            # The compatibility layer takes a bearer token; the native model
            # listing wants these instead. Sending both works for either.
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        return headers

    def to_public(self) -> dict:
        row = asdict(self)
        row["chats"] = self.chats
        row["serves_jev"] = self.serves_jev
        return row


VENDORS: Tuple[Vendor, ...] = (
    Vendor("openrouter", "OpenRouter", "https://openrouter.ai/api/v1",
           keys_url="https://openrouter.ai/keys", key_hint="sk-or-v1-",
           dialect="openrouter", stream_usage=True,
           decisions_root="https://openrouter.ai/api", decisions_path="/alpha/decisions",
           jev_model="~typesafe/jev-latest"),
    Vendor("openai", "OpenAI", "https://api.openai.com/v1",
           keys_url="https://platform.openai.com/api-keys", key_hint="sk-",
           catalogue_prefix="openai", stream_usage=True),
    Vendor("anthropic", "Anthropic (Claude)", "https://api.anthropic.com/v1",
           keys_url="https://console.anthropic.com/settings/keys", key_hint="sk-ant-",
           catalogue_prefix="anthropic", auth="anthropic"),
    Vendor("moonshot", "Kimi (Moonshot)", "https://api.moonshot.cn/v1",
           keys_url="https://platform.moonshot.cn/console/api-keys",
           catalogue_prefix="moonshotai"),
    Vendor("deepseek", "DeepSeek", "https://api.deepseek.com/v1",
           keys_url="https://platform.deepseek.com/api_keys",
           catalogue_prefix="deepseek", stream_usage=True),
    Vendor("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai",
           keys_url="https://aistudio.google.com/app/apikey", key_hint="AI",
           catalogue_prefix="google"),
    Vendor("zhipu", "GLM (Zhipu)", "https://open.bigmodel.cn/api/paas/v4",
           keys_url="https://open.bigmodel.cn/usercenter/apikeys", key_hint="",
           catalogue_prefix="z-ai"),
    Vendor("minimax", "MiniMax", "https://api.minimaxi.com/v1",
           keys_url="https://platform.minimaxi.com/user-center/basic-information/interface-key",
           key_hint="", catalogue_prefix="minimax"),
    Vendor("qwen", "Qwen (DashScope)", "https://dashscope.aliyuncs.com/compatible-mode/v1",
           keys_url="https://dashscope.console.aliyun.com/apiKey",
           catalogue_prefix="qwen"),
    Vendor("typesafe", "TypeSafe (Jev only)", "",
           keys_url="https://typesafe.ai", key_hint="",
           decisions_root="https://api.typesafe.ai", decisions_path="/v1/systemone",
           jev_model="jev-latest"),
    Vendor("custom", "Custom (OpenAI-compatible)", "",
           key_hint=""),
)

BY_ID: Dict[str, Vendor] = {v.id: v for v in VENDORS}
DEFAULT = "openrouter"


def get(vendor_id: Optional[str]) -> Vendor:
    return BY_ID.get(vendor_id or "", BY_ID["custom"])


def can_chat(credential) -> bool:
    """Whether a key can run chat models.

    Decided by the credential, not the preset: a custom vendor has no root of
    its own, so it chats exactly when the user gave it one.
    """
    vendor = get(getattr(credential, "vendor", ""))
    return bool(getattr(credential, "base_url", "")) and vendor.id != "typesafe"


def normalise_base(vendor: Vendor, base_url: str) -> str:
    """The root the chat path hangs off, tolerating the shapes people paste.

    Earlier builds stored OpenRouter as ".../api" and appended "/v1/…"; a user
    pasting from a vendor's docs often includes "/chat/completions". Both are
    turned into the bare root here, once, so nothing downstream has to guess.
    """
    base = (base_url or vendor.base_url or "").strip().rstrip("/")
    base = re.sub(r"/chat/completions$", "", base)
    if vendor.id == "openrouter" and base.endswith("/api"):
        base += "/v1"
    return base


# --------------------------------------------------------------------------- #
# Pricing, borrowed from OpenRouter's public list
# --------------------------------------------------------------------------- #

_CATALOGUE: Optional[List[dict]] = None


def catalogue(refresh: bool = False, timeout: float = 20.0) -> List[dict]:
    """OpenRouter's model list. Public — no key needed — and cached per process."""
    global _CATALOGUE
    if _CATALOGUE is not None and not refresh:
        return _CATALOGUE
    try:
        request = urllib.request.Request(OPENROUTER_MODELS, headers={"User-Agent": "JEVia/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            rows = json.loads(response.read().decode("utf-8")).get("data") or []
    except Exception:  # noqa: BLE001 - pricing is best effort
        return _CATALOGUE or []
    out = []
    for row in rows:
        pricing = row.get("pricing") or {}
        try:
            price_in = float(pricing.get("prompt") or 0) * 1_000_000
            price_out = float(pricing.get("completion") or 0) * 1_000_000
        except (TypeError, ValueError):
            continue
        if price_in < 0 or price_out < 0:
            continue   # routers priced at -1: not concrete models
        if str(row.get("id") or "").endswith(":batch"):
            continue   # batch-only: the chat endpoint refuses them outright
        label = row.get("name") or row.get("id") or ""
        if ": " in label:
            head, _, tail = label.partition(": ")
            if tail.lower().startswith(head.lower()):
                label = tail
        out.append({"model": row.get("id"), "label": label,
                    "price_in": round(price_in, 4), "price_out": round(price_out, 4),
                    "context_length": row.get("context_length")})
    out.sort(key=lambda m: (m["price_in"] + 3 * m["price_out"], m["model"]))
    _CATALOGUE = out
    return out


def _key(model_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", model_id.lower())


def _undated(model_id: str) -> str:
    return re.sub(r"[-_@]?(20\d{6}|\d{4}-\d{2}-\d{2}|latest|preview)$", "", model_id.lower())


def price_for(vendor: Vendor, model_id: str, rows: Optional[List[dict]] = None) -> Optional[dict]:
    """Find a direct model's list price in the OpenRouter catalogue, if it is there."""
    if vendor.dialect == "openrouter":
        rows = rows if rows is not None else catalogue()
        return next((r for r in rows if r["model"] == model_id), None)
    if not vendor.catalogue_prefix:
        return None
    rows = rows if rows is not None else catalogue()
    prefix = vendor.catalogue_prefix + "/"
    candidates = [r for r in rows if r["model"].startswith(prefix) and ":" not in r["model"]]
    wanted = _key(model_id.split("/")[-1])
    for row in candidates:
        if _key(row["model"][len(prefix):]) == wanted:
            return row
    loose = _key(_undated(model_id.split("/")[-1]))
    for row in candidates:
        if _key(_undated(row["model"][len(prefix):])) == loose:
            return row
    return None


def list_models(vendor: Vendor, base_url: str, api_key: str, timeout: float = 20.0
                ) -> Tuple[List[dict], str]:
    """The models this key can use, priced where the catalogue knows them.

    Returns (models, source). The source is "vendor" when the list came from the
    vendor itself, or "catalogue" when the vendor would not say and the ids are
    OpenRouter's — close, but not guaranteed to match the vendor's own spelling.
    """
    rows = catalogue()
    if vendor.dialect == "openrouter":
        return [dict(r, priced=True) for r in rows], "vendor"

    base = normalise_base(vendor, base_url)
    ids: List[str] = []
    if base:
        request = urllib.request.Request(
            f"{base}/models", headers={**vendor.headers(api_key), "User-Agent": "JEVia/1.0"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8")).get("data") or []
            ids = [str(d.get("id") or "").split("models/")[-1] for d in data if d.get("id")]
        except Exception:  # noqa: BLE001 - fall back to the catalogue below
            ids = []

    source = "vendor"
    if not ids and vendor.catalogue_prefix:
        prefix = vendor.catalogue_prefix + "/"
        ids = [r["model"][len(prefix):] for r in rows
               if r["model"].startswith(prefix) and ":" not in r["model"]]
        source = "catalogue"

    out = []
    for model_id in dict.fromkeys(ids):
        hit = price_for(vendor, model_id, rows)
        out.append({
            "model": model_id,
            "label": (hit or {}).get("label") or model_id,
            "price_in": (hit or {}).get("price_in"),
            "price_out": (hit or {}).get("price_out"),
            "context_length": (hit or {}).get("context_length"),
            "priced": hit is not None,
        })
    # Priced models first, cheapest first; unpriced ones after, by name.
    out.sort(key=lambda m: (not m["priced"],
                            (m["price_in"] or 0) + 3 * (m["price_out"] or 0),
                            m["model"]))
    return out, source
