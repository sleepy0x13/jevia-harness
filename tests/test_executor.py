import unittest

from jevharness.config import Thresholds
from jevharness.errors import ConfigError
from jevharness.executor import Executor
from jevharness.plan import Task
from jevharness.planner import Planner, PlanCache
from jevharness.providers import JevClient, LLMClient
from jevharness.roster import CAPABILITY_DECISION, Credential, ModelSpec, Roster
from tests.fakes import FakeTransport, choice, noul, score

CAP_LEVELS = 5

DECIDE_AND_DRAFT = """{
  "strategy": "decide then draft",
  "answer_from": "generation",
  "decisions": {
    "needs_reply": {"type": "noul", "instructions": "Does this need a reply?"},
    "topic": {"type": "choice", "instructions": "What is it about",
              "criteria": {"billing": "money", "bug": "software"}},
    "heat": {"type": "score", "instructions": "How upset",
             "criteria": ["calm", "annoyed", "furious"]}
  },
  "gate": {"skip_generation_when": [{"decision": "needs_reply", "op": "no"}]},
  "generation": {"instruction": "Draft the reply."},
  "notes": "gate first"
}"""


def build(*, decisions=None, completions=None, decision_hook=None,
          models=None, thresholds=None, escalate=True, guard=False):
    transport = FakeTransport(decisions=decisions, completions=completions,
                              decision_hook=decision_hook)
    specs = models or [
        ModelSpec(id="tiny", model="v/tiny", capability=1, price_out=0.01, label="tiny"),
        ModelSpec(id="mid", model="v/mid", capability=2, price_out=0.10, label="mid"),
        ModelSpec(id="big", model="v/big", capability=4, price_out=1.00, label="big"),
    ]
    roster = Roster(models=specs,
                    credentials={"default": Credential("default", "sk-fake-000000000000")})
    roster.validate()
    llm = LLMClient(transport, "v/mid")
    executor = Executor(
        jev=JevClient(transport, "fake/jev"),
        llm_factory=lambda spec: LLMClient(transport, spec.model),
        planner=Planner(llm=llm, cache=PlanCache()),
        roster=roster,
        thresholds=thresholds or Thresholds(),
        escalate_uncertain=escalate,
        guard_output=guard,
    )
    return executor, transport


class TestBatching(unittest.TestCase):
    def test_every_decision_and_the_model_sizing_ride_in_one_call(self):
        executor, transport = build(
            completions=[DECIDE_AND_DRAFT, "Dear customer, sorry."],
            decisions={
                "needs_reply": noul(0.95),
                "topic": choice("billing", {"billing": 0.95, "bug": 0.05}, 0.94),
                "heat": score(2.0, 3, 0.9),
                CAPABILITY_DECISION: score(2.0, CAP_LEVELS, 0.9),
            },
        )
        result = executor.run(Task(prompt="Handle this and reply if warranted.", state="angry mail"))

        batches = [c for c in transport.decision_calls if "needs_reply" in (c["questions"] or {})]
        self.assertEqual(len(batches), 1, "N decisions must cost one Jev call")
        asked = batches[0]["questions"]
        self.assertEqual(len(asked), 4)
        self.assertIn(CAPABILITY_DECISION, asked)
        self.assertEqual(len(result.decisions), 3)
        self.assertNotIn(CAPABILITY_DECISION, result.decisions)

    def test_declared_decisions_never_touch_an_llm(self):
        from jevharness.questions import Choice, Noul

        executor, transport = build(decisions={
            "dept": choice("billing", {"billing": 0.99, "sales": 0.01}, 0.98),
            "refund": noul(0.9),
        })
        task = Task(
            prompt="triage",
            state="mail",
            questions={
                "dept": Choice(instructions="which team",
                               options={"billing": "money", "sales": "deals"}),
                "refund": Noul(instructions="refund asked?"),
            },
        )
        result = executor.run(task)
        self.assertEqual(len(transport.decision_calls), 1,
                         "a declared schema asks nothing extra")
        self.assertEqual(transport.chat_calls, [])
        self.assertTrue(result.generation_skipped)
        self.assertIn("dept", result.output)


