"""The real server and interface, with every outside call replaced by a fake.

For working on the interface without spending anything: Jev answers after a
deliberate pause (so waiting states can be seen), models stream canned text,
web search and page fetches return local stand-ins. Nothing leaves the
machine — no key is read from .env and none typed into the page is used.

    python3 scripts/offline_server.py            # http://127.0.0.1:8766
    python3 scripts/offline_server.py --jev-delay 2.5

Scripted answers are not Jev's. The console says OFFLINE on start.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jevharness import agent, harness, server, vendors  # noqa: E402
from jevharness.config import Config  # noqa: E402
from jevharness.providers import Transport  # noqa: E402
from jevharness.research import Hit, Page, Section  # noqa: E402

JEV_DELAY = 1.2
ASK_FIRST = False
TEXT = ("This is an offline stand-in answer. The run went through the real planner, router and "
        "policies; only the engines were replaced, so every number you see was scripted.")


class OfflineTransport(Transport):
    def post_json(self, path, payload):
        if path.endswith("/decisions"):
            time.sleep(JEV_DELAY)
            return {"model": "offline/jev", "answers": {k: _answer(k, q) for k, q in payload["questions"].items()},
                    "usage": {"input_tokens": 400, "output_tokens": 0, "cost": 0.0000168}}
        messages = payload.get("messages") or [{}]
        if str(messages[0].get("content") or "").startswith("You write web search queries"):
            return _chat(json.dumps([f"offline query {random.randint(1, 999)}"]))
        if "planner for a two-engine harness" in str(messages[0].get("content") or ""):
            time.sleep(0.8)
            return _chat(json.dumps({
                "strategy": "three parts", "answer_from": "generation",
                "steps": [{"title": "Gather the facts", "role": "researcher"},
                          {"title": "Weigh the options", "role": "analyst"},
                          {"title": "Write the recommendation", "role": "writer"}],
                "decisions": {"urgent": {"type": "noul", "instructions": "Is this urgent?"}},
                "generation": {"instruction": "Write it up."}}))
        if str(messages[0].get("content") or "").startswith("You fill in the words"):
            time.sleep(0.5)
            listing = str(messages[-1].get("content") or "")
            ids = [line.split(":")[0].split(". ")[-1].strip()
                   for line in listing.splitlines() if line[:1].isdigit() and ":" in line]
            return _chat(json.dumps({"title": "Offline demo screen",
                                     "components": [{"type": i, "props": _props(i)} for i in ids]}))
        return _chat(TEXT)

    def post_stream(self, path, payload):
        system = str((payload.get("messages") or [{}])[0].get("content") or "")
        if system.startswith("You build one complete, working web app"):
            yield from _stream_text(PAGE)
            return
        # With tools on the table, the stand-in uses one before it answers, so
        # the loop, the gate and the step view can all be seen working.
        names = [t["function"]["name"] for t in payload.get("tools") or []]
        used = [m for m in payload.get("messages") or [] if m.get("role") == "tool"]
        asked = bool(used)
        # A second tool once the first result is in, so the step view has
        # something to show and the gate is exercised on a real write.
        if asked and len(used) == 1 and "write_file" in names and not ASK_FIRST:
            time.sleep(0.2)
            yield "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "offline-2", "function": {"name": "write_file", "arguments": json.dumps(
                    {"path": "notes-from-the-run.md", "content": "Written by the offline stand-in."})}}]}}]})
            yield "data: " + json.dumps({"choices": [], "usage": {"input_tokens": 220, "output_tokens": 30}})
            yield "data: [DONE]"
            return
        if names and not asked:
            # Ask first when the stand-in can: it exercises the question card.
            if ASK_FIRST and "ask_user_question" in names:
                call = ("ask_user_question", {"question": "Which folder should I look at?",
                                              "options": ["This workspace", "Somewhere else"]})
            elif "list_dir" in names:
                call = ("list_dir", {"path": "."})
            else:
                call = (names[0], {})
            time.sleep(0.3)
            yield "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "offline-1",
                 "function": {"name": call[0], "arguments": json.dumps(call[1])}}]}}]})
            yield "data: " + json.dumps({"choices": [], "usage": {"input_tokens": 200, "output_tokens": 20}})
            yield "data: [DONE]"
            return
        time.sleep(0.4)
        for word in TEXT.split(" "):
            time.sleep(0.05)
            yield "data: " + json.dumps({"choices": [{"delta": {"content": word + " "}}]})
        yield "data: " + json.dumps({"choices": [], "usage": {"input_tokens": 300, "output_tokens": 60}})
        yield "data: [DONE]"


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Offline counter</title>
<style>body{font-family:system-ui;margin:0;display:grid;place-items:center;height:100vh;background:#fcfdfd;color:#19232d}
button{font:inherit;padding:10px 18px;border:1px solid #3158ef;background:#fff;color:#3158ef;border-radius:3px}
h1{font-size:64px;margin:0 0 18px}</style></head><body>
<div><h1 id="n">0</h1><button id="up">Count</button></div>
<script>let n=0;document.getElementById('up').onclick=()=>{document.getElementById('n').textContent=++n};</script>
</body></html>"""


