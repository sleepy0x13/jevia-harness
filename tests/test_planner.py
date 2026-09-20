import unittest

from jevharness.errors import CompileError
from jevharness.plan import SOURCE_CACHED, SOURCE_COMPILED, SOURCE_DECLARED, Task
from jevharness.planner import Planner, PlanCache, _first_json_object
from jevharness.providers import LLMClient
from jevharness.questions import Noul
from tests.fakes import FakeTransport

PLAN_JSON = """{
  "strategy": "decide then draft",
  "answer_from": "generation",
  "decisions": {"needs_reply": {"type": "noul", "instructions": "Does this need a reply?"}},
  "gate": {"skip_generation_when": [{"decision": "needs_reply", "op": "no"}]},
  "generation": {"instruction": "Draft the reply."},
  "notes": "the gate skips the expensive call"
}"""


class TestDeterministicRecognisers(unittest.TestCase):
    def setUp(self):
        self.planner = Planner()

    def source_of(self, prompt, **kw):
        plan, call = self.planner.plan(Task(prompt=prompt, **kw))
        self.assertIsNone(call, "a recognised task must not cost a planning call")
        return plan

    def test_enumerated_options_become_one_choice(self):
        plan = self.source_of("Classify this ticket into billing, technical or sales.")
        self.assertEqual(plan.answer_from, "decisions")
        self.assertEqual(len(plan.decisions), 1)
        question = next(iter(plan.decisions.values()))
        self.assertEqual(question.kind, "choice")
        self.assertEqual(
            sorted(question.options.values()), ["billing", "sales", "technical"]
        )

    def test_chinese_enumeration_is_recognised(self):
        plan = self.source_of("把这条工单分类到 计费、技术、销售")
        self.assertEqual(plan.answer_from, "decisions")
        self.assertEqual(next(iter(plan.decisions.values())).kind, "choice")

    def test_numeric_range_becomes_score_levels(self):
        plan = self.source_of("Rate the severity of this bug from 1 to 5.")
        question = plan.decisions["rating"]
        self.assertEqual(question.kind, "score")
        self.assertEqual(list(question.levels), ["1", "2", "3", "4", "5"])

    def test_bare_yes_no_becomes_a_noul(self):
        plan = self.source_of("Is this message a bug report?")
        self.assertEqual(plan.decisions["answer"].kind, "noul")

    def test_plain_writing_skips_planning_entirely(self):
        plan = self.source_of("Write a two-sentence release note for this change.")
        self.assertEqual(plan.answer_from, "generation")
        self.assertEqual(plan.decisions, {})

    def test_conditional_writing_is_not_treated_as_plain(self):
        """'draft a reply if warranted' hides a decision and must be compiled."""
        plan, _ = Planner().plan(Task(prompt="Draft a reply if one is warranted."))
        self.assertNotEqual(plan.source, "deterministic")

    def test_chinese_conditional_is_not_treated_as_plain(self):
        plan, _ = Planner().plan(Task(prompt="判断这条评论要不要人工回复，需要的话起草一条。"))
        self.assertNotEqual(plan.source, "deterministic")

    def test_declared_questions_win_outright(self):
        plan = self.source_of("anything at all", questions={"q": Noul(instructions="yes?")})
        self.assertEqual(plan.source, SOURCE_DECLARED)
        self.assertEqual(plan.answer_from, "decisions")


class TestCompilation(unittest.TestCase):
    def planner(self, *completions):
        transport = FakeTransport(completions=list(completions))
        self.transport = transport
        return Planner(llm=LLMClient(transport, "fake/model"), cache=PlanCache())

    def test_compiles_once_then_serves_from_cache(self):
        planner = self.planner(PLAN_JSON)
        task_a = Task(prompt="Handle this review and reply if warranted.", state="one")
        task_b = Task(prompt="Handle this review and reply if warranted.", state="two")

        plan_a, call_a = planner.plan(task_a)
        self.assertEqual(plan_a.source, SOURCE_COMPILED)
        self.assertIsNotNone(call_a)

        plan_b, call_b = planner.plan(task_b)
        self.assertEqual(plan_b.source, SOURCE_CACHED)
        self.assertIsNone(call_b, "a cached plan must not cost a second call")
        self.assertEqual(len(self.transport.chat_calls), 1)

    def test_compiler_runs_without_chain_of_thought(self):
        planner = self.planner(PLAN_JSON)
        planner.plan(Task(prompt="Handle this and reply if warranted."))
        body = self.transport.chat_calls[0]
        self.assertEqual(body["reasoning"], {"enabled": False})

    def test_unusable_json_degrades_to_an_llm_only_plan(self):
        planner = self.planner("I'm afraid I can't do that.")
        plan, call = planner.plan(Task(prompt="Handle this and reply if warranted."))
        self.assertEqual(plan.source, "fallback")
        self.assertEqual(plan.answer_from, "generation")
        self.assertIn("Planning was skipped", plan.notes)

    def test_invalid_plan_shape_degrades_too(self):
        planner = self.planner('{"strategy":"x","answer_from":"decisions","decisions":{}}')
        plan, _ = planner.plan(Task(prompt="Handle this and reply if warranted."))
        self.assertEqual(plan.source, "fallback")

    def test_no_planner_model_degrades_gracefully(self):
        plan, call = Planner().plan(Task(prompt="Handle this and reply if warranted."))
        self.assertEqual(plan.source, "fallback")
        self.assertIsNone(call)