class TestGating(unittest.TestCase):
    def test_gate_skips_the_expensive_call(self):
        executor, transport = build(
            completions=[DECIDE_AND_DRAFT],
            decisions={
                "needs_reply": noul(0.02),
                "topic": choice("bug", {"bug": 0.9, "billing": 0.1}, 0.9),
                "heat": score(0.0, 3, 0.95),
                CAPABILITY_DECISION: score(2.0, CAP_LEVELS, 0.9),
            },
        )
        result = executor.run(Task(prompt="Handle this and reply if warranted.", state="praise"))
        self.assertTrue(result.generation_skipped)
        self.assertIn("needs_reply is no", result.skip_reason)
        # One chat call for compiling the plan, and none for generation.
        self.assertEqual(len(transport.chat_calls), 1)
        self.assertEqual(result.output.count("\n") + 1, 3, "the three decisions are the answer")
        self.assertIn("topic", result.output)

    def test_level_zero_retires_generation(self):
        executor, transport = build(
            completions=[DECIDE_AND_DRAFT],
            decisions={
                "needs_reply": noul(0.99),
                "topic": choice("billing", {"billing": 0.99, "bug": 0.01}, 0.98),
                "heat": score(1.0, 3, 0.9),
                CAPABILITY_DECISION: score(0.0, CAP_LEVELS, 0.97),
            },
        )
        result = executor.run(Task(prompt="Handle this and reply if warranted.", state="mail"))
        self.assertTrue(result.generation_skipped)
        self.assertIn("whole answer", result.skip_reason)
        self.assertEqual(len(transport.chat_calls), 1)  # the compile only

    def test_level_zero_cannot_leave_the_caller_empty_handed(self):
        """A pure writing task has no decisions to answer with, so it must generate."""
        executor, transport = build(
            completions=["the release note"],
            decisions={CAPABILITY_DECISION: score(0.0, CAP_LEVELS, 0.99)},
        )
        result = executor.run(Task(prompt="Write a two-sentence release note.", state="a change"))
        self.assertFalse(result.generation_skipped)
        self.assertEqual(result.output, "the release note")
        self.assertEqual(len(transport.chat_calls), 1)


class TestModelChoice(unittest.TestCase):
    def cases(self):
        return {1.0: "v/tiny", 2.0: "v/mid", 4.0: "v/big"}

    def test_cheapest_model_that_clears_the_rung_is_used(self):
        for rung, expected in self.cases().items():
            executor, transport = build(
                completions=["ok"],
                decisions={CAPABILITY_DECISION: score(rung, CAP_LEVELS, 0.95)},
            )
            executor.run(Task(prompt="Write a note about this.", state="x"))
            self.assertEqual(transport.chat_calls[0]["model"], expected, f"rung {rung}")

    def test_pinned_model_bypasses_selection(self):
        plan = """{"strategy":"pinned","answer_from":"generation","decisions":{},
                   "generation":{"instruction":"Write it.","model_id":"big"}}"""
        executor, transport = build(
            completions=[plan, "ok"],
            decisions={CAPABILITY_DECISION: score(1.0, CAP_LEVELS, 0.99)},
        )
        executor.run(Task(prompt="Handle this and write if warranted.", state="x"))
        self.assertEqual(transport.chat_calls[-1]["model"], "v/big")

    def test_pinning_an_absent_model_is_a_config_error(self):
        plan = """{"strategy":"pinned","answer_from":"generation","decisions":{},
                   "generation":{"instruction":"Write it.","model_id":"ghost"}}"""
        executor, _ = build(completions=[plan],
                            decisions={CAPABILITY_DECISION: score(1.0, CAP_LEVELS, 0.9)})
        with self.assertRaises(ConfigError):
            executor.run(Task(prompt="Handle this and write if warranted.", state="x"))

    def test_an_empty_roster_is_refused_before_spending_anything(self):
        roster = Roster(models=[], credentials={"default": Credential("default", "sk-fake-0000000000")})
        transport = FakeTransport()
        executor = Executor(
            jev=JevClient(transport, "fake/jev"),
            llm_factory=lambda spec: LLMClient(transport, spec.model),
            planner=Planner(),
            roster=roster,
            thresholds=Thresholds(),
        )
        with self.assertRaises(ConfigError):
            executor.run(Task(prompt="Write something."))
        self.assertEqual(transport.requests, [])


