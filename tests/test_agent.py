import unittest

from jevharness.agent import Evidence, Researcher, assess, route
from jevharness.config import Thresholds
from jevharness.plan import Research
from jevharness.providers import JevClient
from jevharness.research import Hit, Page, Section
from tests.fakes import FakeTransport, choice, noul, score


def sections(n, url="https://e/p", title="T"):
    return [Section(text=f"section {i} of {url} " + "x" * 300, url=url, title=title, index=i)
            for i in range(n)]


def researcher(hook):
    transport = FakeTransport(decision_hook=hook)
    return Researcher(JevClient(transport, "fake/jev"), Thresholds()), transport


class TestHitTriage(unittest.TestCase):
    def hits(self, n=5):
        return [Hit(title=f"t{i}", url=f"https://e/{i}", snippet="s", rank=i) for i in range(n)]

    def test_one_call_scores_every_result(self):
        r, transport = researcher(lambda p: {k: noul(0.9) for k in p["questions"]})
        keep, call = r._pick_hits("q", self.hits(5), top_k=3)
        self.assertEqual(len(transport.decision_calls), 1, "N results, one call")
        self.assertEqual(len(transport.decision_calls[0]["questions"]), 5)
        self.assertEqual(len(keep), 3)
        self.assertIsNotNone(call)

    def test_results_are_ordered_by_probability_not_by_rank(self):
        scores = {"h0": 0.1, "h1": 0.95, "h2": 0.6, "h3": 0.2, "h4": 0.8}
        r, _ = researcher(lambda p: {k: noul(scores[k]) for k in p["questions"]})
        keep, _ = r._pick_hits("q", self.hits(5), top_k=3)
        self.assertEqual([h.rank for h in keep], [1, 4, 2])

    def test_weak_results_are_dropped(self):
        r, _ = researcher(lambda p: {k: noul(0.1) for k in p["questions"]})
        keep, _ = r._pick_hits("q", self.hits(5), top_k=3)
        self.assertEqual(len(keep), 1, "one is kept so a round is never wasted")

    def test_a_jev_failure_falls_back_to_engine_order(self):
        class Broken(FakeTransport):
            def post_json(self, path, payload):
                raise RuntimeError("down")

        r = Researcher(JevClient(Broken(), "fake/jev"), Thresholds())
        keep, call = r._pick_hits("q", self.hits(5), top_k=2)
        self.assertEqual([h.rank for h in keep], [0, 1])
        self.assertIsNone(call)


def kept_of(verdicts, items):
    from jevharness.agent import section_id
    return [s.index for s in items if verdicts[section_id(s)]["status"] == "kept"]


class TestSectionTriage(unittest.TestCase):
    # vNext B01: the sufficiency question no longer rides in the keep batch —
    # answers in one batch are independent, so it would judge material the
    # filter then drops. One call still scores every section.
    def test_one_call_scores_every_section_and_nothing_else(self):
        r, transport = researcher(lambda p: {k: noul(0.9) for k in p["questions"]})
        items = sections(6)
        verdicts, calls = r._pick_sections("q", items)
        asked = transport.decision_calls[0]["questions"]
        self.assertEqual(len(transport.decision_calls), 1)
        self.assertEqual(len(asked), 6)
        self.assertNotIn("__enough__", asked)
        self.assertEqual(len(kept_of(verdicts, items)), 6)
        self.assertEqual(len(calls), 1)

    def test_irrelevant_sections_are_dropped(self):
        scores = {"s0": 0.9, "s1": 0.1, "s2": 0.8, "s3": 0.2}
        r, _ = researcher(
            lambda p: {k: noul(scores.get(k, 0.5)) for k in p["questions"]}
        )
        items = sections(4)
        verdicts, _ = r._pick_sections("q", items)
        self.assertEqual(kept_of(verdicts, items), [0, 2])

    def test_the_state_is_capped(self):
        r, transport = researcher(lambda p: {k: noul(0.9) for k in p["questions"]})
        huge = [Section(text="y" * 5000, url="u", title="t", index=i) for i in range(40)]
        r._pick_sections("q", huge)
        state = transport.decision_calls[0]["state"]
        self.assertLess(len(state), 30_000)


