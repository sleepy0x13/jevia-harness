"""The agent loop: a turn is steps, a step is one request and the tools it runs.

These are the cases the old pipeline could not do at all: look at a result and
decide the next move, be refused and work another way, stop when a budget runs
out instead of going round for ever.
"""
import json
import unittest

from jevharness.errors import ProviderError
from jevharness.events import RunEvents
from jevharness.loop import AgentLoop, Budget, Gate
from jevharness.providers import LLMClient, ToolCall, parse_tool_calls
from jevharness.session import ASSISTANT, Log
from jevharness.tools import Registry, ToolContext
from tests.fakes import FakeTransport


def scripted(*turns):
    """A transport that answers each request with the next scripted reply.

    Each turn is either a string (plain text) or a list of (tool, args) pairs.
    """
    class Scripted(FakeTransport):
        def __init__(self):
            super().__init__()
            self.turn = 0
            self.requests = []
            self.term_calls = []

        def post_stream(self, path, payload):
            self.requests.append((path, payload))
            reply = turns[min(self.turn, len(turns) - 1)]
            self.turn += 1
            if isinstance(reply, str):
                yield "data: " + json.dumps({"choices": [{"delta": {"content": reply}}]})
            else:
                for index, (name, args) in enumerate(reply):
                    yield "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                        {"index": index, "id": f"c{index}",
                         "function": {"name": name, "arguments": json.dumps(args)}}]}}]})
            yield "data: " + json.dumps({"choices": [], "usage": {"input_tokens": 10, "output_tokens": 5}})
            yield "data: [DONE]"

    return Scripted()


def workspace(**files):
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp())
    for name, body in files.items():
        (root / name.replace("__", ".")).write_text(body, encoding="utf-8")
    return root


def registry(root, *names, shell=False):
    return Registry.builtin(enabled=list(names),
                            context=ToolContext(workspace=root, allow_shell=shell))


def turn(transport, tools=None, *, budget=None, events=None, log=None, gate=None):
    log = log or Log()
    loop = AgentLoop(client=LLMClient(transport, "v/m"), model="v/m", log=log, tools=tools,
                     gate=gate if gate is not None else Gate(tools, judge=lambda actions: (
                         [(True, 0.95) for _ in actions], None)),
                     budget=budget or Budget(steps=4), events=events)
    out = {"deltas": [], "tools": [], "outcome": None}
    for event in loop.run():
        if "delta" in event:
            out["deltas"].append(event["delta"])
        elif "tool" in event:
            out["tools"].append(event["tool"])
        elif event.get("done"):
            out["outcome"] = event["outcome"]
    out["log"] = log
    return out