class TestUncertainty(unittest.TestCase):
    def test_uncertain_decisions_are_flagged_not_silently_accepted(self):
        from jevharness.questions import Choice

        executor, _ = build(decisions={
            "dept": choice("billing", {"billing": 0.34, "sales": 0.33, "other": 0.33}, 0.35),
        })
        result = executor.run(Task(
            prompt="triage", state="mail",
            questions={"dept": Choice(instructions="which team",
                                      options={"billing": "a", "sales": "b", "other": "c"})},
        ))
        self.assertIn("dept", result.review.uncertain)

    def test_a_coin_flip_noul_counts_as_uncertain(self):
        from jevharness.questions import Noul

        executor, transport = build(decisions={"q": noul(0.5)}, completions=["settled: yes"])
        result = executor.run(Task(prompt="decide", state="mail",
                                   questions={"q": Noul(instructions="is it?")}))
        self.assertIn("q", result.review.uncertain)
        self.assertTrue(result.escalated)
        self.assertEqual(transport.chat_calls[-1]["model"], "v/big",
                         "escalation goes to the strongest model")
        self.assertIn("settled: yes", result.output)

    def test_escalation_can_be_switched_off(self):
        from jevharness.questions import Noul

        executor, transport = build(decisions={"q": noul(0.5)}, escalate=False)
        result = executor.run(Task(prompt="decide", state="mail",
                                   questions={"q": Noul(instructions="is it?")}))
        self.assertFalse(result.escalated)
        self.assertEqual(transport.chat_calls, [])

    def test_confident_answers_are_not_escalated(self):
        from jevharness.questions import Noul

        executor, transport = build(decisions={"q": noul(0.97)})
        result = executor.run(Task(prompt="decide", state="mail",
                                   questions={"q": Noul(instructions="is it?")}))
        self.assertFalse(result.escalated)
        self.assertEqual(transport.chat_calls, [])


