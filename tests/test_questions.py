import unittest

from jevharness.errors import SchemaError
from jevharness.questions import (
    Choice,
    Noul,
    Score,
    parse_answer,
    question_from_dict,
    questions_to_payload,
)


class TestQuestions(unittest.TestCase):
    def test_choice_payload_matches_the_api(self):
        q = Choice(instructions=" which team ", options={"a": "first", "b": ""})
        self.assertEqual(
            q.to_payload(),
            {"type": "choice", "instructions": "which team",
             "criteria": {"a": "first", "b": None}},
        )

    def test_score_payload_is_an_ordered_list(self):
        q = Score(instructions="severity", levels=["low", "mid", "high"])
        self.assertEqual(q.to_payload()["criteria"], ["low", "mid", "high"])

    def test_noul_omits_empty_criteria(self):
        self.assertNotIn("criteria", Noul(instructions="is it?").to_payload())
        self.assertIn("criteria", Noul(instructions="is it?", criteria={"true": "x"}).to_payload())

    def test_validation_rejects_degenerate_questions(self):
        for spec in (
            {"type": "choice", "instructions": "q", "criteria": {"only": "one"}},
            {"type": "score", "instructions": "q", "criteria": ["single"]},
            {"type": "choice", "instructions": "", "criteria": {"a": "1", "b": "2"}},
            {"type": "score", "instructions": "q", "criteria": "not a list"},
            {"type": "elephant", "instructions": "q"},
        ):
            with self.assertRaises(SchemaError):
                question_from_dict("d", spec)

    def test_questions_to_payload_requires_at_least_one(self):
        with self.assertRaises(SchemaError):
            questions_to_payload({})


class TestAnswers(unittest.TestCase):
    def test_noul_certainty_is_distance_from_a_coin_flip(self):
        self.assertAlmostEqual(parse_answer("d", {"type": "noul", "noul": 0.5}).certainty, 0.0)
        self.assertAlmostEqual(parse_answer("d", {"type": "noul", "noul": 1.0}).certainty, 1.0)
        self.assertAlmostEqual(parse_answer("d", {"type": "noul", "noul": 0.0}).certainty, 1.0)
        self.assertAlmostEqual(parse_answer("d", {"type": "noul", "noul": 0.75}).certainty, 0.5)

    def test_noul_value_is_a_bool(self):
        self.assertTrue(parse_answer("d", {"type": "noul", "noul": 0.51}).value)
        self.assertFalse(parse_answer("d", {"type": "noul", "noul": 0.49}).value)

    def test_score_level_and_normalisation(self):
        a = parse_answer("d", {"type": "score", "score": 1.4,
                               "legend": {"0": "a", "1": "b", "2": "c"},
                               "probabilities": {}, "confidence": 0.8})
        self.assertEqual(a.level, 1)
        self.assertAlmostEqual(a.normalized, 0.7)
        self.assertEqual(a.certainty, 0.8)

    def test_malformed_answers_raise(self):
        for raw in ({"type": "choice"}, {"type": "score"}, {"type": "noul"},
                    {"type": "what"}, "not an object"):
            with self.assertRaises(SchemaError):
                parse_answer("d", raw)


if __name__ == "__main__":
    unittest.main()
