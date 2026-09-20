"""Patterns borrowed from 'The Anatomy of an Agent Harness', and their tests."""
import tempfile
import unittest
from pathlib import Path

from jevharness.agent import gate_actions
from jevharness.memory import house_rules
from jevharness.plan import Task
from jevharness.providers import JevClient
from jevharness.roster import CAPABILITY_DECISION
from jevharness.store import Store
from tests.fakes import FakeTransport, noul, score
from tests.test_executor import CAP_LEVELS, build


class TestConversation(unittest.TestCase):
    def test_a_follow_up_reaches_the_writer_with_its_context(self):
        executor, transport = build(
            completions=["shorter version"],
            decisions={CAPABILITY_DECISION: score(1.0, CAP_LEVELS, 0.95)},
        )
        task = Task(prompt="Write a shorter version of it.",
                    history=(("user", "Write about the sea."),
                             ("assistant", "THE ORIGINAL ANSWER about the sea.")))
        executor.run(task)
        prompt = str(transport.chat_calls[-1]["messages"])
        self.assertIn("THE ORIGINAL ANSWER", prompt)
        self.assertIn("CONVERSATION SO FAR", prompt)

    def test_the_judgements_see_the_conversation_too(self):
        executor, transport = build(
            completions=["x"], decisions={CAPABILITY_DECISION: score(1.0, CAP_LEVELS, 0.95)})
        executor.run(Task(prompt="Write a shorter version of it.",
                          history=(("assistant", "PRIOR TEXT"),)))
        self.assertTrue(any("PRIOR TEXT" in str(c.get("state")) for c in transport.decision_calls))

    def test_compaction_keeps_the_newest_and_trims_the_oldest(self):
        history = tuple((("user", f"q{i}"), ("assistant", "A" * 2000))[i % 2] for i in range(12))
        text = Task(prompt="go", history=history).conversation(budget=4000)
        self.assertLessEqual(len(text), 4200)
        self.assertIn("A" * 2000, text, "the latest answer is kept whole")

    def test_history_does_not_change_the_cached_plan(self):
        a = Task(prompt="summarise this email")
        b = Task(prompt="summarise this email", history=(("user", "hi"),))
        self.assertEqual(a.shape_key(), b.shape_key())


class TestToolGate(unittest.TestCase):
    def jev(self, p):
        transport = FakeTransport(decision_hook=lambda payload: {k: noul(p) for k in payload["questions"]})
        return JevClient(transport, "j"), transport

    def test_every_pending_action_is_judged_in_one_call(self):
        jev, transport = self.jev(0.95)
        verdicts, call = gate_actions(jev, "save my notes",
                                      [("write_file", {"path": "a.md"}), ("fetch_url", {"url": "https://x"})])
        self.assertEqual(len(transport.decision_calls), 1,
                         "both questions for both actions ride in one call")
        self.assertEqual([v.ok for v in verdicts], [True, True])

    def test_anything_short_of_a_clear_yes_is_refused(self):
        jev, _ = self.jev(0.7)
        verdicts, _ = gate_actions(jev, "task", [("shell", {"command": "rm -rf ~"})])
        self.assertFalse(verdicts[0][0])

    def test_no_answer_means_no_permission(self):
        class Broken(FakeTransport):
            def post_json(self, path, payload):
                raise RuntimeError("down")

        verdicts, call = gate_actions(JevClient(Broken(), "j"), "task", [("shell", {"command": "ls"})])
        self.assertFalse(verdicts[0].ok)
        self.assertEqual(verdicts[0].reason, "gate_unavailable")
        self.assertIsNone(call)

    def test_asked_for_and_safe_are_judged_apart(self):
        """One bundled question averages the two and refuses work the user named."""
        def hook(payload):
            # asked: clearly yes. safe: clearly no.
            return {k: noul(0.95 if k.startswith("a") else 0.1)
                    for k in payload["questions"]}

        jev = JevClient(FakeTransport(decision_hook=hook), "j")
        verdicts, _ = gate_actions(jev, "delete the old exports",
                                   [("shell", {"command": "rm -rf /"})])
        self.assertFalse(verdicts[0].ok)
        self.assertEqual(verdicts[0].reason, "gate_unsafe",
                         "an unsafe action is refused however plainly it was asked for")

    def test_an_action_nobody_asked_for_is_named_as_such(self):
        def hook(payload):
            return {k: noul(0.05 if k.startswith("a") else 0.9)
                    for k in payload["questions"]}

        jev = JevClient(FakeTransport(decision_hook=hook), "j")
        verdicts, _ = gate_actions(jev, "summarise these notes",
                                   [("fetch_url", {"url": "https://tracker.example/ping"})])
        self.assertFalse(verdicts[0].ok)
        self.assertEqual(verdicts[0].reason, "gate_not_asked")

    def test_nothing_pending_costs_nothing(self):
        jev, transport = self.jev(0.9)
        self.assertEqual(gate_actions(jev, "t", []), ([], None))
        self.assertEqual(transport.requests, [])