class TestJsonExtraction(unittest.TestCase):
    def test_strips_code_fences(self):
        self.assertEqual(_first_json_object('```json\n{"a": 1}\n```'), '{"a": 1}')

    def test_ignores_chatter_around_the_object(self):
        self.assertEqual(_first_json_object('Sure! {"a": 1} hope that helps'), '{"a": 1}')

    def test_handles_braces_inside_strings(self):
        raw = '{"a": "a } brace", "b": {"c": 1}}'
        self.assertEqual(_first_json_object(raw), raw)

    def test_rejects_unterminated_and_absent_objects(self):
        for bad in ('{"a": 1', "no json here"):
            with self.assertRaises(CompileError):
                _first_json_object(bad)


class TestPlanCache(unittest.TestCase):
    def test_evicts_least_recently_used(self):
        cache = PlanCache(capacity=2)
        from jevharness.plan import Plan

        def plan(name):
            return Plan(strategy=name, answer_from="decisions",
                        decisions={"d": Noul(instructions="q")})

        cache.put("a", plan("a"))
        cache.put("b", plan("b"))
        cache.get("a")
        cache.put("c", plan("c"))
        self.assertIsNone(cache.get("b"))
        self.assertIsNotNone(cache.get("a"))
        self.assertEqual(cache.stats()["size"], 2)


if __name__ == "__main__":
    unittest.main()


class TestPlanRepair(unittest.TestCase):
    """A compiled plan that cannot write must not be accepted for a writing task."""

    DECISIONS_ONLY = """{
      "strategy": "judge first",
      "answer_from": "decisions",
      "decisions": {"needs_reply": {"type": "noul", "instructions": "Does it need a reply?"}},
      "gate": {"skip_generation_when": [{"decision": "needs_reply", "op": "no"}]},
      "generation": null
    }"""

    def planner(self, *completions):
        transport = FakeTransport(completions=list(completions))
        return Planner(llm=LLMClient(transport, "fake/model"), cache=PlanCache())

    def test_a_writing_task_gets_its_generation_step_back(self):
        planner = self.planner(self.DECISIONS_ONLY)
        plan, _ = planner.plan(Task(prompt="Triage this and draft a reply if warranted.",
                                    state="a ticket"))
        self.assertEqual(plan.answer_from, "generation")
        self.assertIsNotNone(plan.generation)
        self.assertIn("draft a reply", plan.generation.instruction)

    def test_the_gate_survives_the_repair(self):
        planner = self.planner(self.DECISIONS_ONLY)
        plan, _ = planner.plan(Task(prompt="Triage this and draft a reply if warranted.",
                                    state="a ticket"))
        self.assertEqual(len(plan.gate.skip_generation_when), 1)
        self.assertEqual(plan.gate.skip_generation_when[0].decision, "needs_reply")

    def test_chinese_writing_tasks_are_repaired_too(self):
        planner = self.planner(self.DECISIONS_ONLY)
        plan, _ = planner.plan(Task(prompt="判断这条评论要不要回复，需要就起草一条。", state="x"))
        self.assertEqual(plan.answer_from, "generation")

    def test_a_pure_classification_plan_is_left_alone(self):
        planner = self.planner(self.DECISIONS_ONLY)
        plan, _ = planner.plan(Task(prompt="Decide whether this is spam or not spam or unclear.",
                                    state="x"))
        self.assertEqual(plan.answer_from, "decisions")
        self.assertIsNone(plan.generation)


class TestGateRepair(unittest.TestCase):
    """A gate belongs on a conditional task, and nowhere else."""

    GATED = """{
      "strategy": "check then write",
      "answer_from": "generation",
      "decisions": {"is_known": {"type": "noul", "instructions": "Is the subject known?"}},
      "gate": {"skip_generation_when": [{"decision": "is_known", "op": "no"}]},
      "generation": {"instruction": "Explain it."}
    }"""

    def planner(self, *completions):
        return Planner(llm=LLMClient(FakeTransport(completions=list(completions)), "fake/model"),
                       cache=PlanCache())

    def test_an_unconditional_task_loses_its_gate(self):
        plan, _ = self.planner(self.GATED).plan(
            Task(prompt="Explain what this is in three sentences.", state="x"))
        self.assertEqual(plan.gate.skip_generation_when, ())
        self.assertIsNotNone(plan.generation)

    def test_a_conditional_task_keeps_its_gate(self):
        plan, _ = self.planner(self.GATED).plan(
            Task(prompt="Explain it, but only if the subject is known.", state="x"))
        self.assertEqual(len(plan.gate.skip_generation_when), 1)

    def test_a_chinese_conditional_task_keeps_its_gate(self):
        plan, _ = self.planner(self.GATED).plan(
            Task(prompt="判断一下，需要的话再写一段说明。", state="x"))
        self.assertEqual(len(plan.gate.skip_generation_when), 1)