class TestGuardAndAccounting(unittest.TestCase):
    def test_guard_screens_output_with_jev_not_an_llm(self):
        executor, transport = build(
            completions=["the note"],
            decision_hook=lambda payload: (
                {"on_task": noul(0.95), "refused": noul(0.02)}
                if "on_task" in (payload.get("questions") or {})
                else {CAPABILITY_DECISION: score(1.0, CAP_LEVELS, 0.95)}
            ),
            guard=True,
        )
        result = executor.run(Task(prompt="Write a note about this.", state="x"))
        # assess (is research needed) + the capability batch + the guard.
        self.assertEqual(len(transport.decision_calls), 3)
        self.assertEqual(len(transport.chat_calls), 1, "the guard is not an LLM judge")
        self.assertIn("on_task", result.guard)

    def test_streaming_and_blocking_paths_agree(self):
        kwargs = dict(completions=["hello there"],
                      decisions={CAPABILITY_DECISION: score(1.0, CAP_LEVELS, 0.95)})
        blocking, _ = build(**kwargs)
        result = blocking.run(Task(prompt="Write a note about this.", state="x"))

        streaming, _ = build(**kwargs)
        deltas, final = [], None
        for event in streaming.stream(Task(prompt="Write a note about this.", state="x")):
            if event["type"] == "delta":
                deltas.append(event["text"])
            elif event["type"] == "done":
                final = event["result"]
        self.assertEqual("".join(deltas), result.output)
        self.assertEqual(final["output"], result.output)

    def test_the_ledger_counts_every_call(self):
        executor, _ = build(
            completions=[DECIDE_AND_DRAFT, "a reply"],
            decisions={
                "needs_reply": noul(0.95),
                "topic": choice("billing", {"billing": 0.99, "bug": 0.01}, 0.98),
                "heat": score(2.0, 3, 0.95),
                CAPABILITY_DECISION: score(2.0, CAP_LEVELS, 0.95),
            },
        )
        result = executor.run(Task(prompt="Handle this and reply if warranted.", state="mail"))
        engines = result.ledger.by_engine()
        # assess, then the one batch carrying every decision plus the sizing.
        self.assertEqual(engines["jev"]["calls"], 2)
        self.assertEqual(engines["llm"]["calls"], 2)  # compile + generate
        self.assertEqual(result.comparison.decisions_answered, 3)
        self.assertEqual(
            [s.code for s in result.steps],
            ["assess", "plan_compiled", "jev_batch", "model", "generate"],
        )
        # The planning call is attributed separately, so the first run can be
        # reported as dearer without hiding it.
        self.assertGreater(result.comparison.plan_cost, 0)
        self.assertLess(result.comparison.steady_state_cost, result.comparison.actual_cost)

    def test_the_second_task_of_a_shape_is_cheaper_than_the_first(self):
        """The economics of the harness are about reuse, so measure reuse."""
        executor, transport = build(
            completions=[DECIDE_AND_DRAFT, "reply one", "reply two"],
            decisions={
                "needs_reply": noul(0.95),
                "topic": choice("billing", {"billing": 0.99, "bug": 0.01}, 0.98),
                "heat": score(2.0, 3, 0.95),
                CAPABILITY_DECISION: score(2.0, CAP_LEVELS, 0.95),
            },
        )
        first = executor.run(Task(prompt="Handle this and reply if warranted.", state="mail one"))
        second = executor.run(Task(prompt="Handle this and reply if warranted.", state="mail two"))

        self.assertEqual(first.plan.source, "compiled")
        self.assertEqual(second.plan.source, "cached")
        self.assertGreater(first.comparison.plan_cost, 0)
        self.assertEqual(second.comparison.plan_cost, 0)
        self.assertLess(second.comparison.actual_cost, first.comparison.actual_cost)
        self.assertNotEqual(second.comparison.verdict, "dearer")
        self.assertEqual(len([c for c in transport.chat_calls
                              if "planner for a two-engine harness" in
                              str(c.get("messages"))]), 1)


if __name__ == "__main__":
    unittest.main()