class TestLoop(unittest.TestCase):
    def setUp(self):
        import jevharness.agent as agent

        self.agent = agent
        self._search, self._fetch = agent.search, agent.fetch_all
        agent.search = lambda q, limit=10: [
            Hit(title=f"t{i}", url=f"https://e/{i}", snippet="s", rank=i) for i in range(4)
        ]
        agent.fetch_all = lambda urls, workers=4: [
            Page(url=u, title="T", text="", sections=sections(3, url=u)) for u in urls
        ]

    def tearDown(self):
        self.agent.search, self.agent.fetch_all = self._search, self._fetch

    def test_a_round_costs_three_jev_calls_whatever_the_result_count(self):
        r, transport = researcher(
            lambda p: {k: noul(0.9 if k != "__enough__" else 0.95) for k in p["questions"]}
        )
        evidence, calls = r.run("q", Research(rounds=2, top_k=2))
        self.assertEqual(len(transport.decision_calls), 3, "triage + filter + sufficiency")
        self.assertEqual(len(calls), 3)
        self.assertEqual(evidence.rounds, 1, "it stopped because the delivered set was enough")
        self.assertEqual(evidence.stop_reason, "sufficient")

    def test_a_second_round_runs_when_it_is_not_enough(self):
        self.agent.search = lambda q, limit=10: [
            Hit(title=f"t{i}", url=f"https://e/{q}/{i}", snippet="s", rank=i) for i in range(4)
        ]
        r, transport = researcher(
            lambda p: {k: noul(0.9 if k != "__enough__" else 0.2) for k in p["questions"]}
        )
        evidence, _ = r.run("q", Research(rounds=2, top_k=2),
                            queries=lambda question, ev, n: [f"round{n}"])
        self.assertEqual(evidence.rounds, 2)
        self.assertEqual(len(transport.decision_calls), 6)
        self.assertEqual(evidence.stop_reason, "budget_exhausted")
        self.assertEqual(evidence.queries, ["round0", "round1"])

    def test_the_same_words_are_never_searched_twice(self):
        r, transport = researcher(
            lambda p: {k: noul(0.9 if k != "__enough__" else 0.2) for k in p["questions"]}
        )
        evidence, _ = r.run("q", Research(rounds=3, top_k=2),
                            queries=lambda question, ev, n: ["same words"])
        self.assertEqual(evidence.queries, ["same words"])

    def test_pages_already_read_are_not_fetched_again(self):
        fetched = []
        self.agent.fetch_all = lambda urls, workers=4: fetched.extend(urls) or []
        r, _ = researcher(
            lambda p: {k: noul(0.9 if k != "__enough__" else 0.2) for k in p["questions"]}
        )
        r.run("q", Research(rounds=2, top_k=2), queries=lambda question, ev, n: [f"w{n}"])
        self.assertEqual(len(fetched), len(set(fetched)))

    def test_result_snippets_count_as_evidence(self):
        self.agent.fetch_all = lambda urls, workers=4: []      # every page renders nothing
        r, _ = researcher(
            lambda p: {k: noul(0.9 if k != "__enough__" else 0.95) for k in p["questions"]}
        )
        evidence, _ = r.run("q", Research(rounds=1, top_k=2))
        self.assertTrue(evidence.sections, "the snippets alone were worth keeping")
        self.assertIn("Search results", evidence.sections[0].title)

    def test_pinned_urls_skip_search_entirely(self):
        called = []
        self.agent.search = lambda q, limit=10: called.append(q) or []
        r, _ = researcher(
            lambda p: {k: noul(0.9 if k != "__enough__" else 0.95) for k in p["questions"]}
        )
        evidence, _ = r.run("q", Research(rounds=1, urls=("https://e/pinned",)))
        self.assertEqual(called, [])
        self.assertTrue(evidence.sections)

    def test_sources_are_deduplicated_by_page(self):
        r, _ = researcher(
            lambda p: {k: noul(0.9 if k != "__enough__" else 0.95) for k in p["questions"]}
        )
        evidence, _ = r.run("q", Research(rounds=1, top_k=2))
        pages = [x for x in evidence.sources() if not x["title"].startswith("Search results")]
        self.assertEqual(len(pages), 2)
        self.assertEqual(sum(s["sections"] for s in evidence.sources()), len(evidence.sections))


class TestEvidenceText(unittest.TestCase):
    def test_a_tight_limit_truncates_rather_than_emptying(self):
        evidence = Evidence(sections=sections(3))
        self.assertTrue(evidence.as_text(80))
        self.assertLessEqual(len(evidence.as_text(80)), 81)

    def test_nothing_kept_yields_nothing(self):
        self.assertEqual(Evidence().as_text(), "")


