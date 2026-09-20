"""OpenRouter transport. stdlib only.

Two endpoints are used, and the distinction is the whole point of this harness:

  POST /api/alpha/decisions       -> Jev, a System One model. Typed questions in,
                                     calibrated probabilities out. No text.
  POST /api/v1/chat/completions   -> a normal LLM. Prose and code out.

Jev is *not* available on chat/completions; OpenRouter rejects it explicitly.
"""
from __future__ import annotations

import json
import random
import re
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .errors import ProviderError, RateLimitError

USER_AGENT = "jev-harness/1.0 (+https://github.com/)"
# Costs worked out here, rather than reported by the vendor, come from the
# prices configured for each model; the version says which rule priced them.
PRICE_TABLE_VERSION = "configured-model-prices/v1"

PROVIDER_REPORTED = "provider_reported"
PRICE_TABLE_ESTIMATED = "price_table_estimated"
UNKNOWN_COST = "unknown"


def new_call_id() -> str:
    return "call-" + uuid.uuid4().hex[:12]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    # True when a vendor did not report usage and the numbers were worked out
    # here from the text. Shown as such; never passed off as a bill.
    estimated: bool = False
    # Where ``cost`` came from: the vendor's own figure, the configured price
    # table, or nowhere. An unknown cost is 0.0 here only so sums work; it is
    # never shown or averaged as zero.
    source: str = UNKNOWN_COST
    price_table: str = ""

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost += other.cost
        self.estimated = self.estimated or other.estimated

    @property
    def known(self) -> bool:
        return self.source != UNKNOWN_COST

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost": round(self.cost, 8),
            "estimated": self.estimated,
            "cost_source": self.source,
            "price_table": self.price_table,
        }

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "Usage":
        raw = raw or {}
        return cls(input_tokens=int(raw.get("input_tokens") or 0),
                   output_tokens=int(raw.get("output_tokens") or 0),
                   cost=float(raw.get("cost") or 0.0),
                   estimated=bool(raw.get("estimated")),
                   source=str(raw.get("cost_source") or UNKNOWN_COST),
                   price_table=str(raw.get("price_table") or ""))


_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def estimate_tokens(text: str) -> int:
    """A rough token count: ~4 characters each for Latin text, ~1.5 for CJK."""
    text = text or ""
    cjk = len(_CJK.findall(text))
    return int((len(text) - cjk) / 4 + cjk / 1.5) + 1


def price_usage(usage: Usage, price: Optional[Tuple[float, float]]) -> Usage:
    """Fill in the cost from list prices when the vendor did not send one."""
    if usage.source == PROVIDER_REPORTED:
        return usage
    if usage.cost > 0:
        # Only a vendor puts a figure here before pricing; never overwrite it.
        usage.source = PROVIDER_REPORTED
        return usage
    if not price:
        return usage
    price_in, price_out = price
    usage.cost = (usage.input_tokens * (price_in or 0) +
                  usage.output_tokens * (price_out or 0)) / 1_000_000
    usage.source = PRICE_TABLE_ESTIMATED
    usage.price_table = PRICE_TABLE_VERSION
    return usage


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked for, as it came off the wire."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    # The arguments exactly as the model wrote them, kept when they would not
    # parse: a malformed call is reported back to the model, never guessed at.
    raw: str = ""
    malformed: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments,
                "malformed": self.malformed}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ToolCall":
        return cls(id=str(raw.get("id") or ""), name=str(raw.get("name") or ""),
                   arguments=dict(raw.get("arguments") or {}),
                   malformed=bool(raw.get("malformed")))


def _accumulate(pending: Dict[int, dict], fragment: Mapping[str, Any]) -> None:
    """Fold one streamed tool-call fragment into the call it belongs to."""
    index = int(fragment.get("index") or 0)
    slot = pending.setdefault(index, {"id": "", "function": {"name": "", "arguments": ""}})
    if fragment.get("id"):
        slot["id"] = fragment["id"]
    function = fragment.get("function") or {}
    if function.get("name"):
        slot["function"]["name"] += function["name"]
    if function.get("arguments"):
        slot["function"]["arguments"] += function["arguments"]


