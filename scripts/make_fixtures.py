"""Build the Demo replay fixtures by running the real executor on fake engines.

Every fixture is the decision-event record of an actual offline run: the same
executor, reducer and protocol as a live task, with Jev and the models replaced
by scripted answers. The numbers are chosen to exercise the interface; they are
not measurements of Jev or of any model, and each file says so.

    python3 scripts/make_fixtures.py        # writes jevharness/ui/fixtures/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jevharness import agent  # noqa: E402
from jevharness.config import Thresholds  # noqa: E402
from jevharness.errors import ProviderError  # noqa: E402
from jevharness.events import RunEvents  # noqa: E402
from jevharness.executor import Executor  # noqa: E402
from jevharness.plan import Task  # noqa: E402
from jevharness.planner import Planner, PlanCache  # noqa: E402
from jevharness.providers import JevClient, LLMClient  # noqa: E402
from jevharness.questions import Choice, Noul  # noqa: E402
from jevharness.research import Hit, Page, Section  # noqa: E402
from jevharness.roster import CAPABILITY_DECISION, Credential, ModelSpec, Roster  # noqa: E402
from tests.fakes import FakeTransport, _asks_for_search_terms, choice, noul, score  # noqa: E402


class TermsTransport(FakeTransport):
    """Writes a fresh search term each round, as a real query writer would."""

    terms = (["ev battery recycling rate 2026"], ["lithium recovery pilot plant results"])

    def post_json(self, path, payload):
        if _asks_for_search_terms(payload):
            self.requests.append((path, payload))
            pick = self.terms[min(len(self.term_calls), len(self.terms) - 1)]
            self.term_calls.append(payload)
            return {"model": payload.get("model"), "choices": [{"message": {"content": json.dumps(pick)}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 8, "cost": 0.00001}}
        return super().post_json(path, payload)

OUT = ROOT / "jevharness" / "ui" / "fixtures"
CAP = 5
NOTE = ("Synthetic: produced offline by scripts/make_fixtures.py with scripted answers. "
        "Not a measurement of Jev or of any model.")

MODELS = [
    ModelSpec(id="tiny", model="demo/tiny", capability=1, price_in=0.02, price_out=0.03, label="Tiny (demo)"),
    ModelSpec(id="free", model="demo/free", capability=2, price_in=0, price_out=0, label="Free (demo)"),
    ModelSpec(id="mid", model="demo/mid", capability=3, price_in=0.03, price_out=0.13, label="Mid (demo)"),
    ModelSpec(id="big", model="demo/big", capability=4, price_in=1.0, price_out=4.0, label="Big (demo)"),
]


def executor(transport, models=MODELS, **kw):
    roster = Roster(models=list(models), credentials={"default": Credential("default", "sk-fake-000000000000")})
    roster.validate()
    return Executor(
        jev=JevClient(transport, "fake/jev"),
        llm_factory=lambda spec: LLMClient(transport, spec.model, price=(spec.price_in, spec.price_out)),
        planner=Planner(llm=LLMClient(transport, "demo/mid"), cache=PlanCache()),
        roster=roster, thresholds=Thresholds(), events=RunEvents(), **kw)


def record(name, title, description, ex, task):
    ex.run(task)
    events = [dict(e) for e in ex.events.log]
    for e in events:
        e["run_id"] = f"fixture-{name}"
        e["event_id"] = e["event_id"].replace(ex.events.run_id, f"fixture-{name}")
    return {"name": name, "title": title, "description": description, "synthetic": True,
            "note": NOTE, "events": events}


# 1 · Chinese customer feedback, classified in one batch ---------------------- #
def feedback():
    items = [
        ("fb1", "付款后页面一直转圈，订单也没生成。", "bug_report", 0.91, 0.88),
        ("fb2", "希望能导出 Excel 格式的账单。", "feature_request", 0.84, 0.12),
        ("fb3", "客服回复很快，谢谢！", "other", 0.77, 0.03),
        ("fb4", "iOS 更新后登录就闪退，今天必须处理。", "bug_report", 0.95, 0.93),
        ("fb5", "能不能加一个深色模式？", "feature_request", 0.88, 0.05),
        ("fb6", "发票抬头改不了，不知道是不是故障。", "bug_report", 0.52, 0.41),
        ("fb7", "价格有点贵。", "other", 0.63, 0.08),
    ]
    options = {"bug_report": "Bug report", "feature_request": "Feature request", "other": "Other"}
    questions, targets, answers = {}, {}, {}
    for key, text, label, p, urgent in items:
        questions[f"{key}_category"] = Choice(instructions=f"这条反馈属于哪一类？「{text}」", options=options)
        questions[f"{key}_urgent"] = Noul(instructions=f"这条反馈需要今天处理吗？「{text}」")
        targets[f"{key}_category"] = targets[f"{key}_urgent"] = text
        rest = (1 - p) / 2
        probs = {k: (p if k == label else rest) for k in options}
        answers[f"{key}_category"] = choice(label, probs, 0.35 if p < 0.6 else min(0.95, p - 0.05))
        answers[f"{key}_urgent"] = noul(urgent)
    transport = FakeTransport(decision_hook=lambda payload: {k: answers[k] for k in payload["questions"] if k in answers})
    ex = executor(transport, escalate_uncertain=False)
    task = Task(prompt="把这些客户反馈分类，并标出需要今天处理的。", state="\n".join(t for _, t, *_ in items),
                questions=questions, targets=targets)
    return record("feedback-zh", "Chinese feedback classification · 中文客户反馈分类",
                  "One batch, 14 questions: a category per item and a separate urgency flag. No model writes anything.",
                  ex, task)


# 2 · Research: filter sections, then check what the writer will receive ----- #
def research():
    rounds = {"n": 0}

    def fake_search(term, limit=8):
        rounds["n"] += 1
        r = rounds["n"]
        return [Hit(title=f"Source {r}.{i} on EV battery recycling", url=f"https://example.org/r{r}/p{i}",
                    snippet=f"Recycling rates and costs, report {r}.{i}", rank=i) for i in range(6)]

    def fake_fetch(urls, workers=4):
        pages = []
        for u in urls:
            sections = [Section(text=f"{u} section {k}: " + ("Lithium recovery reached 90 percent in the pilot. " * 20),
                                url=u, title=f"{u.rsplit('/', 1)[-1]} §{k}", index=k) for k in range(30)]
            pages.append(Page(url=u, title=u, text="", sections=sections))
        return pages

    agent.search, agent.fetch_all = fake_search, fake_fetch
    check_round = {"n": 0}

    filters = {"n": 0}

    def hook(payload):
        qs = payload["questions"]
        if "s0" in qs:
            filters["n"] += 1
        out = {}
        for k in qs:
            if k == "__needs_research__":
                out[k] = noul(0.93)
            elif k in ("__multi_step__", "__wants_app__"):
                out[k] = noul(0.08)
            elif k.startswith("h"):
                out[k] = noul([0.91, 0.84, 0.35, 0.72, 0.2, 0.66][int(k[1:]) % 6])
            elif k == "__enough__":
                check_round["n"] += 1
                out[k] = noul(0.41 if check_round["n"] == 1 else 0.83)
            elif k.startswith("s"):
                i = int(k[1:])
                # The first round finds little worth keeping; the second, more.
                early = filters["n"] <= 3
                pattern = [0.92, 0.12, 0.47, 0.2, 0.05, 0.3, 0.1] if early else [0.92, 0.12, 0.47, 0.81, 0.05, 0.63, 0.3]
                out[k] = noul(pattern[i % 7])
            elif k == CAPABILITY_DECISION:
                out[k] = score(2.3, CAP, 0.82)
        return out

    transport = TermsTransport(decision_hook=hook, completions=["Pilot plants recover about 90% of lithium [1]; costs vary [2]."])
    ex = executor(transport)
    return record("research-filter", "Research: sources filtered · 研究材料筛选",
                  "Sections judged in budget-sized batches; the rest stay Not assessed. Sufficiency is checked on "
                  "what the writer receives, and a second round is run when the first is not enough.",
                  ex, Task(prompt="What is the latest EV battery recycling rate?"))


# 3 · Several steps, routed in one call ---------------------------------------- #
PLAN_STEPS = json.dumps({
    "strategy": "compare then recommend", "answer_from": "generation",
    "steps": [{"title": "Extract the prices from the three quotes", "role": "extractor"},
              {"title": "Weigh the trade-offs between the vendors", "role": "analyst"},
              {"title": "Summary table", "role": "writer"}],
    "decisions": {}, "generation": {"instruction": "Recommend a vendor."}, "notes": "split"})


def routing():
    def hook(payload):
        out = {}
        for k in payload["questions"]:
            if k in ("__needs_research__", "__wants_app__"):
                out[k] = noul(0.1)
            elif k == "__multi_step__":
                out[k] = noul(0.88)
            elif k == "c0":
                out[k] = score(1.2, CAP, 0.9)
            elif k == "c1":
                out[k] = score(2.6, CAP, 0.42)
            elif k == "c2":
                out[k] = score(2.1, CAP, 0.8)
            elif k.startswith("w"):
                out[k] = noul(0.1)
            elif k == "d2":
                out[k] = noul(0.9)
            elif k.startswith("d"):
                out[k] = noul(0.05)
            elif k == "__independent__":
                out[k] = noul(0.8)
            elif k == "__assembly__":
                out[k] = noul(0.2)
            elif k == CAPABILITY_DECISION:
                out[k] = score(2.0, CAP, 0.9)
        return out

    transport = FakeTransport(decision_hook=hook, completions=[
        PLAN_STEPS, "A: $12k, B: $9.5k, C: $14k.", "B is cheapest but slower to support; A balances both."])
    ex = executor(transport)
    return record("multi-route", "Several steps, one routing call · 多子任务分流",
                  "Jev rates every step in one batch; the policy rounds, raises on low confidence and picks the "
                  "cheapest eligible model. A step already written in the material is skipped.",
                  ex, Task(prompt="Compare the three vendor quotes and recommend one.",
                           state="Quote A ... Quote B ... Quote C ... Summary table: already attached."))


# 4 · Decided, and no generation needed ---------------------------------------- #
PLAN_DECIDE = json.dumps({
    "strategy": "decide only", "answer_from": "generation",
    "decisions": {"is_refund_request": {"type": "noul", "instructions": "Is this a refund request?"},
                  "priority": {"type": "score", "instructions": "How urgent is it?",
                               "criteria": ["low", "normal", "high", "critical"]}},
    "generation": {"instruction": "Reply only if needed."}, "notes": "judge first"})


def decide_only():
    def hook(payload):
        out = {}
        for k in payload["questions"]:
            if k in ("__needs_research__", "__wants_app__"):
                out[k] = noul(0.05)
            elif k == "__multi_step__":
                out[k] = noul(0.2)
            elif k == "is_refund_request":
                out[k] = noul(0.97)
            elif k == "priority":
                out[k] = score(1.7, 4, 0.74)
            elif k == CAPABILITY_DECISION:
                out[k] = score(0.3, CAP, 0.88)
        return out

    transport = FakeTransport(decision_hook=hook, completions=[PLAN_DECIDE])
    ex = executor(transport)
    return record("decide-no-generate", "Decided without writing · 判断后不生成",
                  "The plan was written by a model, then Jev's answers were the whole deliverable: no generation "
                  "call for this step — but not zero LLM calls for the run.",
                  ex, Task(prompt="Is this email a refund request, and how urgent is it?",
                           state="Hi, I was charged twice for order 5521, please refund one."))


# 5 · Needs review: low confidence and no qualified model --------------------- #
PLAN_HARD = json.dumps({
    "strategy": "assess the contract", "answer_from": "generation",
    "decisions": {"has_auto_renewal": {"type": "noul", "instructions": "Does the contract renew automatically?"},
                  "jurisdiction": {"type": "choice", "instructions": "Which law governs it?",
                                   "criteria": {"uk": "England and Wales", "de": "Germany", "us_ny": "New York"}}},
    "generation": {"instruction": "Write a risk memo."}, "notes": "high stakes"})


def needs_review():
    def hook(payload):
        out = {}
        for k in payload["questions"]:
            if k in ("__needs_research__", "__wants_app__"):
                out[k] = noul(0.1)
            elif k == "__multi_step__":
                out[k] = noul(0.7)
            elif k == "has_auto_renewal":
                out[k] = noul(0.52)
            elif k == "jurisdiction":
                out[k] = choice("uk", {"uk": 0.46, "de": 0.31, "us_ny": 0.23}, 0.21)
            elif k == CAPABILITY_DECISION:
                out[k] = score(3.7, CAP, 0.86)
        return out

    small = [m for m in MODELS if m.capability <= 3]
    transport = FakeTransport(decision_hook=hook, completions=[PLAN_HARD])
    ex = executor(transport, models=small, escalate_uncertain=False)
    return record("needs-review", "Needs review · 存疑与无合格模型",
                  "Two answers sit in the uncertain band, and the memo needs level 4 while the best configured model "
                  "is level 3: the step waits for the user instead of running on a weaker model.",
                  ex, Task(prompt="Review this contract and write a risk memo.", state="Clause 12: ... renewal ..."))


# 6 · A failure branch: the Jev batch itself fails ---------------------------- #
def jev_failure():
    def hook(payload):
        if CAPABILITY_DECISION in payload["questions"]:
            raise ProviderError("upstream HTTP 503", detail={"message": "Service temporarily unavailable"})
        out = {}
        for k in payload["questions"]:
            out[k] = noul(0.1 if k != "__multi_step__" else 0.2)
        return out

    transport = FakeTransport(decision_hook=hook, completions=[PLAN_DECIDE])
    ex = executor(transport)
    return record("jev-failed", "Failure branch: Jev request failed · 失败分支",
                  "The decisions batch fails. It is shown as a failed request with its reason — never as a No — "
                  "and the run ends needing review.",
                  ex, Task(prompt="Is this email a refund request, and how urgent is it?", state="Charged twice."))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    search, fetch = agent.search, agent.fetch_all
    try:
        fixtures = [feedback(), research(), routing(), decide_only(), needs_review(), jev_failure()]
    finally:
        agent.search, agent.fetch_all = search, fetch
    index = []
    for fx in fixtures:
        text = json.dumps(fx, ensure_ascii=False, indent=1)
        assert "sk-fake" not in text, "a fixture must never carry a key"
        (OUT / f"{fx['name']}.json").write_text(text + "\n", encoding="utf-8")
        index.append({"name": fx["name"], "file": f"{fx['name']}.json", "title": fx["title"],
                      "description": fx["description"], "events": len(fx["events"])})
    (OUT / "index.json").write_text(json.dumps({"synthetic": True, "note": NOTE, "fixtures": index},
                                               ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for row in index:
        print(f"{row['name']:22} {row['events']:4} events")


if __name__ == "__main__":
    main()
