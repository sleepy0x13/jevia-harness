#!/usr/bin/env python3
"""Three ways to use the harness from Python. Run: python3 examples/use_as_a_library.py

Needs OPENROUTER_API_KEY in .env or the environment.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jevharness.harness import Harness, Settings, parse_task
from jevharness.questions import Choice, Noul, Score

TICKET = """From: dana@acme.example
Subject: still no refund, cancelling today

This is my third email. You charged me $240 on the 2nd for the annual plan I
cancelled on the 1st. Checkout also threw a 500 error, screenshot attached.
If this is not resolved today I am disputing it with my bank.
"""


def banner(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


harness = Harness(Settings.from_env())


# 1. Typed decisions. One Jev call, no LLM, guaranteed. Use this in production
#    code paths where you want a label rather than a paragraph.
banner("1. decide() — typed, one call, never an LLM")
result = harness.decide(
    TICKET,
    {
        "department": Choice(
            instructions="Which team should handle this",
            options={
                "billing": "Payments, refunds, subscriptions",
                "technical": "Bugs and integration failures",
                "sales": "Pricing and account questions",
            },
        ),
        "severity": Score(
            instructions="How severe is this for the customer",
            levels=[
                "Minor annoyance",
                "Real problem, has a workaround",
                "Blocking and money is involved",
                "Legal or chargeback risk",
            ],
        ),
        "refund_requested": Noul(instructions="Is the customer asking for a refund?"),
        "churn_risk": Noul(instructions="Is this customer about to leave?"),
    },
)
for name, answer in result.decisions.items():
    print(f"  {name:<18} {answer.describe():<42} certainty {answer.certainty:.2f}")
print(f"  -> {len(result.decisions)} decisions, "
      f"{len(result.ledger.calls)} call(s), ${result.ledger.total_cost:.8f}, "
      f"{result.elapsed_ms}ms")

# Route on the typed answer, in ordinary Python.
if result.decisions["severity"].value >= 2.5 and result.decisions["churn_risk"].value:
    print("  -> would page the retention queue")


# 2. A natural-language task. The harness plans it, batches the decisions, and
#    calls a model only if prose is actually owed.
banner("2. run() — planned, gated, model chosen by capability")
result = harness.run(parse_task({
    "prompt": "Triage this ticket and draft a reply only if one is warranted.",
    "state": TICKET,
}))
print(f"  plan      {result.plan.strategy}  [{result.plan.source}]")
print(f"  decisions {', '.join(result.decisions) or '—'}")
print(f"  model     {result.selection.describe() if result.selection else '—'}")
print(f"  skipped   {result.generation_skipped}  {result.skip_reason}")
for step in result.steps:
    print(f"    {step.engine:<8} {step.name:<16} {step.detail}")
print(f"  cost      ${result.ledger.total_cost:.8f}   {result.comparison.note()}")
print(f"  output    {(result.output or '')[:160].strip()}...")


# 3. Plan without spending anything, to see the decomposition.
banner("3. plan_only() — see the decomposition before paying for it")
plan = harness.plan_only(parse_task({
    "prompt": "Classify this ticket into billing, technical or sales.",
    "state": TICKET,
}))
print(f"  {plan.strategy} [{plan.source}] — {plan.notes}")
for name, question in plan.decisions.items():
    print(f"    {name}: {question.kind} — {question.instructions[:60]}")