def parse_tool_calls(raw: Any) -> List[ToolCall]:
    """Typed tool calls from whatever shape the vendor sent."""
    out: List[ToolCall] = []
    for index, entry in enumerate(raw or []):
        if not isinstance(entry, Mapping):
            continue
        function = entry.get("function") or {}
        name = str(function.get("name") or entry.get("name") or "").strip()
        if not name:
            continue
        text = function.get("arguments")
        if text is None:
            text = entry.get("arguments")
        arguments: Dict[str, Any] = {}
        malformed = False
        if isinstance(text, Mapping):
            arguments = dict(text)
        elif isinstance(text, str) and text.strip():
            try:
                loaded = json.loads(text)
                arguments = dict(loaded) if isinstance(loaded, Mapping) else {"value": loaded}
            except json.JSONDecodeError:
                malformed = True
        out.append(ToolCall(id=str(entry.get("id") or f"call_{index}"), name=name,
                            arguments=arguments, raw=str(text or ""), malformed=malformed))
    return out


@dataclass
class Call:
    """One upstream call, recorded for the trace."""

    engine: str            # "jev" | "llm"
    purpose: str           # "route" | "decide" | "generate" | "compile" | "guard"
    model: str
    latency_ms: int
    usage: Usage = field(default_factory=Usage)
    # One id per charged request, so a batch of eight questions is billed
    # once and a later correction lands on the same record.
    call_id: str = field(default_factory=new_call_id)
    # What the model asked to run, when it asked for anything.
    calls: List[ToolCall] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "engine": self.engine,
            "purpose": self.purpose,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "usage": self.usage.to_dict(),
        }


class Transport:
    """HTTP POST with retries. Injectable so tests never touch the network."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0,
                 max_retries: int = 3, sleep: Callable[[float], None] = time.sleep,
                 headers: Optional[Dict[str, str]] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        # How the key is presented differs by vendor; the caller says how.
        self._auth = headers or {"Authorization": f"Bearer {api_key}"}

    def _headers(self) -> dict:
        return {
            **self._auth,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-Title": "JEVia",
        }

    def post_json(self, path: str, payload: dict) -> dict:
        raw = self.post_raw(path, payload)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("upstream returned invalid JSON", detail=raw[:500]) from exc

    def post_raw(self, path: str, payload: dict) -> str:
        body = json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}{path}"
        last: Optional[Exception] = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, data=body, headers=self._headers(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", errors="replace")
                if exc.code in (429, 500, 502, 503, 504, 529) and attempt < self.max_retries - 1:
                    last = exc
                    self._sleep(min(8.0, (2 ** attempt) + random.random()))
                    continue
                if exc.code == 429:
                    raise RateLimitError("upstream rate limited", detail=_trim(text)) from exc
                raise ProviderError(
                    f"upstream HTTP {exc.code}", detail=_trim(text)
                ) from exc
            except urllib.error.URLError as exc:
                last = exc
                if attempt < self.max_retries - 1:
                    self._sleep(min(8.0, (2 ** attempt) + random.random()))
                    continue
                raise ProviderError(f"network error: {exc.reason}") from exc
        raise ProviderError(f"request failed after {self.max_retries} attempts: {last}")

    def post_stream(self, path: str, payload: dict) -> Iterator[str]:
        """Yield raw SSE lines. Not retried: a partial stream cannot be replayed."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for line in resp:
                    yield line.decode("utf-8", errors="replace").rstrip("\n")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"upstream HTTP {exc.code}", detail=_trim(text)) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"network error: {exc.reason}") from exc