def roster_of(*specs):
    from jevharness.roster import Credential, ModelSpec, Roster

    r = Roster(models=list(specs),
               credentials={"default": Credential("default", "sk-fake-000000000000")})
    r.validate()
    return r


def cheap_and_dear():
    from jevharness.roster import ModelSpec

    return roster_of(
        ModelSpec(id="tiny", model="v/tiny", capability=1, price_out=0.01, label="tiny"),
        ModelSpec(id="big", model="v/big", capability=4, price_out=2.0, label="big"),
    )


class TestAssess(unittest.TestCase):
    def test_one_question_decides_whether_to_search(self):
        transport = FakeTransport(decision_hook=lambda p: {k: noul(0.9) for k in p["questions"]})
        needs, skill, call, *_ = assess(JevClient(transport, "j"), "what is the price today?", "")
        self.assertTrue(needs)
        self.assertIsNone(skill, "no library was offered, so no skill is chosen")
        self.assertEqual(len(transport.decision_calls), 1)
        # research and "is this an app", nothing else
        self.assertEqual(set(transport.decision_calls[0]["questions"]),
                         {"__needs_research__", "__multi_step__", "__wants_app__"})
        self.assertIsNotNone(call)

    def test_what_the_run_can_open_itself_is_named_in_the_state(self):
        """Otherwise a task pointing at workspace files reads as missing its facts."""
        seen = {}

        def hook(payload):
            seen["state"] = payload["state"]
            return {k: noul(0.05) for k in payload["questions"]}

        transport = FakeTransport(decision_hook=hook)
        assess(JevClient(transport, "j"), "read the files and summarise them", "",
               reachable="The workspace ...: notes.md, data.csv")
        self.assertIn("REACHABLE WITHOUT THE WEB", seen["state"])
        self.assertIn("notes.md", seen["state"])

    def test_nothing_reachable_leaves_the_state_alone(self):
        seen = {}

        def hook(payload):
            seen["state"] = payload["state"]
            return {k: noul(0.9) for k in payload["questions"]}

        assess(JevClient(FakeTransport(decision_hook=hook), "j"), "what is the price today?", "")
        self.assertNotIn("REACHABLE WITHOUT THE WEB", seen["state"])

    def test_material_that_suffices_says_no(self):
        transport = FakeTransport(decision_hook=lambda p: {k: noul(0.05) for k in p["questions"]})
        needs, *_ = assess(JevClient(transport, "j"), "summarise this", "a long email")
        self.assertFalse(needs)

    def test_a_failure_is_not_fatal(self):
        class Broken(FakeTransport):
            def post_json(self, path, payload):
                raise RuntimeError("down")

        needs, skill, call, *_ = assess(JevClient(Broken(), "j"), "q", "")
        self.assertIsNone(needs)
        self.assertIsNone(skill)
        self.assertIsNone(call)


class TestSkillPick(unittest.TestCase):
    """The skill choice rides in the assess call, so it costs nothing extra."""

    def library(self):
        from jevharness.skills import Library

        return Library()

    def respond(self, picked, certainty=0.9):
        def hook(payload):
            out = {}
            for name in payload["questions"]:
                if name == "__skill__":
                    out[name] = choice(picked, {picked: certainty}, certainty)
                else:
                    out[name] = noul(0.9)
            return out

        return hook

    def test_both_questions_travel_in_one_call(self):
        transport = FakeTransport(decision_hook=self.respond("code-review"))
        needs, skill, call, *_ = assess(JevClient(transport, "j"), "review this diff", "",
                                    self.library())
        self.assertEqual(len(transport.decision_calls), 1)
        self.assertEqual(len(transport.decision_calls[0]["questions"]), 4)
        self.assertTrue(needs)
        self.assertEqual(skill, "code-review")

    def test_none_means_no_skill(self):
        transport = FakeTransport(decision_hook=self.respond("none"))
        _, skill, *_ = assess(JevClient(transport, "j"), "anything", "", self.library())
        self.assertIsNone(skill)

    def test_a_coin_flip_choice_applies_no_skill(self):
        """Forcing a shape onto a task that did not ask for it is worse than none."""
        transport = FakeTransport(decision_hook=self.respond("translate", certainty=0.3))
        _, skill, *_ = assess(JevClient(transport, "j"), "anything", "", self.library())
        self.assertIsNone(skill)

    def test_a_confident_choice_applies(self):
        transport = FakeTransport(decision_hook=self.respond("translate", certainty=0.85))
        _, skill, *_ = assess(JevClient(transport, "j"), "translate this", "", self.library())
        self.assertEqual(skill, "translate")