class TestResearchIntegration(unittest.TestCase):
    """Research runs first, and what it finds reaches everything downstream."""

    PLAN = """{
      "strategy": "look it up",
      "answer_from": "generation",
      "research": {"rounds": 1, "query": "seed", "top_k": 2},
      "steps": ["find the fact", "write it up"],
      "decisions": {"is_recent": {"type": "noul", "instructions": "Is it recent?"}},
      "gate": {"skip_generation_when": [{"decision": "is_recent", "op": "no"}]},
      "generation": {"instruction": "Explain it, but only if it is recent."}
    }"""

    def setUp(self):
        import jevharness.agent as agent
        from jevharness.research import Hit, Page, Section

        self.agent = agent
        self._search, self._fetch = agent.search, agent.fetch_all
        agent.search = lambda q, limit=10: [
            Hit(title="t", url="https://e/1", snippet="s", rank=0)
        ]
        agent.fetch_all = lambda urls, workers=4: [
            Page(url=u, title="Src", text="",
                 sections=[Section(text="THE FOUND FACT " + "x" * 260, url=u, title="Src")])
            for u in urls
        ]

    def tearDown(self):
        self.agent.search, self.agent.fetch_all = self._search, self._fetch

    def hook(self, *, is_recent=0.9, keep=0.9, enough=0.95, done=0.05,
             capability=2.0, web=0.05, independent=0.1, assembly=0.1):
        def respond(payload):
            asked = payload.get("questions") or {}
            out = {}
            for name in asked:
                if name == CAPABILITY_DECISION or name.startswith("c"):
                    out[name] = score(capability, CAP_LEVELS, 0.95)
                elif name == "is_recent":
                    out[name] = noul(is_recent)
                elif name == "__enough__":
                    out[name] = noul(enough)
                elif name == "__needs_research__":
                    out[name] = noul(0.95)
                elif name == "__wants_app__":
                    out[name] = noul(0.02)
                elif name == "__independent__":
                    out[name] = noul(independent)
                elif name == "__assembly__":
                    out[name] = noul(assembly)
                elif name.startswith("r"):
                    out[name] = choice("extractor", {"extractor": 0.95}, 0.95)
                elif name.startswith("d"):
                    out[name] = noul(done)
                elif name.startswith("w"):
                    out[name] = noul(web)
                else:
                    out[name] = noul(keep)
            return out

        return respond

    def test_the_evidence_reaches_the_decision_call(self):
        executor, transport = build(completions=[self.PLAN, "an answer"],
                                    decision_hook=self.hook())
        executor.run(Task(prompt="Explain the thing, only if it is recent.", state="local notes"))
        # triage, section filter, todo, then the decision batch
        decision_call = transport.decision_calls[-1]
        self.assertIn("THE FOUND FACT", decision_call["state"])
        self.assertIn("local notes", decision_call["state"])

    def test_the_evidence_reaches_every_subtask_prompt(self):
        executor, transport = build(completions=[self.PLAN, "part one", "part two"],
                                    decision_hook=self.hook())
        executor.run(Task(prompt="Explain the thing, only if it is recent.", state="notes"))
        subtask_prompts = [str(c["messages"]) for c in transport.chat_calls[1:]]
        self.assertEqual(len(subtask_prompts), 2, "one call per step")
        for prompt in subtask_prompts:
            self.assertIn("THE FOUND FACT", prompt)
            self.assertIn("SOURCES", prompt)

    def test_a_step_the_material_already_answers_is_not_run(self):
        executor, transport = build(completions=[self.PLAN, "only one part"],
                                    decision_hook=self.hook(done=0.95))
        result = executor.run(Task(prompt="Explain the thing, only if it is recent.", state="n"))
        self.assertEqual([t.status for t in result.subtasks], ["skipped", "skipped"])
        self.assertEqual(len(transport.chat_calls), 1, "the compile only")

    def test_each_step_runs_on_the_model_its_rung_calls_for(self):
        executor, transport = build(completions=[self.PLAN, "a", "b"],
                                    decision_hook=self.hook(capability=4.0))
        result = executor.run(Task(prompt="Explain the thing, only if it is recent.", state="n"))
        self.assertEqual([t.model_label for t in result.subtasks], ["big", "big"])
        self.assertEqual([c["model"] for c in transport.chat_calls[1:]], ["v/big", "v/big"])

    def test_sections_are_joined_without_an_extra_call_when_they_stand_alone(self):
        executor, transport = build(completions=[self.PLAN, "part one", "part two"],
                                    decision_hook=self.hook(assembly=0.05))
        result = executor.run(Task(prompt="Explain the thing, only if it is recent.", state="n"))
        self.assertEqual(len(transport.chat_calls), 3, "compile + two steps, no assembly call")
        self.assertIn("## Find the fact", result.output)
        self.assertIn("part two", result.output)

    def test_a_heading_the_model_wrote_anyway_is_not_printed_twice(self):
        executor, _ = build(completions=[self.PLAN, "## Find the fact\n\nbody one", "body two"],
                            decision_hook=self.hook(assembly=0.05))
        result = executor.run(Task(prompt="Explain the thing, only if it is recent.", state="n"))
        self.assertEqual(result.output.count("## Find the fact"), 1)
        self.assertIn("body one", result.output)

    def test_the_subtask_prompt_forbids_headings_and_source_lists(self):
        executor, transport = build(completions=[self.PLAN, "a", "b"], decision_hook=self.hook())
        executor.run(Task(prompt="Explain the thing, only if it is recent.", state="n"))
        system = transport.chat_calls[1]["messages"][0]["content"]
        self.assertIn("Do not write a heading", system)
        self.assertIn("do not list the sources", system)

    def test_an_assembly_call_runs_only_when_jev_asks_for_one(self):
        executor, transport = build(completions=[self.PLAN, "part one", "part two", "one piece"],
                                    decision_hook=self.hook(assembly=0.95))
        result = executor.run(Task(prompt="Explain the thing, only if it is recent.", state="n"))
        self.assertEqual(len(transport.chat_calls), 4)
        self.assertEqual(result.output, "one piece")
        self.assertIn("assemble", [s.code for s in result.steps])

    def test_a_firing_gate_cannot_silence_a_successful_search(self):
        executor, _ = build(completions=[self.PLAN, "a", "b"],
                            decision_hook=self.hook(is_recent=0.02))
        result = executor.run(Task(prompt="Explain the thing, only if it is recent.", state="n"))
        self.assertFalse(result.generation_skipped)
        self.assertTrue(result.output)
        self.assertIn("gate_ignored", [s.code for s in result.steps])

    def test_research_can_be_switched_off(self):
        executor, transport = build(completions=[self.PLAN, "a", "b"],
                                    decision_hook=self.hook())
        executor.research_enabled = False
        result = executor.run(Task(prompt="Explain the thing, only if it is recent.", state="n"))
        self.assertIsNone(result.evidence)
        self.assertNotIn("research", [s.code for s in result.steps])

    def test_the_trail_names_every_stage_in_order(self):
        executor, _ = build(completions=[self.PLAN, "a", "b"], decision_hook=self.hook())
        result = executor.run(Task(prompt="Explain the thing, only if it is recent.", state="n"))
        self.assertEqual(
            [s.code for s in result.steps],
            ["assess", "plan_compiled", "research", "route", "jev_batch", "model",
             "subtask", "subtask", "assemble_free"],
        )

    def test_independent_steps_run_at_the_same_time(self):
        executor, _ = build(completions=[self.PLAN, "a", "b"],
                            decision_hook=self.hook(independent=0.95))
        result = executor.run(Task(prompt="Explain the thing, only if it is recent.", state="n"))
        self.assertTrue(result.routing.independent)
        self.assertEqual([t.status for t in result.subtasks], ["done", "done"])