def _stream_text(text):
    for i in range(0, len(text), 40):
        time.sleep(0.02)
        yield "data: " + json.dumps({"choices": [{"delta": {"content": text[i:i + 40]}}]})
    yield "data: " + json.dumps({"choices": [], "usage": {"input_tokens": 300, "output_tokens": 900}})
    yield "data: [DONE]"


def _props(component):
    return {
        "hero": {"title": "Offline demo", "subtitle": "Every engine is a local fake"},
        "stats": {"items": [{"label": "Runs", "value": "3"}, {"label": "Saved", "value": "0"}]},
        "table": {"columns": ["Item", "State"], "rows": [["First", "ok"], ["Second", "ok"]]},
        "list": {"items": ["An offline row", "Another offline row"]},
        "text": {"body": "This screen was filled by a local stand-in, not a model."},
        "callout": {"title": "Offline", "body": "Nothing here came from a provider."},
        "checklist": {"items": ["Check one", "Check two"]},
        "form": {"fields": [{"label": "Name", "type": "text"}], "submit": "Save"},
        "cards": {"items": [{"title": "Card", "body": "Offline card"}]},
        "tabs": {"tabs": [{"label": "One", "body": "First"}, {"label": "Two", "body": "Second"}]},
        "timeline": {"items": [{"when": "Today", "what": "Built offline"}]},
        "steps": {"items": ["Plan", "Build", "Check"]},
        "filter": {"placeholder": "Filter"},
        "chart": {"items": [{"label": "A", "value": 3}, {"label": "B", "value": 5}]},
    }.get(component, {"body": "Offline"})


def _chat(text):
    return {"model": "offline/llm", "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 60}}


def _answer(name, q):
    kind = q.get("type")
    if kind == "noul":
        p = {"__needs_research__": 0.82, "__multi_step__": 0.74, "__wants_app__": 0.06,
             "__enough__": 0.78, "__independent__": 0.8, "__assembly__": 0.3}.get(name)
        if p is None and str(q.get("instructions") or "").startswith("Is action"):
            p = 0.93            # the gate: allow, so the happy path can be seen
        if p is None:
            p = round(random.uniform(0.05, 0.95), 2)
        return {"type": "noul", "noul": p}
    if kind == "score":
        n = len(q.get("criteria") or [0, 1])
        v = round(random.uniform(1.0, min(3.2, n - 1)), 2)
        return {"type": "score", "score": v, "legend": {str(i): str(c) for i, c in enumerate(q.get("criteria") or [])},
                "probabilities": {str(i): (0.7 if i == round(v) else 0.3 / max(n - 1, 1)) for i in range(n)},
                "confidence": round(random.uniform(0.4, 0.9), 2)}
    options = list((q.get("criteria") or {"a": ""}).keys())
    pick = options[0]
    rest = 0.3 / max(len(options) - 1, 1)
    return {"type": "choice", "choice": pick, "confidence": 0.7,
            "probabilities": {o: (0.7 if o == pick else rest) for o in options}}


def _search(term, limit=8):
    return [Hit(title=f"Offline source {i} for {term}", url=f"https://offline.invalid/{abs(hash(term)) % 997}/{i}",
                snippet="A local stand-in result.", rank=i) for i in range(5)]


def _fetch(urls, workers=4):
    return [Page(url=u, title=u, text="", sections=[
        Section(text=f"Offline paragraph {k} from {u}. " * 12, url=u, title=f"§{k}", index=k) for k in range(6)])
        for u in urls]


def main():
    global JEV_DELAY, ASK_FIRST
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--jev-delay", type=float, default=JEV_DELAY)
    parser.add_argument("--ask", action="store_true",
                        help="make the stand-in ask a question before it answers")
    args = parser.parse_args()
    JEV_DELAY, ASK_FIRST = args.jev_delay, args.ask
    harness.Transport = OfflineTransport
    agent.search, agent.fetch_all = _search, _fetch
    vendors.list_models = lambda vendor, base, key: ([], "offline")
    config = Config(api_key="offline-not-a-key-000000")
    config.host, config.port = "127.0.0.1", args.port
    print("  OFFLINE: every engine is a local fake; nothing is sent anywhere.")
    server.serve(config)


if __name__ == "__main__":
    main()