class TestVerification(unittest.TestCase):
    def hook(self, on_task):
        def respond(payload):
            out = {}
            for name in payload["questions"]:
                if name == CAPABILITY_DECISION:
                    out[name] = score(1.0, CAP_LEVELS, 0.95)
                elif name == "on_task":
                    out[name] = noul(on_task)
                elif name == "refused":
                    out[name] = noul(0.02)
            return out
        return respond

    def test_a_missed_answer_gets_one_more_attempt(self):
        executor, transport = build(completions=["I cannot help.", "The real answer."],
                                    decision_hook=self.hook(0.1), guard=True)
        result = executor.run(Task(prompt="Write a note about this.", state="x"))
        self.assertEqual(result.output, "The real answer.")
        self.assertIn("retry", [s.code for s in result.steps])
        self.assertEqual(len(transport.chat_calls), 2)

    def test_a_good_answer_is_not_redone(self):
        executor, transport = build(completions=["Fine."], decision_hook=self.hook(0.95), guard=True)
        result = executor.run(Task(prompt="Write a note about this.", state="x"))
        self.assertNotIn("retry", [s.code for s in result.steps])
        self.assertEqual(len(transport.chat_calls), 1)

    def test_the_check_is_shown_the_task(self):
        executor, transport = build(completions=["Fine."], decision_hook=self.hook(0.95), guard=True)
        executor.run(Task(prompt="Write a note about THE SPECIFIC THING.", state="x"))
        guard = [c for c in transport.decision_calls if "on_task" in c["questions"]][0]
        self.assertIn("THE SPECIFIC THING", guard["state"])


class TestWorkspaceFiles(unittest.TestCase):
    def test_agents_md_is_read_as_house_rules(self):
        ws = Path(tempfile.mkdtemp())
        (ws / "AGENTS.md").write_text("Always use metric units.")
        self.assertEqual(house_rules(ws), "Always use metric units.")
        self.assertEqual(house_rules(None), "")

    def test_house_rules_reach_the_writer(self):
        from jevharness.executor import Executor

        executor, transport = build(completions=["x"],
                                    decisions={CAPABILITY_DECISION: score(1.0, CAP_LEVELS, 0.95)})
        executor.rules = "Always use metric units."
        executor.run(Task(prompt="Write a note about this.", state="x"))
        self.assertIn("metric units", str(transport.chat_calls[-1]["messages"]))

    def test_evidence_is_written_beside_the_answer(self):
        ws = Path(tempfile.mkdtemp())
        saved = Store(ws).save_turn(chat_id="a", title="t", prompt="p", state="",
                                    result={"output": "answer",
                                            "evidence": {"sources": [{"title": "S", "url": "https://s"}]}},
                                    evidence="[S]\nthe kept section")
        names = [p.name for p in saved.output_paths]
        self.assertIn("evidence.md", names)
        text = next(p for p in saved.output_paths if p.name == "evidence.md").read_text()
        self.assertIn("the kept section", text)
        self.assertIn("https://s", text)


if __name__ == "__main__":
    unittest.main()


class TestHonestResearch(unittest.TestCase):
    def test_an_empty_search_is_reported_not_denied(self):
        from jevharness.policy import generation_messages, Review
        messages = generation_messages("flight prices", "", {}, Review(),
                                       searched=["北京 曼谷 机票 2026"])
        text = messages[1]["content"]
        self.assertIn("never say you cannot browse", text)
        self.assertIn("no specific prices", text)

    def test_every_prompt_knows_the_date(self):
        import datetime
        from jevharness.policy import generation_messages, Review
        messages = generation_messages("x", "", {}, Review())
        self.assertIn(datetime.date.today().isoformat(), messages[0]["content"])

    def test_search_terms_come_back_as_a_list_whatever_the_format(self):
        from jevharness.policy import parse_terms
        self.assertEqual(parse_terms('["a b", "c"]'), ["a b", "c"])
        self.assertEqual(parse_terms("1. a b\n2. c"), ["a b", "c"])