class TestTheLoop(unittest.TestCase):
    def test_a_model_reads_a_file_then_answers_from_what_it_found(self):
        root = workspace(notes__md="The rate is 41 percent.")
        transport = scripted([("read_file", {"path": "notes.md"})], "The rate is 41 percent.")
        out = turn(transport, registry(root, "read_file"))
        self.assertEqual(out["outcome"].text, "The rate is 41 percent.")
        self.assertEqual(out["outcome"].steps, 2, "one request to ask, one to answer")
        self.assertEqual(out["outcome"].tools_run, 1)
        # The result went back to the model as a tool message, not as prose,
        # and the answer came after it.
        roles = [m["role"] for m in out["log"].messages()]
        self.assertEqual(roles, ["assistant", "tool", "assistant"])
        self.assertIn("41 percent", out["log"].messages()[1]["content"])

    def test_offering_no_tools_is_exactly_one_request(self):
        transport = scripted("Just an answer.")
        out = turn(transport, None, budget=Budget(steps=1))
        self.assertEqual(out["outcome"].text, "Just an answer.")
        self.assertEqual(out["outcome"].steps, 1)
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn("tools", transport.requests[0][1], "no tool schemas when none are offered")

    def test_tool_schemas_travel_with_the_request(self):
        root = workspace(a__txt="x")
        transport = scripted("done")
        turn(transport, registry(root, "read_file", "write_file"))
        sent = transport.requests[0][1]["tools"]
        self.assertEqual({t["function"]["name"] for t in sent}, {"read_file", "write_file"})
        self.assertIn("path", sent[0]["function"]["parameters"]["properties"])

    def test_a_refused_call_is_told_to_the_model_and_the_turn_continues(self):
        root = workspace()
        gate = Gate(registry(root, "write_file"),
                    judge=lambda actions: ([(False, 0.2) for _ in actions], None))
        transport = scripted([("write_file", {"path": "x.md", "content": "no"})],
                             "I will not write that file.")
        out = turn(transport, registry(root, "write_file"), gate=gate)
        self.assertFalse(out["tools"][0]["allowed"])
        self.assertFalse((root / "x.md").exists(), "a refused call does not run")
        self.assertIn("refused", out["log"].messages()[-2]["content"])
        self.assertEqual(out["outcome"].text, "I will not write that file.")

    def test_an_unknown_tool_is_an_answerable_mistake_not_a_crash(self):
        root = workspace()
        transport = scripted([("teleport", {})], "Sorry, I used a tool that does not exist.")
        out = turn(transport, registry(root, "read_file"))
        self.assertEqual(out["tools"][0]["reason"], "unknown_tool")
        self.assertIn("no tool called", out["log"].messages()[-2]["content"])
        self.assertEqual(out["outcome"].stop_reason, "answered")

    def test_malformed_arguments_come_back_as_a_result_not_a_guess(self):
        calls = parse_tool_calls([{"id": "c0", "function": {"name": "read_file",
                                                            "arguments": "{not json"}}])
        self.assertTrue(calls[0].malformed)
        self.assertEqual(calls[0].arguments, {})

    def test_a_failed_tool_is_reported_and_the_model_carries_on(self):
        root = workspace()
        transport = scripted([("read_file", {"path": "missing.md"})], "That file is not there.")
        out = turn(transport, registry(root, "read_file"))
        self.assertEqual(out["outcome"].tools_run, 1)
        self.assertIn("no such file", out["log"].messages()[-2]["content"])
        self.assertEqual(out["outcome"].text, "That file is not there.")

    def test_a_model_that_never_stops_is_stopped_by_the_step_budget(self):
        root = workspace(a__txt="x")
        transport = scripted([("read_file", {"path": "a.txt"})])   # asks for ever
        out = turn(transport, registry(root, "read_file"), budget=Budget(steps=3))
        self.assertEqual(out["outcome"].steps, 3)
        self.assertEqual(out["outcome"].stop_reason, "step_budget")

    def test_the_tool_budget_stops_the_calls_and_says_so(self):
        root = workspace(a__txt="x")
        transport = scripted([("read_file", {"path": "a.txt"}), ("read_file", {"path": "a.txt"})])
        out = turn(transport, registry(root, "read_file"),
                   budget=Budget(steps=3, tool_calls=1))
        self.assertEqual(out["outcome"].stop_reason, "tool_budget")
        self.assertLessEqual(out["outcome"].tools_run, 1)

    def test_a_provider_failure_before_any_words_is_reported_as_failed(self):
        class Broken(FakeTransport):
            def post_stream(self, path, payload):
                raise ProviderError("upstream HTTP 502")
                yield  # pragma: no cover

        out = turn(Broken(), None, budget=Budget(steps=2))
        self.assertEqual(out["outcome"].stop_reason, "failed")
        self.assertFalse(out["outcome"].partial)

    def test_every_step_and_tool_is_on_the_event_record(self):
        root = workspace(a__txt="x")
        events = RunEvents()
        transport = scripted([("read_file", {"path": "a.txt"})], "done")
        turn(transport, registry(root, "read_file"), events=events)
        kinds = []
        while True:
            try:
                _, event = events._queue.get_nowait()
            except Exception:
                break
            kinds.append(event["kind"])
        self.assertEqual(kinds.count("step.started"), 2)
        self.assertIn("tool.called", kinds)
        self.assertIn("tool.result", kinds)


class TestTheLog(unittest.TestCase):
    def test_what_the_model_sees_is_what_the_log_says(self):
        log = Log()
        log.system("Be brief.")
        log.append("user", "Hello.")
        log.append(ASSISTANT, "", calls=[ToolCall(id="c1", name="now", arguments={})])
        log.append("tool", "2026-09-20", call_id="c1", name="now")
        roles = [m["role"] for m in log.messages()]
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(log.messages()[2]["tool_calls"][0]["function"]["name"], "now")

    def test_a_second_system_entry_replaces_the_first(self):
        log = Log()
        log.system("One.")
        log.system("Two.")
        systems = [m for m in log.messages() if m["role"] == "system"]
        self.assertEqual([m["content"] for m in systems], ["Two."])

    def test_compaction_shadows_the_old_and_keeps_it_on_the_record(self):
        log = Log()
        log.system("Be brief.")
        for i in range(10):
            log.append("user", f"question {i}")
            log.append(ASSISTANT, f"answer {i}")
        before = log.tokens()
        replacing = log.compactable(keep_last=4)
        entry = log.compact("Earlier: ten questions answered.", replacing)
        self.assertIsNotNone(entry)
        self.assertLess(log.tokens(), before)
        self.assertEqual(len(log.entries), 21 + 1, "nothing is deleted")
        contents = [m["content"] for m in log.messages()]
        self.assertIn("Earlier: ten questions answered.", contents)
        self.assertNotIn("question 0", contents)
        self.assertIn("question 8", contents, "the recent turns stay")

    def test_a_summary_never_splits_a_call_from_its_result(self):
        log = Log()
        log.append("user", "go")
        log.append(ASSISTANT, "", calls=[ToolCall(id="c1", name="now")])
        log.append("tool", "now", call_id="c1")
        for i in range(8):
            log.append("user", f"more {i}")
        replacing = log.compactable(keep_last=2)
        self.assertFalse(any(e.role == ASSISTANT and e.calls for e in replacing)
                         and not any(e.role == "tool" for e in replacing))


if __name__ == "__main__":
    unittest.main()


