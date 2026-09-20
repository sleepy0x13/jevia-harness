import unittest

from jevharness import vendors as V
from jevharness.providers import LLMClient, Usage, estimate_tokens, price_usage
from jevharness.roster import Credential, ModelSpec
from tests.fakes import FakeTransport

ROWS = [
    {"model": "openai/gpt-6-astra", "label": "GPT-6 Astra", "price_in": 10.0, "price_out": 50.0},
    {"model": "anthropic/claude-opus-5", "label": "Claude Opus 5", "price_in": 5.0, "price_out": 25.0},
    {"model": "anthropic/claude-opus-5:batch", "label": "batch", "price_in": 2.5, "price_out": 12.5},
    {"model": "z-ai/glm-5.3", "label": "GLM 5.3", "price_in": 0.91, "price_out": 2.86},
]


class TestPricing(unittest.TestCase):
    def test_exact_ids_match(self):
        self.assertEqual(V.price_for(V.get("openai"), "gpt-6-astra", ROWS)["price_in"], 10.0)

    def test_dated_and_differently_spelled_ids_match(self):
        self.assertEqual(V.price_for(V.get("anthropic"), "claude-opus-5-20260801", ROWS)["price_out"], 25.0)
        self.assertEqual(V.price_for(V.get("zhipu"), "GLM-5.3", ROWS)["price_in"], 0.91)

    def test_batch_variants_are_never_used_as_the_price(self):
        hit = V.price_for(V.get("anthropic"), "claude-opus-5", ROWS)
        self.assertEqual(hit["model"], "anthropic/claude-opus-5")

    def test_another_vendors_model_is_not_matched(self):
        self.assertIsNone(V.price_for(V.get("openai"), "claude-opus-5", ROWS))

    def test_a_custom_vendor_is_never_priced_by_guess(self):
        self.assertIsNone(V.price_for(V.get("custom"), "gpt-6-astra", ROWS))


class TestBaseUrls(unittest.TestCase):
    def test_a_pasted_chat_url_is_reduced_to_its_root(self):
        self.assertEqual(V.normalise_base(V.get("custom"), "https://x.test/v1/chat/completions/"),
                         "https://x.test/v1")

    def test_the_legacy_openrouter_root_is_upgraded(self):
        self.assertEqual(V.normalise_base(V.get("openrouter"), "https://openrouter.ai/api"),
                         "https://openrouter.ai/api/v1")

    def test_a_blank_url_takes_the_vendor_default(self):
        self.assertEqual(V.normalise_base(V.get("deepseek"), ""), "https://api.deepseek.com/v1")

    def test_who_can_chat_is_decided_by_the_credential(self):
        self.assertTrue(V.can_chat(Credential("a", "k", base_url="https://x.test/v1", vendor="custom")))
        self.assertFalse(V.can_chat(Credential("a", "k", base_url="", vendor="custom")))
        self.assertFalse(V.can_chat(Credential("a", "k", base_url="https://x", vendor="typesafe")))

    def test_only_openrouter_and_typesafe_serve_jev(self):
        serving = sorted(v.id for v in V.VENDORS if v.serves_jev)
        self.assertEqual(serving, ["openrouter", "typesafe"])


class TestDialects(unittest.TestCase):
    def body(self, dialect, stream_usage, stream):
        transport = FakeTransport(completions=["hi"])
        client = LLMClient(transport, "m", dialect=dialect, stream_usage=stream_usage)
        if stream:
            list(client.stream([{"role": "user", "content": "x"}]))
        else:
            client.complete([{"role": "user", "content": "x"}])
        return transport.chat_calls[0]

    def test_openrouter_gets_its_own_switches(self):
        body = self.body("openrouter", True, stream=True)
        self.assertEqual(body["reasoning"], {"enabled": False})
        self.assertEqual(body["usage"], {"include": True})

    def test_a_plain_vendor_gets_no_unknown_fields(self):
        body = self.body("openai", False, stream=True)
        self.assertNotIn("reasoning", body)
        self.assertNotIn("usage", body)
        self.assertNotIn("stream_options", body)

    def test_a_vendor_that_reports_stream_usage_is_asked_to(self):
        self.assertEqual(self.body("openai", True, stream=True)["stream_options"],
                         {"include_usage": True})

    def test_anthropic_keys_are_sent_both_ways(self):
        headers = V.get("anthropic").headers("sk-ant-x")
        self.assertEqual(headers["x-api-key"], "sk-ant-x")
        self.assertEqual(headers["Authorization"], "Bearer sk-ant-x")


class TestCosting(unittest.TestCase):
    def test_list_prices_fill_a_missing_cost(self):
        usage = price_usage(Usage(1_000_000, 1_000_000, 0.0), (1.0, 2.0))
        self.assertAlmostEqual(usage.cost, 3.0)

    def test_a_reported_cost_is_never_overwritten(self):
        self.assertEqual(price_usage(Usage(10, 10, 0.5), (1.0, 2.0)).cost, 0.5)

    def test_missing_usage_is_estimated_and_says_so(self):
        transport = FakeTransport(completions=["an answer"])
        transport.post_json = lambda path, payload: {
            "choices": [{"message": {"content": "an answer"}}]}
        client = LLMClient(transport, "m", dialect="openai", price=(1.0, 1.0))
        _, call = client.complete([{"role": "user", "content": "a question"}])
        self.assertTrue(call.usage.estimated)
        self.assertGreater(call.usage.cost, 0)

    def test_cjk_counts_more_tokens_per_character(self):
        self.assertGreater(estimate_tokens("你好世界你好世界"), estimate_tokens("abcdefgh"))

    def test_an_unpriced_model_never_wins_cheapest(self):
        free = ModelSpec(id="a", model="v/a", capability=2, price_in=0, price_out=0)
        unknown = ModelSpec(id="b", model="v/b", capability=2, priced=False)
        self.assertLess(free.blended_price, unknown.blended_price)
        self.assertFalse(unknown.is_free)


if __name__ == "__main__":
    unittest.main()
