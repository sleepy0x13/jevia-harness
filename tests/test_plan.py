import unittest

from jevharness.errors import SchemaError
from jevharness.plan import Condition, Gate, Plan, Task
from jevharness.questions import parse_answer
from tests.fakes import choice, noul, score


def answers(**raw):
    return {name: parse_answer(name, spec) for name, spec in raw.items()}


class TestCondition(unittest.TestCase):
    def test_yes_no_reads_a_noul(self):
        a = answers(d=noul(0.9))
        self.assertTrue(Condition("d", "yes").holds(a))
        self.assertFalse(Condition("d", "no").holds(a))

    def test_yes_no_also_reads_a_two_option_choice(self):
        """A planner may express a yes/no as a choice; the gate must still fire."""
        a = answers(d=choice("no", {"no": 1.0, "yes": 0.0}, 1.0))
        self.assertTrue(Condition("d", "no").holds(a))
        self.assertFalse(Condition("d", "yes").holds(a))

    def test_yes_no_ignores_an_unrelated_choice(self):
        a = answers(d=choice("billing", {"billing": 1.0, "sales": 0.0}, 1.0))
        self.assertFalse(Condition("d", "yes").holds(a))
        self.assertFalse(Condition("d", "no").holds(a))

    def test_is_compares_choice_values(self):
        a = answers(d=choice("billing", {"billing": 0.9, "sales": 0.1}, 0.9))
        self.assertTrue(Condition("d", "is", "billing").holds(a))
        self.assertFalse(Condition("d", "is", "sales").holds(a))

    def test_numeric_ops_apply_to_scores_only(self):
        a = answers(d=score(1.8, 3, 0.9))
        self.assertTrue(Condition("d", "at_least", 1.5).holds(a))
        self.assertFalse(Condition("d", "below", 1.5).holds(a))
        self.assertFalse(Condition("d", "at_least", 1.5).holds(answers(d=noul(0.9))))

    def test_missing_decision_never_holds(self):
        self.assertFalse(Condition("absent", "yes").holds(answers(d=noul(1.0))))

    def test_validation(self):
        for cond in (Condition("", "yes"), Condition("d", "maybe"),
                     Condition("d", "is", 5), Condition("d", "at_least", "high")):
            with self.assertRaises(SchemaError):
                cond.validate()


class TestGateAndPlan(unittest.TestCase):
    def base(self):
        return {
            "strategy": "triage",
            "answer_from": "generation",
            "decisions": {"needs_reply": {"type": "noul", "instructions": "reply needed?"}},
            "gate": {"skip_generation_when": [{"decision": "needs_reply", "op": "no"}]},
            "generation": {"instruction": "draft it"},
        }

    def test_gate_fires_and_explains_itself(self):
        plan = Plan.from_dict(self.base())
        hit = plan.gate.should_skip_generation(answers(needs_reply=noul(0.05)))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.describe(), "needs_reply is no")
        self.assertIsNone(plan.gate.should_skip_generation(answers(needs_reply=noul(0.95))))

    def test_round_trip_is_lossless(self):
        plan = Plan.from_dict(self.base())
        again = Plan.from_dict(plan.to_dict())
        self.assertEqual(plan.to_dict(), again.to_dict())

    def test_gate_cannot_reference_an_unknown_decision(self):
        raw = self.base()
        raw["gate"]["skip_generation_when"][0]["decision"] = "ghost"
        with self.assertRaises(SchemaError):
            Plan.from_dict(raw)

    def test_decisions_plan_must_have_decisions(self):
        with self.assertRaises(SchemaError):
            Plan.from_dict({"strategy": "x", "answer_from": "decisions", "decisions": {}})

    def test_generation_plan_must_have_a_generation_step(self):
        with self.assertRaises(SchemaError):
            Plan.from_dict({"strategy": "x", "answer_from": "generation",
                            "decisions": {"a": {"type": "noul", "instructions": "q"}}})

    def test_reserved_decision_names_are_refused(self):
        raw = self.base()
        raw["decisions"] = {"__capability__": {"type": "noul", "instructions": "q"}}
        raw["gate"] = {}
        with self.assertRaises(SchemaError):
            Plan.from_dict(raw)


class TestTask(unittest.TestCase):
    def test_shape_key_ignores_digits_and_case_and_state(self):
        a = Task(prompt="Route ticket 41", state="one")
        b = Task(prompt="route TICKET 9999", state="something else entirely")
        self.assertEqual(a.shape_key(), b.shape_key())

    def test_shape_key_separates_different_prompts(self):
        self.assertNotEqual(
            Task(prompt="route this").shape_key(),
            Task(prompt="summarise this").shape_key(),
        )

    def test_state_is_serialised_for_non_strings(self):
        self.assertIn('"a": 1', Task(prompt="p", state={"a": 1}).state_text)

    def test_empty_prompt_is_refused(self):
        with self.assertRaises(SchemaError):
            Task(prompt="   ").validate()


if __name__ == "__main__":
    unittest.main()
