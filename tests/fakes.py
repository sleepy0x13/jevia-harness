"""Stand-ins for the two engines, so the whole harness runs offline."""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional

from jevharness.providers import Transport


class FakeTransport(Transport):
    """Answers /alpha/decisions and /v1/chat/completions from canned data.

    Records every request so tests can assert on *how many* calls were made,
    which is the property the harness exists to minimise.
    """

    def __init__(
        self,
        *,
        decisions: Optional[Dict[str, Any]] = None,
        completions: Optional[List[str]] = None,
        decision_hook=None,
    ) -> None:
        super().__init__("https://fake.test/api", "sk-fake-000000000000")
        self.decisions = decisions or {}
        self.completions = list(completions or [])
        self.decision_hook = decision_hook
        self.requests: List[tuple] = []
        self.term_calls: List[dict] = []

    # -- introspection ------------------------------------------------------ #

    @property
    def decision_calls(self) -> List[dict]:
        return [body for path, body in self.requests if path.endswith("/alpha/decisions")]

    @property
    def chat_calls(self) -> List[dict]:
        return [body for path, body in self.requests
                if path.endswith("/chat/completions") and not _asks_for_search_terms(body)]

    # -- Transport surface -------------------------------------------------- #

    def post_json(self, path: str, payload: dict) -> dict:
        self.requests.append((path, payload))
        if path.endswith("/alpha/decisions"):
            answers = (
                self.decision_hook(payload) if self.decision_hook else dict(self.decisions)
            )
            asked = set(payload.get("questions") or {})
            return {
                "model": "fake-jev",
                "answers": {k: v for k, v in answers.items() if k in asked},
                "usage": {"input_tokens": 100, "output_tokens": 20, "cost": 0.00002},
            }
        if _asks_for_search_terms(payload):
            # Search terms are their own small job; answering them from the
            # queue would hand a step's text to the query writer.
            self.term_calls.append(payload)
            return {"model": payload.get("model", "fake-llm"),
                    "choices": [{"message": {"role": "assistant", "content": '["fake query"]'}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 8, "cost": 0.00001}}
        text = self.completions.pop(0) if self.completions else "fake output"
        return {
            "model": payload.get("model", "fake-llm"),
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 60, "cost": 0.0004},
        }

    def post_stream(self, path: str, payload: dict) -> Iterator[str]:
        self.requests.append((path, payload))
        text = self.completions.pop(0) if self.completions else "fake output"
        for piece in _chunks(text):
            yield "data: " + json.dumps({"choices": [{"delta": {"content": piece}}]})
        yield "data: " + json.dumps(
            {
                "model": payload.get("model", "fake-llm"),
                "choices": [],
                "usage": {"input_tokens": 200, "output_tokens": 60, "cost": 0.0004},
            }
        )
        yield "data: [DONE]"


def _asks_for_search_terms(payload: dict) -> bool:
    messages = payload.get("messages") or [{}]
    return str(messages[0].get("content") or "").startswith("You write web search queries")


def _chunks(text: str, size: int = 12):
    for i in range(0, len(text), size):
        yield text[i : i + size]


def choice(value: str, probabilities: dict, confidence: float) -> dict:
    return {
        "type": "choice",
        "choice": value,
        "probabilities": probabilities,
        "confidence": confidence,
    }


def score(value: float, levels: int, confidence: float) -> dict:
    return {
        "type": "score",
        "score": value,
        "legend": {str(i): f"level {i}" for i in range(levels)},
        "probabilities": {str(i): (1.0 if i == round(value) else 0.0) for i in range(levels)},
        "confidence": confidence,
    }


def noul(probability: float) -> dict:
    return {"type": "noul", "noul": probability}
