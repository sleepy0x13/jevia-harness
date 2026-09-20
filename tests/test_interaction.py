"""Asking, approving, planning: the three places a run stops for a person.

A harness that guesses when it could have asked is the thing people complain
about. These check that the question reaches the interface *while* the run
waits, that an unanswered question does not hang the run for ever, and that a
refusal is final.
"""
import threading
import time
import unittest

from jevharness.events import Cancelled, RunEvents
from jevharness.loop import Gate
from jevharness.providers import ToolCall
from jevharness.tools import Registry, ToolContext


def workspace():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())


def drain(events):
    out = []
    while True:
        try:
            _, event = events._queue.get_nowait()
        except Exception:
            return out
        out.append(event)


def answer_when_asked(events, reply, after=0.05):
    """Play the interface: wait for the question, then send the answer."""
    def respond():
        for _ in range(100):
            if events.waiting:
                events.answer(events.waiting[0], reply)
                return
            time.sleep(after)

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()
    return thread


class TestAsking(unittest.TestCase):
    def test_the_question_is_on_the_record_before_the_answer_arrives(self):
        events = RunEvents()
        answer_when_asked(events, "the second one")
        reply = events.ask("Which report do you mean?", options=["First", "Second"])
        self.assertEqual(reply, "the second one")
        kinds = [e["kind"] for e in drain(events)]
        self.assertEqual(kinds, ["question.asked", "question.answered"])

    def test_nobody_answering_does_not_hang_the_run(self):
        events = RunEvents()
        started = time.monotonic()
        self.assertIsNone(events.ask("Anyone there?", timeout=0.1))
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual([e["kind"] for e in drain(events)][-1], "question.answered")

    def test_stopping_the_run_releases_a_waiting_question(self):
        events = RunEvents()
        threading.Timer(0.05, events.cancel.set).start()
        with self.assertRaises(Cancelled):
            events.ask("Which one?", timeout=5)
        self.assertEqual(events.waiting, [], "a stopped run leaves nothing waiting")

    def test_the_tool_asks_and_hands_back_what_was_said(self):
        events = RunEvents()
        registry = Registry.builtin(
            enabled=["ask_user_question"],
            context=ToolContext(workspace=workspace(),
                                ask=lambda q, options=(), kind="question": events.ask(
                                    q, options=options, kind=kind)))
        answer_when_asked(events, "Use the 2026 figures.")
        result = registry.run("ask_user_question",
                              {"question": "Which year?", "options": ["2025", "2026"]})
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "Use the 2026 figures.")

    def test_with_no_one_to_ask_the_tool_says_so_instead_of_guessing(self):
        registry = Registry.builtin(enabled=["ask_user_question"],
                                    context=ToolContext(workspace=workspace()))
        result = registry.run("ask_user_question", {"question": "Which year?"})
        self.assertFalse(result.ok)
        self.assertIn("no one to ask", result.detail)


class TestApproval(unittest.TestCase):
    def registry(self, root):
        return Registry.builtin(enabled=["write_file"], context=ToolContext(workspace=root))

    def gate(self, root, decision):
        return Gate(self.registry(root),
                    judge=lambda actions: ([(True, 0.95) for _ in actions], None),
                    ask=lambda name, call: decision)

    def test_a_refusal_stops_a_call_the_gate_had_allowed(self):
        root = workspace()
        call = ToolCall(id="c1", name="write_file", arguments={"path": "x.md", "content": "hi"})
        verdicts, _ = self.gate(root, False).screen([call])
        self.assertFalse(verdicts["c1"]["ok"])
        self.assertEqual(verdicts["c1"]["reason"], "user_refused")

    def test_an_approval_lets_it_through_and_is_recorded_as_the_reason(self):
        root = workspace()
        call = ToolCall(id="c1", name="write_file", arguments={"path": "x.md", "content": "hi"})
        verdicts, _ = self.gate(root, True).screen([call])
        self.assertTrue(verdicts["c1"]["ok"])
        self.assertEqual(verdicts["c1"]["reason"], "user_approved")

    def test_reading_is_never_worth_a_question(self):
        root = workspace()
        asked = []
        gate = Gate(Registry.builtin(enabled=["read_file"], context=ToolContext(workspace=root)),
                    judge=lambda actions: ([(True, 0.9) for _ in actions], None),
                    ask=lambda name, call: asked.append(name) or True)
        verdicts, _ = gate.screen([ToolCall(id="c1", name="read_file", arguments={"path": "a"})])
        self.assertTrue(verdicts["c1"]["ok"])
        self.assertEqual(asked, [], "only changes are worth interrupting for")