class TestRouting(unittest.TestCase):
    """Every subtask is sized, sourced and skipped in a single call."""

    def hook(self, caps, web=0.05, done=0.05, independent=0.1, assembly=0.1,
             role="extractor"):
        """extractor is the default here: floor 1, so the rung under test shows."""
        def respond(payload):
            out = {}
            for name in payload["questions"]:
                if name == "__independent__":
                    out[name] = noul(independent)
                elif name == "__assembly__":
                    out[name] = noul(assembly)
                elif name.startswith("r"):
                    picked = role if isinstance(role, str) else role[int(name[1:])]
                    out[name] = choice(picked, {picked: 0.95}, 0.95)
                elif name.startswith("c"):
                    out[name] = score(caps[int(name[1:])], 5, 0.95)
                elif name.startswith("w"):
                    out[name] = noul(web)
                elif name.startswith("d"):
                    out[name] = noul(done)
            return out

        return respond

    def test_four_questions_per_step_plus_two_in_one_call(self):
        transport = FakeTransport(decision_hook=self.hook([1.0, 4.0, 2.0]))
        routing, call = route(JevClient(transport, "j"), cheap_and_dear(),
                              ["reformat", "analyse", "summarise"], "q", Evidence())
        self.assertEqual(len(transport.decision_calls), 1, "one call, whatever the step count")
        # role, capability, own-lookup and already-done per step, plus the two
        # questions about the set.
        self.assertEqual(len(transport.decision_calls[0]["questions"]), 3 * 4 + 2)
        self.assertIsNotNone(call)

    def test_each_step_gets_the_cheapest_model_that_clears_its_own_rung(self):
        transport = FakeTransport(decision_hook=self.hook([1.0, 4.0], role="extractor"))
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(),
                           ["reformat", "analyse"], "q", Evidence())
        self.assertEqual([t.model_id for t in routing.subtasks], ["tiny", "big"])
        self.assertEqual([t.required for t in routing.subtasks], [1, 4])

    def test_a_role_that_may_research_can_be_flagged_for_its_own_lookup(self):
        transport = FakeTransport(decision_hook=self.hook([2.0, 2.0], web=0.9, role="researcher"))
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(),
                           ["a", "b"], "q", Evidence())
        self.assertTrue(all(t.needs_web for t in routing.subtasks))

    def test_a_role_that_may_not_research_is_never_sent_to_the_web(self):
        transport = FakeTransport(decision_hook=self.hook([2.0], web=0.99, role="editor"))
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(), ["a"], "q", Evidence())
        self.assertFalse(routing.subtasks[0].needs_web)

    def test_each_step_is_staffed_with_a_role(self):
        transport = FakeTransport(
            decision_hook=self.hook([1.0, 2.0], role=["extractor", "analyst"])
        )
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(),
                           ["pull the numbers", "judge the risk"], "q", Evidence())
        self.assertEqual([t.role for t in routing.subtasks], ["extractor", "analyst"])

    def test_a_role_floor_overrides_a_low_capability_rating(self):
        """An analyst on the toy model produces confident nonsense."""
        transport = FakeTransport(decision_hook=self.hook([1.0], role="analyst"))
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(), ["judge it"], "q", Evidence())
        self.assertEqual(routing.subtasks[0].required, 3)
        self.assertEqual(routing.subtasks[0].model_id, "big")

    def test_a_high_capability_rating_is_not_lowered_by_a_role(self):
        transport = FakeTransport(decision_hook=self.hook([4.0], role="extractor"))
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(), ["pull it"], "q", Evidence())
        self.assertEqual(routing.subtasks[0].required, 4)

    def test_an_unknown_role_falls_back_rather_than_failing(self):
        transport = FakeTransport(decision_hook=self.hook([2.0], role="astronaut"))
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(), ["a"], "q", Evidence())
        self.assertEqual(routing.subtasks[0].role, "writer")

    def test_steps_the_material_already_answers_are_marked(self):
        transport = FakeTransport(decision_hook=self.hook([2.0], done=0.95))
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(),
                           ["a"], "q", Evidence(sections=sections(1)))
        self.assertTrue(routing.subtasks[0].already_done)

    def test_the_set_level_flags_are_read(self):
        transport = FakeTransport(decision_hook=self.hook([2.0, 2.0], independent=0.9, assembly=0.9))
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(), ["a", "b"], "q", Evidence())
        self.assertTrue(routing.independent)
        self.assertTrue(routing.needs_assembly)

    def test_no_steps_means_no_call(self):
        transport = FakeTransport()
        routing, call = route(JevClient(transport, "j"), cheap_and_dear(), [], "q", Evidence())
        self.assertEqual(routing.subtasks, [])
        self.assertIsNone(call)
        self.assertEqual(transport.requests, [])

    def test_a_routing_failure_still_yields_runnable_steps(self):
        class Broken(FakeTransport):
            def post_json(self, path, payload):
                raise RuntimeError("down")

        routing, call = route(JevClient(Broken(), "j"), cheap_and_dear(), ["a", "b"], "q", Evidence())
        self.assertEqual(len(routing.subtasks), 2)
        self.assertTrue(all(t.model_id for t in routing.subtasks))
        self.assertIsNone(call)


