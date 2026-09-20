import unittest

from jevharness.errors import ConfigError, SchemaError
from jevharness.questions import parse_answer
from jevharness.roster import (
    CAPABILITY_LEVELS,
    Credential,
    ModelSpec,
    Roster,
    default_models,
)
from tests.fakes import score

LEVELS = len(CAPABILITY_LEVELS)


def cap(value, confidence):
    return parse_answer("c", score(value, LEVELS, confidence))


def roster(*models, round_up=0.5):
    r = Roster(
        models=list(models),
        credentials={"default": Credential(ref="default", api_key="sk-fake-000000000000")},
    )
    r.round_up_below_certainty = round_up
    r.validate()
    return r


def model(mid, capability, price_out, price_in=0.0):
    return ModelSpec(id=mid, model=f"vendor/{mid}", capability=capability,
                     price_in=price_in, price_out=price_out, label=mid)


class TestSelection(unittest.TestCase):
    def setUp(self):
        self.r = roster(
            model("tiny", 1, 0.01),
            model("mid", 2, 0.10),
            model("big", 4, 1.00),
        )

    def test_level_zero_retires_the_llm(self):
        sel = self.r.select(cap(0.0, 0.95))
        self.assertTrue(sel.jev_only)
        self.assertIsNone(sel.model)

    def test_picks_the_cheapest_model_that_clears_the_rung(self):
        self.assertEqual(self.r.select(cap(1.0, 0.9)).model.id, "tiny")
        self.assertEqual(self.r.select(cap(2.0, 0.9)).model.id, "mid")
        self.assertEqual(self.r.select(cap(3.0, 0.9)).model.id, "big")

    def test_never_picks_a_cheaper_underpowered_model(self):
        sel = self.r.select(cap(4.0, 0.9))
        self.assertEqual(sel.model.id, "big")
        self.assertIn("tiny", sel.cheaper_rejected)

    def test_low_certainty_rounds_up_a_rung(self):
        sure = self.r.select(cap(1.0, 0.95))
        unsure = self.r.select(cap(1.0, 0.2))
        self.assertEqual(sure.model.id, "tiny")
        self.assertEqual(unsure.model.id, "mid")
        self.assertTrue(unsure.rounded_up)

    def test_low_certainty_at_level_zero_does_not_skip_the_llm(self):
        """Wrongly skipping generation is unrecoverable, so it fails safe."""
        sel = self.r.select(cap(0.2, 0.1))
        self.assertFalse(sel.jev_only)
        self.assertTrue(sel.rounded_up)

    def test_no_answer_generates_on_the_cheapest_model(self):
        sel = self.r.select(None)
        self.assertFalse(sel.jev_only)
        self.assertEqual(sel.model.id, "tiny")

    # vNext B03 / T10: this used to fall back to the strongest model and
    # lower ``required`` to match it. The requirement now stands, and the
    # step is held unless the user allowed a downgrade.
    def test_nothing_qualifying_is_reported_not_downgraded(self):
        weak = roster(model("tiny", 1, 0.01), model("also", 1, 0.02))
        sel = weak.select(cap(4.0, 0.99))
        self.assertIsNone(sel.model)
        self.assertTrue(sel.blocked)
        self.assertFalse(sel.jev_only)
        self.assertEqual(sel.required, 4)
        self.assertEqual(sel.best_available, 1)
        self.assertFalse(any(c["eligible"] for c in sel.candidates))

    def test_a_permitted_downgrade_is_labelled(self):
        weak = roster(model("tiny", 1, 0.01), model("also", 1, 0.02))
        weak.allow_downgrade = True
        sel = weak.select(cap(4.0, 0.99))
        self.assertEqual(sel.model.id, "tiny")
        self.assertEqual(sel.required, 4, "the requirement is kept")
        self.assertTrue(sel.downgraded)
        self.assertFalse(sel.qualified)

    def test_an_unknown_price_is_not_free(self):
        spec = ModelSpec.from_dict({"id": "x", "model": "v/x", "capability": 2})
        self.assertFalse(spec.priced)
        self.assertFalse(spec.is_free)
        known = ModelSpec.from_dict({"id": "y", "model": "v/y", "capability": 2,
                                     "price_in": 0, "price_out": 0})
        self.assertTrue(known.is_free)

    def test_output_tokens_dominate_the_ranking(self):
        r = roster(model("cheap-out", 2, 0.01, price_in=1.0),
                   model("cheap-in", 2, 1.00, price_in=0.0))
        self.assertEqual(r.select(cap(2.0, 0.9)).model.id, "cheap-out")

    def test_no_models_at_all_is_a_config_error(self):
        empty = Roster(credentials={"default": Credential("default", "sk-fake-000000000000")})
        with self.assertRaises(ConfigError):
            empty.select(cap(2.0, 0.9))