class TestThroughTheExecutor(unittest.TestCase):
    """The loop where it matters: a whole run that looks something up on disk."""

    def run_with(self, *turns, files=None, preset="write"):
        import tempfile
        from pathlib import Path

        from jevharness.config import Thresholds
        from jevharness.executor import Executor
        from jevharness.plan import Task
        from jevharness.planner import Planner, PlanCache
        from jevharness.providers import JevClient
        from jevharness.roster import CAPABILITY_DECISION, Credential, ModelSpec, Roster
        from jevharness.tools import Registry, ToolContext, preset as tools_preset
        from tests.fakes import score

        root = Path(tempfile.mkdtemp())
        for name, body in (files or {}).items():
            (root / name).write_text(body, encoding="utf-8")
        transport = scripted(*turns)
        def judge(payload):
            answers = {}
            for name in payload["questions"]:
                if name == CAPABILITY_DECISION:
                    answers[name] = score(2.0, 5, 0.9)
                elif name in ("__needs_research__", "__wants_app__", "__multi_step__"):
                    answers[name] = {"type": "noul", "noul": 0.05}
                else:                      # the gate: these actions are allowed
                    answers[name] = {"type": "noul", "noul": 0.95}
            return answers

        transport.decision_hook = judge
        roster = Roster(models=[ModelSpec(id="m", model="v/m", capability=2, price_out=0.1)],
                        credentials={"default": Credential("default", "sk-fake-000000000000")})
        roster.validate()
        executor = Executor(
            jev=JevClient(transport, "fake/jev"),
            llm_factory=lambda spec: LLMClient(transport, spec.model),
            planner=Planner(llm=LLMClient(transport, "v/m"), cache=PlanCache()),
            roster=roster, thresholds=Thresholds(), escalate_uncertain=False,
            tools=Registry.builtin(enabled=tools_preset(preset), context=ToolContext(workspace=root)),
        )
        return executor.run(Task(prompt="What does the note say?")), root, transport

    def test_a_run_reads_a_file_and_answers_from_it(self):
        result, _, _ = self.run_with(
            [("read_file", {"path": "note.md"})], "The note says the rate is 41 percent.",
            files={"note.md": "rate: 41 percent"})
        self.assertEqual(result.output, "The note says the rate is 41 percent.")
        self.assertEqual(result.turns[0]["tools_run"], 1)
        self.assertEqual(result.turns[0]["steps"], 2)

    def test_a_write_is_judged_before_it_happens_and_then_lands(self):
        result, root, transport = self.run_with(
            [("write_file", {"path": "out.md", "content": "done"})], "Saved it.",
            files={})
        self.assertEqual((root / "out.md").read_text(), "done")
        gated = [c for c in transport.decision_calls
                 if any(k.startswith("a") for k in (c.get("questions") or {}))]
        self.assertTrue(gated, "a write goes past Jev first")

    def test_the_read_preset_offers_no_way_to_write(self):
        result, root, _ = self.run_with(
            [("write_file", {"path": "out.md", "content": "no"})], "I cannot write here.",
            preset="read")
        self.assertFalse((root / "out.md").exists())
        self.assertEqual(result.output, "I cannot write here.")


class TestDelegation(unittest.TestCase):
    """Work handed to a model of its own: the caller gets the answer, not the trip."""

    def test_a_delegate_runs_on_its_own_model_and_returns_only_its_answer(self):
        import tempfile
        from pathlib import Path

        from jevharness.config import Thresholds
        from jevharness.events import RunEvents
        from jevharness.executor import Executor
        from jevharness.plan import Task
        from jevharness.planner import Planner, PlanCache
        from jevharness.providers import JevClient
        from jevharness.roster import Credential, ModelSpec, Roster
        from jevharness.tools import Registry, ToolContext

        transport = scripted("the delegate's answer")
        roster = Roster(models=[ModelSpec(id="cheap", model="v/cheap", capability=2, price_out=0.01),
                                ModelSpec(id="strong", model="v/strong", capability=4, price_out=1.0)],
                        credentials={"default": Credential("default", "sk-fake-000000000000")})
        roster.validate()
        registry = Registry.builtin(enabled=["delegate"],
                                    context=ToolContext(workspace=Path(tempfile.mkdtemp())))
        executor = Executor(
            jev=JevClient(transport, "fake/jev"),
            llm_factory=lambda spec: LLMClient(transport, spec.model),
            planner=Planner(llm=LLMClient(transport, "v/cheap"), cache=PlanCache()),
            roster=roster, thresholds=Thresholds(), tools=registry, events=RunEvents())
        text, model = executor._delegate("Summarise the report.", "", 4)
        self.assertEqual(text, "the delegate's answer")
        self.assertIn("strong", model, "the level asked for decides the model")

    def test_with_nobody_to_delegate_to_the_tool_says_so(self):
        import tempfile
        from pathlib import Path

        from jevharness.tools import Registry, ToolContext

        registry = Registry.builtin(enabled=["delegate"],
                                    context=ToolContext(workspace=Path(tempfile.mkdtemp())))
        result = registry.run("delegate", {"task": "do a thing"})
        self.assertFalse(result.ok)
        self.assertIn("nobody to delegate", result.detail)