class TestTalkingToAWorker(unittest.TestCase):
    def executor(self, transport):
        from tests.test_appkit import executor_with
        return executor_with(transport)

    def test_a_worker_rewrites_its_own_part_from_what_it_is_told(self):
        from jevharness.agent import Subtask
        transport = FakeTransport(decision_hook=lambda p: {"web": noul(0.1)},
                                  completions=["The shorter version."])
        step = Subtask(title="Background", index=1, model_id="mid", output="A long first draft.")
        events = list(self.executor(transport).revise_subtask(
            Task(prompt="Write a brief"), step, "make it shorter",
            thread=[{"role": "user", "content": "earlier ask"}, {"role": "assistant", "content": "earlier reply"}]))
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["output"], "The shorter version.")
        sent = transport.chat_calls[-1]["messages"]
        self.assertEqual(sent[2], {"role": "assistant", "content": "A long first draft."},
                         "it starts from what it wrote")
        self.assertIn("make it shorter", sent[-1]["content"])
        self.assertEqual(transport.chat_calls[-1]["model"], "v/mid", "same worker, same model")
        self.assertFalse(transport.term_calls, "Jev said no lookup was needed")

    def test_asking_for_new_facts_sends_it_back_to_the_web(self):
        import jevharness.agent as agent
        from jevharness.agent import Subtask
        from jevharness.research import Hit, Page, Section
        saved = agent.search, agent.fetch_all
        agent.search = lambda q, limit=10: [Hit(title="t", url="https://e/1", snippet="s", rank=0)]
        agent.fetch_all = lambda urls, workers=4: [
            Page(url=u, title="Src", text="", sections=[Section(text="FRESH FIGURE " + "x" * 250, url=u, title="Src")])
            for u in urls]
        try:
            transport = FakeTransport(decision_hook=lambda p: {k: noul(0.9) for k in p["questions"]},
                                      completions=["With the figure."])
            step = Subtask(title="Market size", index=0, model_id="mid", output="Draft.")
            events = list(self.executor(transport).revise_subtask(
                Task(prompt="Market report"), step, "add this year's market size"))
        finally:
            agent.search, agent.fetch_all = saved
        self.assertTrue(any(e["type"] == "log" and e["entry"]["code"] == "search" for e in events))
        self.assertIn("FRESH FIGURE", transport.chat_calls[-1]["messages"][1]["content"])
        self.assertTrue(events[-1]["sources"])


class TestWatchingWorkers(unittest.TestCase):
    def test_each_worker_streams_its_words_and_says_what_it_is_doing(self):
        from tests.test_executor import build, CAP_LEVELS
        from tests.fakes import choice, score
        from jevharness.roster import CAPABILITY_DECISION
        plan = ('{"strategy":"s","answer_from":"generation","generation":{"instruction":"write"},'
                '"steps":[{"title":"one"},{"title":"two"}]}')

        def hook(p):
            out = {}
            for name in p["questions"]:
                if name == CAPABILITY_DECISION or name.startswith("c"):
                    out[name] = score(2.0, CAP_LEVELS, 0.95)
                elif name.startswith("r"):
                    out[name] = choice("writer", {"writer": 0.95}, 0.95)
                elif name == "__independent__":
                    out[name] = noul(0.9)
                else:
                    out[name] = noul(0.05)
            return out

        executor, _ = build(completions=[plan, "part one text", "part two text"], decision_hook=hook)
        events = list(executor.stream(Task(prompt="Write two parts about tea.", state="notes")))
        deltas = [e for e in events if e["type"] == "subtask_delta"]
        logs = [e for e in events if e["type"] == "subtask_log"]
        self.assertEqual({e["index"] for e in deltas}, {0, 1})
        self.assertTrue(all(e["entry"]["code"] == "write" for e in logs))
        done = events[-1]["result"]["subtasks"]
        self.assertTrue(all(t["log"] for t in done), "the log travels with the step")