class TestPlanMode(unittest.TestCase):
    """The plan is shown before the work, and what the user says goes back into it."""

    PLAN = ('{"strategy": "%s", "answer_from": "generation", "decisions": {},'
            ' "generation": {"instruction": "Write it."}}')

    def build(self, *, plan_mode=True, completions=None):
        from jevharness.config import Thresholds
        from jevharness.executor import Executor
        from jevharness.planner import Planner, PlanCache
        from jevharness.providers import JevClient, LLMClient
        from jevharness.roster import CAPABILITY_DECISION, Credential, ModelSpec, Roster
        from tests.fakes import FakeTransport, noul, score

        def judge(payload):
            return {k: (score(2.0, 5, 0.9) if k == CAPABILITY_DECISION
                        else noul(0.9 if k == "__multi_step__" else 0.05))
                    for k in payload["questions"]}

        transport = FakeTransport(decision_hook=judge, completions=list(completions or []))
        roster = Roster(models=[ModelSpec(id="m", model="v/m", capability=2, price_out=0.1)],
                        credentials={"default": Credential("default", "sk-fake-000000000000")})
        roster.validate()
        events = RunEvents()
        executor = Executor(
            jev=JevClient(transport, "fake/jev"),
            llm_factory=lambda spec: LLMClient(transport, spec.model),
            planner=Planner(llm=LLMClient(transport, "v/m"), cache=PlanCache()),
            roster=roster, thresholds=Thresholds(), escalate_uncertain=False,
            events=events, plan_mode=plan_mode, agent_loop=False)
        return executor, transport, events

    def test_the_plan_is_proposed_and_running_it_proceeds(self):
        from jevharness.plan import Task

        executor, _, events = self.build(completions=[self.PLAN % "first plan", "the answer"])
        answer_when_asked(events, "Run it")
        result = executor.run(Task(prompt="Write something with several parts."))
        kinds = [e["kind"] for e in events.log]
        self.assertIn("plan.proposed", kinds)
        decided = [e for e in events.log if e["kind"] == "plan.decided"][0]
        self.assertTrue(decided["payload"]["approved"])
        self.assertEqual(result.output, "the answer")

    def test_feedback_recompiles_the_plan_before_any_work(self):
        from jevharness.plan import Task

        executor, transport, events = self.build(
            completions=[self.PLAN % "first plan", self.PLAN % "second plan", "the answer"])
        replies = iter(["Change it: do it in two parts", "Run it"])

        def respond():
            for _ in range(200):
                if events.waiting:
                    events.answer(events.waiting[0], next(replies))
                time.sleep(0.02)

        threading.Thread(target=respond, daemon=True).start()
        result = executor.run(Task(prompt="Write something with several parts."))
        decisions = [e["payload"]["approved"] for e in events.log if e["kind"] == "plan.decided"]
        self.assertEqual(decisions, [False, True])
        compiles = [c for c in transport.chat_calls
                    if "planner for a two-engine harness" in str(c["messages"][0]["content"])]
        self.assertEqual(len(compiles), 2, "the plan is written again with what the user said")
        self.assertIn("do it in two parts", str(compiles[1]["messages"][-1]["content"]))
        self.assertEqual(result.output, "the answer")

    def test_with_plan_mode_off_nothing_is_asked(self):
        from jevharness.plan import Task

        executor, _, events = self.build(plan_mode=False,
                                         completions=[self.PLAN % "plain", "the answer"])
        executor.run(Task(prompt="Write something with several parts."))
        self.assertNotIn("plan.proposed", [e["kind"] for e in events.log])


if __name__ == "__main__":
    unittest.main()