if __name__ == "__main__":
    unittest.main()


class TestSkipSafety(unittest.TestCase):
    """Dropping a step removes part of the deliverable, so the bar is high."""

    def hook(self, done):
        def respond(payload):
            out = {}
            for name in payload["questions"]:
                if name.startswith("c"):
                    out[name] = score(2.0, 5, 0.95)
                elif name.startswith("d"):
                    out[name] = noul(done)
                else:
                    out[name] = noul(0.05)
            return out

        return respond

    def test_a_bare_majority_does_not_skip_a_step(self):
        transport = FakeTransport(decision_hook=self.hook(0.6))
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(), ["a"], "q", Evidence())
        self.assertFalse(routing.subtasks[0].already_done)

    def test_a_clear_yes_skips_it(self):
        transport = FakeTransport(decision_hook=self.hook(0.95))
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(), ["a"], "q", Evidence())
        self.assertTrue(routing.subtasks[0].already_done)

    def test_the_question_asks_about_written_output_not_facts(self):
        transport = FakeTransport(decision_hook=self.hook(0.1))
        route(JevClient(transport, "j"), cheap_and_dear(), ["the cost section"], "q", Evidence())
        asked = transport.decision_calls[0]["questions"]["d0"]["instructions"]
        self.assertIn("FINISHED, WRITTEN", asked)
        self.assertIn("merely contains facts", asked)


class TestBespokeWorkers(unittest.TestCase):
    """A step the plan staffed itself is not put to Jev again."""

    from jevharness.plan import StepSpec as _Spec

    def hook(self):
        def respond(payload):
            out = {}
            for name in payload["questions"]:
                if name.startswith("c"):
                    out[name] = score(2.0, 5, 0.95)
                elif name.startswith("r"):
                    out[name] = choice("writer", {"writer": 0.9}, 0.9)
                else:
                    out[name] = noul(0.05)
            return out

        return respond

    def test_a_declared_role_skips_its_role_question(self):
        steps = [self._Spec(title="pull the numbers", role="extractor"),
                 self._Spec(title="write it up")]
        transport = FakeTransport(decision_hook=self.hook())
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(), steps, "q", Evidence())
        asked = transport.decision_calls[0]["questions"]
        self.assertNotIn("r0", asked, "the plan already staffed step 0")
        self.assertIn("r1", asked)
        self.assertEqual(routing.subtasks[0].role, "extractor")

    def test_a_bespoke_persona_is_carried_onto_the_step(self):
        persona = "You are a compliance officer who cites the clause number."
        steps = [self._Spec(title="the regulatory annex", persona=persona)]
        transport = FakeTransport(decision_hook=self.hook())
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(), steps, "q", Evidence())
        self.assertEqual(routing.subtasks[0].persona, persona)
        self.assertNotIn("r0", transport.decision_calls[0]["questions"])
        self.assertTrue(routing.subtasks[0].to_dict()["bespoke"])

    def test_plain_string_steps_still_work(self):
        transport = FakeTransport(decision_hook=self.hook())
        routing, _ = route(JevClient(transport, "j"), cheap_and_dear(),
                           ["a plain step"], "q", Evidence())
        self.assertEqual(routing.subtasks[0].title, "a plain step")
        self.assertIn("r0", transport.decision_calls[0]["questions"])
