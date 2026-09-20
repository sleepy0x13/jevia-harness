"""Scrubbing secrets out of anything that is logged, streamed or saved.

Every new log, event record and export goes through here. Keys arrive in
request bodies and live only for the request; a provider's error body can echo
a header back, and a model can repeat a key it was shown. None of it may reach
disk or the browser in a form that still works.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

MASK = "[redacted]"

_PATTERNS = (
    # Provider keys: OpenRouter sk-or-v1-…, OpenAI sk-…/sk-proj-…, Anthropic sk-ant-…
    re.compile(r"\bsk-(?:or-v1-|ant-|proj-)?[A-Za-z0-9_\-]{12,}"),
    # Google-style keys and generic long bearer tokens.
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{8,}"),
    # key: value pairs whose name says what they are.
    re.compile(r"(?i)((?:api[_-]?key|x-api-key|authorization|cookie|set-cookie|"
               r"access[_-]?token|secret|password)\"?\s*[:=]\s*\"?)[^\s\"',;}]{4,}"),
)

# Field names whose values are never passed on, whatever they contain.
SECRET_FIELDS = frozenset({
    "api_key", "apikey", "authorization", "cookie", "cookies", "set-cookie",
    "headers", "x-api-key", "password", "secret", "token", "access_token",
})


def redact(text: Any, secrets: Iterable[str] = ()) -> str:
    """``text`` with every known secret and anything shaped like one masked."""
    out = str(text if text is not None else "")
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, MASK)
    for pattern in _PATTERNS:
        if pattern.groups:
            out = pattern.sub(lambda m: m.group(1) + MASK, out)
        else:
            out = pattern.sub(MASK, out)
    return out


def scrub(value: Any, secrets: Iterable[str] = (), *, depth: int = 0) -> Any:
    """A JSON-like value with secret fields dropped and strings redacted."""
    secrets = tuple(secrets)
    if depth > 8:
        return None
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, Mapping):
        return {str(k): scrub(v, secrets, depth=depth + 1) for k, v in value.items()
                if str(k).lower() not in SECRET_FIELDS}
    if isinstance(value, (list, tuple)):
        return [scrub(v, secrets, depth=depth + 1) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(value, secrets)
