import unittest

from jevharness.config import Thresholds
from jevharness.policy import (
    escalation_messages,
    facts_block,
    generation_messages,
    is_uncertain,
    review,
)
from jevharness.questions import Noul, parse_answer
from tests.fakes import choice, noul, score


def answers(**raw):
    return {name: parse_answer(name, spec) for name, spec in raw.items()}


class TestUncertainty(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_noul_band_is_inclusive(self):
        self.assertTrue(is_uncertain(parse_answer("d", noul(0.35)), self.t))
        self.assertTrue(is_uncertain(parse_answer("d", noul(0.65)), self.t))
        self.assertFalse(is_uncertain(parse_answer("d", noul(0.34)), self.t))
        self.assertFalse(is_uncertain(parse_answer("d", noul(0.66)), self.t))

    def test_choice_and_score_use_confidence(self):
        self.assertTrue(is_uncertain(parse_answer("d", choice("a", {"a": 0.5, "b": 0.5}, 0.5)), self.t))
        self.assertFalse(is_uncertain(parse_answer("d", choice("a", {"a": 0.9, "b": 0.1}, 0.9)), self.t))
        self.assertTrue(is_uncertain(parse_answer("d", score(1.0, 3, 0.4)), self.t))

    def test_review_splits_cleanly(self):
        result = review(answers(good=noul(0.95), bad=noul(0.5)), self.t)
        self.assertEqual(sorted(result.accepted), ["good"])
        self.assertEqual(sorted(result.uncertain), ["bad"])
        self.assertFalse(result.clean)

    def test_all_confident_is_clean(self):
        self.assertTrue(review(answers(a=noul(0.99), b=noul(0.01)), self.t).clean)

    def test_thresholds_are_configurable(self):
        strict = Thresholds(noul_uncertain_low=0.1, noul_uncertain_high=0.9)
        self.assertTrue(is_uncertain(parse_answer("d", noul(0.8)), strict))


class TestPrompts(unittest.TestCase):
    def test_facts_block_is_sorted_and_marks_uncertainty(self):
        a = answers(zulu=noul(0.9), alpha=noul(0.1))
        block = facts_block(a, mark_uncertain={"zulu": a["zulu"]})
        self.assertTrue(block.index("alpha") < block.index("zulu"))
        self.assertIn("[uncertain", block)
        self.assertEqual(block.count("[uncertain"), 1)

    def test_generation_prompt_carries_the_decisions_as_given(self):
        messages = generation_messages(
            "Draft a reply.", "the material", answers(topic=noul(0.9)),
            review(answers(topic=noul(0.9)), Thresholds()),
        )
        user = messages[1]["content"]
        self.assertIn("ESTABLISHED", user)
        self.assertIn("topic", user)
        self.assertIn("the material", user)
        self.assertIn("do not second-guess", messages[0]["content"])
        self.assertIn("do not re-derive", user)

    def test_generation_prompt_can_omit_the_state(self):
        messages = generation_messages(
            "Draft a reply.", "the material", {}, review({}, Thresholds()),
            include_state=False,
        )
        self.assertNotIn("the material", messages[1]["content"])

    def test_escalation_asks_only_about_the_unresolved(self):
        a = answers(sure=noul(0.99), shaky=noul(0.5))
        result = review(a, Thresholds())
        messages = escalation_messages(
            "triage", "material", result, {"shaky": Noul(instructions="is it shaky?")}
        )
        user = messages[1]["content"]
        self.assertIn("is it shaky?", user)
        self.assertIn("ALREADY SETTLED", user)
        self.assertIn("sure", user.split("ALREADY SETTLED")[1])


if __name__ == "__main__":
    unittest.main()