class TestFailover(unittest.TestCase):
    """A model that refuses (region lock, withdrawn id) hands over to the next."""

    def setUp(self):
        import jevharness.roster as roster
        self.roster = roster
        roster._benched.clear()

    def tearDown(self):
        self.roster._benched.clear()

    def transport(self, completions):
        from jevharness.errors import ProviderError

        class Refusing(FakeTransport):
            def post_stream(self, path, payload):
                if payload.get("model") == "v/tiny":
                    self.requests.append((path, payload))
                    raise ProviderError("upstream HTTP 403", detail='{"error":{"message":"This model is not available in your region."}}')
                return super().post_stream(path, payload)

        def hook(p):
            out = {}
            for name in p["questions"]:
                out[name] = score(1.0, 5, 0.95) if name == CAPABILITY_DECISION else noul(0.05)
            return out
        return Refusing(decision_hook=hook, completions=completions)

    def test_a_refused_model_is_replaced_and_benched(self):
        from tests.test_appkit import executor_with
        transport = self.transport(["Bonjour."])
        result = executor_with(transport).run(Task(prompt="Translate hello into French."))
        self.assertEqual(result.output, "Bonjour.")
        self.assertEqual(result.selection.model.model, "v/mid", "the next cheapest that clears the rung")
        switch = [s for s in result.steps if s.code == "failover"]
        self.assertEqual(len(switch), 1)
        self.assertIn("not available in your region", switch[0].data["reason"])
        self.assertTrue(self.roster.unavailable("v/tiny"))

    def test_a_benched_model_is_not_chosen_again(self):
        from tests.test_appkit import executor_with
        self.roster.bench("v/tiny", "region")
        transport = self.transport(["Hola."])
        executor_with(transport).run(Task(prompt="Translate hello into Spanish."))
        self.assertNotIn("v/tiny", [b.get("model") for p, b in transport.requests if p.endswith("/chat/completions")])


class TestMultiStep(unittest.TestCase):
    def test_jev_saying_several_stages_forces_a_real_plan(self):
        from tests.test_appkit import executor_with
        plan = ('{"strategy":"s","answer_from":"generation","generation":{"instruction":"write"},'
                '"steps":[{"title":"gather"},{"title":"outline"}]}')

        def hook(p):
            return {n: (noul(0.9) if n == "__multi_step__" else score(2.0, 5, 0.9) if n == CAPABILITY_DECISION
                        or n.startswith("c") else noul(0.05)) for n in p["questions"]}
        transport = FakeTransport(decision_hook=hook, completions=[plan, "part a", "part b"])
        # "write an article" alone would take the no-planning shortcut.
        result = executor_with(transport).run(Task(prompt="Write an article about tea."))
        self.assertEqual(result.plan.source, "compiled")
        self.assertEqual([t.title for t in result.subtasks], ["gather", "outline"])

    def test_a_single_piece_of_work_keeps_the_shortcut(self):
        from tests.test_appkit import executor_with

        def hook(p):
            return {n: (score(2.0, 5, 0.9) if n == CAPABILITY_DECISION else noul(0.05)) for n in p["questions"]}
        transport = FakeTransport(decision_hook=hook, completions=["A poem."])
        result = executor_with(transport).run(Task(prompt="Write a short poem about tea."))
        self.assertEqual(result.plan.source, "deterministic")


class TestPlanRescue(unittest.TestCase):
    def test_a_truncated_plan_is_retried_on_a_stronger_model(self):
        from jevharness.planner import Planner, PlanCache
        from jevharness.providers import LLMClient
        cheap = FakeTransport(completions=['{"strategy":"s","answer_from":"generation","steps":[{"title":"a"'])
        strong = FakeTransport(completions=['{"strategy":"s","answer_from":"generation",'
                                            '"generation":{"instruction":"w"},"steps":[{"title":"a"},{"title":"b"}]}'])
        planner = Planner(LLMClient(cheap, "cheap"), cache=PlanCache(), compiler_model="cheap",
                          backup=(LLMClient(strong, "strong"), "strong"))
        planner.must_compile = True
        plan, call = planner.plan(Task(prompt="Research the market, then write a report."))
        self.assertEqual(plan.source, "compiled")
        self.assertEqual([s.title for s in plan.steps], ["a", "b"])
        self.assertEqual(call.model, "strong")