class TestCacheKey(unittest.TestCase):
    """vNext B02. The old key was a sorted set of content words: it dropped
    numbers and order, so "3 cases" and "30 cases" shared a plan, and so did
    the two directions of a translation. The key is now conservative: the
    whole normalised instruction plus the versions of whatever shapes the plan.
    These replace the earlier tests that asserted rewordings collide."""

    def key(self, prompt, **kw):
        from jevharness.planner import plan_cache_key
        return plan_cache_key(Task(prompt=prompt, **kw), language="en")

    def test_an_exact_repeat_hits(self):
        self.assertEqual(self.key("Summarise this email"), self.key("summarise  this email."))

    def test_numbers_are_part_of_the_task(self):
        self.assertNotEqual(self.key("Find 3 case studies on churn"),
                            self.key("Find 30 case studies on churn"))

    def test_direction_is_part_of_the_task(self):
        self.assertNotEqual(self.key("Translate from English to Chinese"),
                            self.key("Translate from Chinese to English"))
        self.assertNotEqual(self.key("把中文翻译成英文"), self.key("把英文翻译成中文"))

    def test_negation_is_part_of_the_task(self):
        self.assertNotEqual(self.key("Reply if it is urgent"), self.key("Reply if it is not urgent"))

    def test_the_skill_text_is_part_of_the_key(self):
        from types import SimpleNamespace
        from jevharness.planner import plan_cache_key

        task = Task(prompt="review this")
        a = plan_cache_key(task, language="en", skill=SimpleNamespace(id="s", body="v1"))
        b = plan_cache_key(task, language="en", skill=SimpleNamespace(id="s", body="v2"))
        self.assertNotEqual(a, b, "editing a skill invalidates the plans built on it")

    def test_declared_questions_separate_the_keys(self):
        from jevharness.questions import Noul

        self.assertNotEqual(self.key("triage"),
                            self.key("triage", questions={"a": Noul(instructions="q")}))

    def test_an_old_cache_file_is_discarded_not_reused(self):
        import json
        import tempfile
        from pathlib import Path

        workspace = Path(tempfile.mkdtemp())
        (workspace / ".jevia").mkdir()
        (workspace / ".jevia" / "plans.json").write_text(json.dumps(
            {"en:-:abc": {"strategy": "old", "answer_from": "generation",
                          "generation": {"instruction": "x"}}}))
        cache = PlanCache(workspace=workspace)
        self.assertEqual(cache.stats()["size"], 0)
        self.assertEqual(cache.stats()["discarded"], 1)


class TestCachePersistence(unittest.TestCase):
    def test_a_compiled_plan_survives_a_restart(self):
        import tempfile
        from pathlib import Path

        workspace = Path(tempfile.mkdtemp())
        first = PlanCache(workspace=workspace)
        transport = FakeTransport(completions=[PLAN_JSON])
        planner = Planner(llm=LLMClient(transport, "m"), cache=first)
        task = Task(prompt="Handle this and reply if warranted.", state="one")
        planner.plan(task)
        self.assertEqual(len(transport.chat_calls), 1)

        second = PlanCache(workspace=workspace)
        again = Planner(llm=LLMClient(transport, "m"), cache=second)
        # vNext B02: the same instruction (case, spacing and final full stop
        # aside) with different material hits; a rewording no longer does.
        plan, call = again.plan(Task(prompt="handle this  and reply if warranted", state="two"))
        self.assertEqual(plan.source, SOURCE_CACHED)
        self.assertIsNone(call, "a restart must not re-buy the plan")
        self.assertEqual(len(transport.chat_calls), 1)
        self.assertEqual(second.stats()["rate"], 1.0)


class TestTinyDeliverables(unittest.TestCase):
    STEPPED = """{
      "strategy": "over-planned",
      "answer_from": "generation",
      "steps": ["facts", "draft", "polish"],
      "decisions": {},
      "generation": {"instruction": "Write it."}
    }"""

    def plan(self, prompt):
        planner = Planner(llm=LLMClient(FakeTransport(completions=[self.STEPPED]), "m"),
                          cache=PlanCache())
        return planner.plan(Task(prompt=prompt, state="some notes"))[0]

    def test_a_one_sentence_task_is_not_split(self):
        for prompt in ("Based on these notes, if relevant, write one sentence about it.",
                       "如果需要的话，根据这些笔记写一句话。",
                       "Give me a single headline for this, only if it is newsworthy."):
            self.assertEqual(self.plan(prompt).steps, (), prompt)

    def test_a_report_keeps_its_steps(self):
        plan = self.plan("Write a report on this, and only include the risks if relevant.")
        self.assertEqual(len(plan.steps), 3)
