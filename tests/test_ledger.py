import unittest

from jevharness.ledger import Comparison, Ledger, compare
from jevharness.providers import Call, Usage
from jevharness.roster import Credential, ModelSpec, Roster


def comparison(baseline, actual, plan_cost=0.0, plan_reused=False):
    return Comparison(
        baseline_model="big",
        baseline_cost=baseline,
        actual_cost=actual,
        plan_cost=plan_cost,
        decisions_answered=3,
        jev_calls=1,
        llm_calls=1,
        llm_calls_avoided=3,
        generation_skipped=False,
        plan_reused=plan_reused,
    )


class TestVerdict(unittest.TestCase):
    def test_clearly_cheaper(self):
        self.assertEqual(comparison(1.0, 0.1).verdict, "cheaper")

    def test_clearly_dearer(self):
        self.assertEqual(comparison(0.1, 1.0).verdict, "dearer")

    def test_within_five_percent_is_even(self):
        self.assertEqual(comparison(1.0, 1.0).verdict, "even")
        self.assertEqual(comparison(1.02, 1.0).verdict, "even")

    def test_free_runs_count_as_cheaper(self):
        self.assertEqual(comparison(1.0, 0.0).verdict, "cheaper")
        self.assertIsNone(comparison(1.0, 0.0).ratio)

    def test_a_dearer_first_run_names_the_planning_call(self):
        note = comparison(0.001, 0.010, plan_cost=0.009).note()
        self.assertIn("compiling the plan", note)
        self.assertIn("now cached", note)

    def test_steady_state_strips_the_planning_cost(self):
        self.assertAlmostEqual(
            comparison(0.001, 0.010, plan_cost=0.009).steady_state_cost, 0.001
        )

    def test_saving_may_be_negative_and_says_so(self):
        self.assertLess(comparison(0.001, 0.010).saved, 0)


class TestCompare(unittest.TestCase):
    def roster(self):
        r = Roster(
            models=[
                ModelSpec(id="cheap", model="v/cheap", capability=1, price_out=0.01),
                ModelSpec(id="best", model="v/best", capability=4, price_out=2.00),
            ],
            credentials={"default": Credential("default", "sk-fake-000000000000")},
        )
        r.validate()
        return r

    def test_baseline_uses_the_strongest_model(self):
        ledger = Ledger()
        ledger.add(Call("jev", "decide", "jev", 300, Usage(1000, 50, 0.00002)))
        result = compare(ledger, self.roster(), decisions_answered=4,
                         generation_skipped=True, plan_reused=True)
        self.assertEqual(result.baseline_model, "v/best")
        # 4 decisions x 120 tokens + 50 measured output, at $2/Mtok out.
        self.assertAlmostEqual(result.baseline_cost, (4 * 120 + 50) / 1e6 * 2.0, places=9)

    def test_avoided_calls_count_one_llm_call_per_decision(self):
        ledger = Ledger()
        ledger.add(Call("jev", "decide", "jev", 300, Usage(100, 20, 0.00002)))
        ledger.add(Call("llm", "generate", "m", 900, Usage(200, 80, 0.0001)))
        result = compare(ledger, self.roster(), decisions_answered=5,
                         generation_skipped=False, plan_reused=True)
        self.assertEqual(result.llm_calls, 1)
        self.assertEqual(result.llm_calls_avoided, 5)

    def test_no_models_yields_a_zero_baseline_not_a_crash(self):
        empty = Roster(credentials={"default": Credential("default", "sk-fake-0000000000")})
        result = compare(Ledger(), empty, decisions_answered=0,
                         generation_skipped=True, plan_reused=False)
        self.assertEqual(result.baseline_cost, 0.0)
        self.assertIsNone(result.baseline_model)


class TestLedgerTotals(unittest.TestCase):
    def test_groups_by_engine(self):
        ledger = Ledger()
        ledger.add(Call("jev", "decide", "j", 100, Usage(10, 1, 0.1)))
        ledger.add(Call("llm", "compile", "m", 200, Usage(20, 2, 0.2)))
        ledger.add(Call("llm", "generate", "m", 300, Usage(30, 3, 0.3)))
        engines = ledger.by_engine()
        self.assertEqual(engines["llm"]["calls"], 2)
        self.assertAlmostEqual(engines["llm"]["usage"]["cost"], 0.5)
        self.assertEqual(ledger.total_latency_ms, 600)
        self.assertEqual(ledger.input_tokens(), 60)


if __name__ == "__main__":
    unittest.main()