def _trim(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _message_text(message: dict) -> str:
    """The assistant's text, tolerating reasoning-only replies.

    Some cheap models are reasoning models: they put everything in
    ``reasoning`` and leave ``content`` null, especially when a token budget
    runs out mid-thought. Falling back keeps a usable answer instead of an
    empty string, and the harness disables thinking by default anyway.
    """
    content = (message.get("content") or "").strip()
    if content:
        return content
    return (message.get("reasoning") or "").strip()


def _usage_from(raw: dict) -> Usage:
    u = raw.get("usage") or {}
    reported = u.get("cost") is not None
    try:
        cost = float(u.get("cost") or 0.0)
    except (TypeError, ValueError):
        cost, reported = 0.0, False
    return Usage(
        input_tokens=int(u.get("input_tokens") or u.get("prompt_tokens") or 0),
        output_tokens=int(u.get("output_tokens") or u.get("completion_tokens") or 0),
        cost=cost,
        source=PROVIDER_REPORTED if reported else UNKNOWN_COST,
    )


class JevClient:
    """System One. Typed questions -> calibrated answers."""

    PATH = "/alpha/decisions"

    def __init__(self, transport: Transport, model: str, path: Optional[str] = None) -> None:
        self.transport = transport
        self.model = model
        self.path = path or self.PATH

    def decide(self, state: Any, questions: dict, *, purpose: str = "decide",
               observer: Any = None, targets: Optional[Dict[str, str]] = None
               ) -> tuple[dict, Call]:
        """One batch. ``observer`` belongs to the caller's run, never to this client.

        It hears ``started`` before the request goes out and exactly one of
        ``completed`` or ``failed`` after, so a waiting state always ends.
        """
        batch = observer.started(purpose, questions, targets) if observer is not None else None
        try:
            payload = {"model": self.model, "state": state, "questions": questions}
            started = time.perf_counter()
            raw = self.transport.post_json(self.path, payload)
            latency = int((time.perf_counter() - started) * 1000)
            if "error" in raw:
                err = raw["error"]
                raise ProviderError(
                    str(err.get("message", "jev call failed")), detail=err
                )
            answers = raw.get("answers")
            if not isinstance(answers, dict):
                raise ProviderError("jev response missing 'answers'", detail=_trim(json.dumps(raw)))
        except BaseException as exc:
            if observer is not None:
                observer.failed(batch, questions, exc, purpose)
            raise
        call = Call(
            engine="jev",
            purpose=purpose,
            model=raw.get("model") or self.model,
            latency_ms=latency,
            usage=_usage_from(raw),
        )
        if observer is not None:
            observer.completed(batch, questions, answers, call, purpose)
        return answers, call


class LLMClient:
    """System Two. Prose and code, from any OpenAI-compatible vendor.

    ``dialect`` decides which non-standard fields may be sent. OpenRouter takes
    a reasoning switch and returns cost; a plain OpenAI-compatible endpoint may
    reject both, so neither is sent there. ``price`` fills in the cost when the
    vendor reports tokens but not money, or reports nothing at all.
    """

    PATH = "/chat/completions"

    def __init__(self, transport: Transport, model: str, *, dialect: str = "openrouter",
                 stream_usage: bool = True,
                 price: Optional[Tuple[float, float]] = None) -> None:
        self.transport = transport
        self.model = model
        self.dialect = dialect
        self.stream_usage = stream_usage
        self.price = price

    @staticmethod
    def _tooling(tools: Optional[Sequence[dict]]) -> dict:
        """The tool half of a request, in the shape every OpenAI-compatible API takes."""
        if not tools:
            return {}
        return {"tools": [{"type": "function", "function": t} for t in tools],
                "tool_choice": "auto"}

    def _extras(self, thinking: bool, stream: bool) -> dict:
        extras: dict = {}
        if self.dialect == "openrouter":
            # Thinking is off unless a model is configured to want it. On
            # reasoning models it otherwise burns the entire token budget
            # before a single visible character: measured on qwen3.7-flash,
            # the same one-word answer cost 9x more with it on.
            extras["reasoning"] = {"enabled": bool(thinking)}
            if stream:
                extras["usage"] = {"include": True}
        elif stream and self.stream_usage:
            extras["stream_options"] = {"include_usage": True}
        return extras

    def _settle(self, usage: Usage, messages: list, text: str) -> Usage:
        if not usage.input_tokens and not usage.output_tokens:
            prompt = "".join(str(m.get("content") or "") for m in messages)
            reported = usage.source == PROVIDER_REPORTED and usage.cost > 0
            usage = Usage(estimate_tokens(prompt), estimate_tokens(text),
                          usage.cost if reported else 0.0, True,
                          source=PROVIDER_REPORTED if reported else UNKNOWN_COST)
        return price_usage(usage, self.price)

    def complete(self, messages: list, *, purpose: str = "generate",
                 model: str | None = None, temperature: float = 0.2,
                 max_tokens: int | None = None,
                 thinking: bool = False, observer: Any = None,
                 tools: Optional[Sequence[dict]] = None) -> tuple[str, Call]:
        """One reply. ``observer`` (per run) hears started, then completed or failed."""
        call_id = new_call_id()
        if observer is not None:
            observer.started(purpose, model or self.model)
        try:
            text, call = self._complete(messages, purpose, model, temperature, max_tokens,
                                        thinking, tools)
        except BaseException as exc:
            if observer is not None:
                observer.failed(purpose, model or self.model, exc, 0, call_id)
            raise
        call.call_id = call_id
        if observer is not None:
            observer.completed(purpose, call, len(text))
        return text, call

    def _complete(self, messages: list, purpose: str, model: Optional[str], temperature: float,
                  max_tokens: Optional[int], thinking: bool,
                  tools: Optional[Sequence[dict]] = None) -> tuple[str, Call]:
        payload: dict = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            **self._extras(thinking, stream=False),
            **self._tooling(tools),
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        started = time.perf_counter()
        raw = self.transport.post_json(self.PATH, payload)
        latency = int((time.perf_counter() - started) * 1000)
        if "error" in raw:
            err = raw["error"]
            raise ProviderError(str(err.get("message", "llm call failed")), detail=err)
        try:
            message = raw["choices"][0]["message"]
            text = _message_text(message)
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                "llm response has no choices[0].message",
                detail=_trim(json.dumps(raw)),
            ) from exc
        call = Call(
            engine="llm",
            purpose=purpose,
            model=raw.get("model") or payload["model"],
            latency_ms=latency,
            usage=self._settle(_usage_from(raw), messages, text),
            calls=parse_tool_calls(message.get("tool_calls")),
        )
        return text, call

    def stream(self, messages: list, *, model: str | None = None,
               temperature: float = 0.2, max_tokens: int | None = None,
               thinking: bool = False, observer: Any = None,
               purpose: str = "generate",
               tools: Optional[Sequence[dict]] = None) -> Iterator[dict]:
        """Yield {'delta': str} chunks, then one {'done': True, 'usage': ..., 'model': ...}.

        If a model streams nothing but reasoning, that reasoning is emitted at
        the end rather than handing the caller an empty answer. ``observer``
        hears started before the request and one final state after it.
        """
        call_id = new_call_id()
        if observer is not None:
            observer.started(purpose, model or self.model)
        streamed = 0
        started = time.perf_counter()
        try:
            for event in self._stream(messages, model, temperature, max_tokens, thinking, tools):
                if "delta" in event:
                    streamed += len(event["delta"])
                if event.get("done"):
                    event["call_id"] = call_id
                    if observer is not None:
                        call = Call(engine="llm", purpose=purpose, model=event.get("model") or "",
                                    latency_ms=int((time.perf_counter() - started) * 1000),
                                    usage=Usage.from_dict(event.get("usage")), call_id=call_id)
                        observer.completed(purpose, call, streamed)
                yield event
        except GeneratorExit:
            raise
        except BaseException as exc:
            if observer is not None:
                observer.failed(purpose, model or self.model, exc, streamed, call_id)
            raise

    def _stream(self, messages: list, model: Optional[str], temperature: float,
                max_tokens: Optional[int], thinking: bool,
                tools: Optional[Sequence[dict]] = None) -> Iterator[dict]:
        payload: dict = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            **self._extras(thinking, stream=True),
            **self._tooling(tools),
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        usage = Usage()
        model_used = payload["model"]
        saw_content = False
        reasoning_parts: list = []
        written: list = []
        # Tool calls arrive in fragments, indexed, across many chunks.
        pending: Dict[int, dict] = {}
        for line in self.transport.post_stream(self.PATH, payload):
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("model"):
                model_used = chunk["model"]
            if chunk.get("usage"):
                usage = _usage_from(chunk)
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                for fragment in delta.get("tool_calls") or []:
                    _accumulate(pending, fragment)
                text = delta.get("content")
                if text:
                    saw_content = True
                    written.append(text)
                    yield {"delta": text}
                elif delta.get("reasoning"):
                    reasoning_parts.append(delta["reasoning"])
        if not saw_content and reasoning_parts:
            fallback = "".join(reasoning_parts).strip()
            written.append(fallback)
            yield {"delta": fallback}
        usage = self._settle(usage, messages, "".join(written))
        yield {"done": True, "usage": usage.to_dict(), "model": model_used,
               "tool_calls": [c.to_dict() for c in parse_tool_calls(
                   [pending[i] for i in sorted(pending)])]}
