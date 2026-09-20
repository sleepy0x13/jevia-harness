import unittest

from jevharness.config import Config, Thresholds
from jevharness.errors import ConfigError, SchemaError
from jevharness.harness import Harness, Settings, parse_task

KEY = "sk-or-v1-abcdefghijklmnop"


def env_config(key=KEY):
    cfg = Config(api_key=key)
    return cfg


class TestSettingsFromRequest(unittest.TestCase):
    def payload(self, **over):
        base = {
            "roster": {
                "credentials": [{"ref": "mine", "api_key": KEY}],
                "models": [{"id": "m", "model": "v/m", "capability": 2,
                            "price_in": 0.01, "price_out": 0.02, "credential_ref": "mine"}],
            }
        }
        base.update(over)
        return base

    def test_the_callers_own_key_is_used(self):
        settings = Settings.from_request(self.payload())
        self.assertEqual(list(settings.roster.credentials), ["mine"])
        self.assertEqual(settings.jev_credential_ref, "mine")

    def test_blank_credential_falls_back_to_env(self):
        """The browser sends an empty row before a key is typed."""
        settings = Settings.from_request(
            {"roster": {"credentials": [{"ref": "default", "api_key": ""}], "models": []}},
            fallback=env_config(),
        )
        self.assertEqual(settings.roster.credentials["default"].api_key, KEY)
        self.assertTrue(settings.roster.models, "a default roster is supplied")

    def test_no_key_anywhere_is_a_clear_error(self):
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_request({"roster": {"credentials": []}}, fallback=Config())
        self.assertIn("no API key", str(ctx.exception))

    def test_a_model_pointing_at_a_dropped_credential_is_repaired(self):
        settings = Settings.from_request({
            "roster": {
                "credentials": [{"ref": "real", "api_key": KEY}],
                "models": [{"id": "m", "model": "v/m", "capability": 2,
                            "credential_ref": "was-left-blank"}],
            }
        })
        self.assertEqual(settings.roster.models[0].credential_ref, "real")

    def test_jev_credential_falls_back_when_the_named_one_is_gone(self):
        settings = Settings.from_request(self.payload(jev_credential_ref="ghost"))
        self.assertEqual(settings.jev_credential_ref, "mine")

    def test_thresholds_are_taken_from_the_request_and_validated(self):
        settings = Settings.from_request(
            self.payload(thresholds={"decision_abstain_below": 0.8,
                                     "capability_round_up_below": 0.3})
        )
        self.assertEqual(settings.thresholds.decision_abstain_below, 0.8)
        self.assertEqual(settings.roster.round_up_below_certainty, 0.3)

    def test_nonsense_thresholds_are_refused(self):
        with self.assertRaises(SchemaError):
            Settings.from_request(self.payload(thresholds={"noul_uncertain_low": "very"}))
        with self.assertRaises(ValueError):
            Settings.from_request(self.payload(thresholds={"noul_uncertain_low": 0.9,
                                                           "noul_uncertain_high": 0.1}))

    def test_unknown_threshold_keys_are_ignored_not_fatal(self):
        settings = Settings.from_request(self.payload(thresholds={"made_up": 1}))
        self.assertEqual(settings.thresholds, Thresholds())

    def test_keys_are_never_echoed_in_the_public_view(self):
        harness = Harness(Settings.from_request(self.payload()))
        blob = repr(harness.describe())
        self.assertNotIn(KEY, blob)
        self.assertIn("...", blob)


class TestCompilerChoice(unittest.TestCase):
    def harness(self, models):
        return Harness(Settings.from_request({
            "roster": {"credentials": [{"ref": "k", "api_key": KEY}], "models": models}
        }))

    def test_cheapest_model_competent_for_json_is_the_compiler(self):
        harness = self.harness([
            {"id": "weak", "model": "v/weak", "capability": 1, "price_out": 0.001,
             "credential_ref": "k"},
            {"id": "ok", "model": "v/ok", "capability": 2, "price_out": 0.05,
             "credential_ref": "k"},
            {"id": "strong", "model": "v/strong", "capability": 4, "price_out": 5.0,
             "credential_ref": "k"},
        ])
        self.assertEqual(harness.describe()["compiler_model"], "v/ok")

    def test_falls_back_when_nothing_is_competent(self):
        harness = self.harness([
            {"id": "weak", "model": "v/weak", "capability": 1, "price_out": 0.001,
             "credential_ref": "k"},
        ])
        self.assertEqual(harness.describe()["compiler_model"], "v/weak")


class TestParseTask(unittest.TestCase):
    def test_declared_questions_are_typed_on_the_way_in(self):
        task = parse_task({"prompt": "p", "state": "s", "questions": {
            "d": {"type": "choice", "instructions": "q", "criteria": {"a": "1", "b": "2"}}
        }})
        self.assertEqual(task.questions["d"].kind, "choice")

    def test_a_bad_question_is_rejected_before_any_call(self):
        with self.assertRaises(SchemaError):
            parse_task({"prompt": "p", "questions": {"d": {"type": "choice"}}})

    def test_missing_prompt_is_rejected(self):
        with self.assertRaises(SchemaError):
            parse_task({"state": "s"})

    def test_non_string_state_is_allowed(self):
        task = parse_task({"prompt": "p", "state": [1, 2, 3]})
        self.assertIn("1", task.state_text)


if __name__ == "__main__":
    unittest.main()