class TestRosterValidation(unittest.TestCase):
    def test_capability_zero_is_reserved(self):
        with self.assertRaises(SchemaError):
            model("x", 0, 0.1).validate()

    def test_reserved_ids_are_refused(self):
        with self.assertRaises(SchemaError):
            ModelSpec(id="__capability__", model="a/b", capability=1).validate()

    def test_duplicate_ids_are_refused(self):
        r = Roster(models=[model("same", 1, 0.1), model("same", 2, 0.2)],
                   credentials={"default": Credential("default", "sk-fake-000000000000")})
        with self.assertRaises(SchemaError):
            r.validate()

    def test_missing_credential_is_refused(self):
        r = Roster(models=[model("a", 1, 0.1)], credentials={})
        with self.assertRaises(ConfigError):
            r.validate()

    def test_blank_credentials_are_dropped_not_accepted(self):
        r = Roster.from_dict({"credentials": [
            {"ref": "default", "api_key": ""},
            {"ref": "real", "api_key": "sk-or-v1-abcdefghijkl"},
        ], "models": []})
        self.assertEqual(list(r.credentials), ["real"])

    def test_disabled_models_are_not_selectable(self):
        r = Roster(models=[model("on", 2, 0.5),
                           ModelSpec(id="off", model="v/off", capability=1,
                                     price_out=0.001, enabled=False)],
                   credentials={"default": Credential("default", "sk-fake-000000000000")})
        r.validate()
        self.assertEqual([m.id for m in r.available()], ["on"])

    def test_defaults_are_internally_consistent(self):
        r = Roster(models=default_models(),
                   credentials={"default": Credential("default", "sk-fake-000000000000")})
        r.validate()
        self.assertTrue(r.needs_capability_question())


if __name__ == "__main__":
    unittest.main()


class TestRoundUpRelaxation(unittest.TestCase):
    """The low-certainty round-up is a margin, not the task's requirement: when
    no model clears it, the margin goes rather than the run. A level Jev really
    asked for is never given up this way."""

    def test_the_margin_is_given_up_before_the_run_is(self):
        r = roster(model("a", 2, 0.01), model("b", 3, 0.02))
        sel = r.select(cap(3.0, 0.2))           # 3, raised to 4 on low certainty
        self.assertTrue(sel.rounded_up)
        self.assertTrue(sel.relaxed)
        self.assertEqual(sel.required, 3)
        self.assertEqual(sel.model.id, "b")
        self.assertTrue(sel.qualified)

    def test_a_level_jev_asked_for_is_not_given_up(self):
        r = roster(model("a", 2, 0.01), model("b", 3, 0.02))
        sel = r.select(cap(4.0, 0.95))
        self.assertTrue(sel.blocked)
        self.assertFalse(sel.relaxed)


class TestQuality(unittest.TestCase):
    """The dial between the cheapest model that clears the rung and the best one."""

    def roster(self, quality):
        r = roster(model("tiny", 1, 0.01), model("mid", 2, 0.10), model("big", 4, 1.00))
        r.quality = quality
        return r

    def test_thrift_buys_the_cheapest_that_clears_it(self):
        self.assertEqual(self.roster("thrift").select(cap(2.0, 0.9)).model.id, "mid")

    def test_best_buys_the_strongest_and_keeps_the_requirement(self):
        selection = self.roster("best").select(cap(2.0, 0.9))
        self.assertEqual(selection.model.id, "big")
        self.assertEqual(selection.required, 2, "what the work needs is unchanged")
        self.assertTrue(selection.qualified)

    def test_best_still_writes_nothing_when_jev_says_nothing_is_owed(self):
        self.assertTrue(self.roster("best").select(cap(0.0, 0.95)).jev_only)