class TestBespokePersona(unittest.TestCase):
    """A persona the planner wrote wins over the preset role."""

    PLAN = """{
      "strategy": "specialist",
      "answer_from": "generation",
      "steps": [{"title": "the regulatory annex",
                 "persona": "You are a compliance officer who cites clause numbers."}],
      "decisions": {},
      "generation": {"instruction": "Write it."}
    }"""

    def hook(self):
        def respond(payload):
            out = {}
            for name in payload["questions"]:
                if name.startswith("c") or name == CAPABILITY_DECISION:
                    out[name] = score(2.0, CAP_LEVELS, 0.95)
                elif name.startswith("r"):
                    out[name] = choice("writer", {"writer": 0.9}, 0.9)
                else:
                    out[name] = noul(0.05)
            return out

        return respond

    def test_the_bespoke_persona_reaches_the_model(self):
        executor, transport = build(completions=[self.PLAN, "the annex"],
                                    decision_hook=self.hook())
        # A prompt the deterministic fast path will not swallow, so the plan
        # above is the one that runs.
        executor.run(Task(
            prompt="Draft the regulatory annex section, and only if the rules require it.",
            state="rules"))
        system = transport.chat_calls[-1]["messages"][0]["content"]
        self.assertIn("compliance officer who cites clause numbers", system)
        self.assertNotIn("You write for a reader who is busy", system,
                         "the preset writer persona must not also be applied")
