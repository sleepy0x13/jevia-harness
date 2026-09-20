"""vNext regression cases T01–T20, backend half. All offline.

Each case is named after its row in the requirement (§12). The interface half
— reducer, motion, rendering — is in scripts/ui_tests.mjs and runs under node.
Scripted answers exercise the policy; they are not claims about Jev.
"""
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

from jevharness import agent
from jevharness.agent import MAX_STATE_CHARS, Evidence, Researcher, section_id
from jevharness.config import Thresholds
from jevharness.errors import ProviderError
from jevharness.events import Cancelled, RunEvents, result_row
from jevharness.executor import Executor, SUBTASK_EVIDENCE_CHARS
from jevharness.plan import Research, Task
from jevharness.planner import Planner, PlanCache, plan_cache_key
from jevharness.providers import JevClient, LLMClient
from jevharness.questions import Noul
from jevharness.redact import redact
from jevharness.research import Hit, Page, Section
from jevharness.roster import CAPABILITY_DECISION, Credential, ModelSpec, Roster
from tests.fakes import FakeTransport, choice, noul, score

CAP = 5
SECRET = "sk-or-v1-0123456789abcdef0123456789abcdef"


def roster(*specs, downgrade=False):
    r = Roster(models=list(specs) or [
        ModelSpec(id="tiny", model="v/tiny", capability=1, price_out=0.01, label="tiny"),
        ModelSpec(id="mid", model="v/mid", capability=2, price_out=0.10, label="mid"),
        ModelSpec(id="big", model="v/big", capability=4, price_out=1.00, label="big"),
    ], credentials={"default": Credential("default", "sk-fake-000000000000")})
    r.allow_downgrade = downgrade
    r.validate()
    return r


def build(transport, *, specs=(), escalate=False, events=None, downgrade=False):
    return Executor(
        jev=JevClient(transport, "fake/jev"),
        llm_factory=lambda spec: LLMClient(transport, spec.model),
        planner=Planner(llm=LLMClient(transport, "v/mid"), cache=PlanCache()),
        roster=roster(*specs, downgrade=downgrade), thresholds=Thresholds(),
        escalate_uncertain=escalate, events=events or RunEvents(),
    )


def decision_events(stream_events):
    return [e for e in stream_events if e.get("type") == "decision_event"]


def kinds(events):
    return [e["kind"] for e in decision_events(events)]


def queued(run_events):
    out = []
    while True:
        try:
            _, event = run_events._queue.get_nowait()
        except queue.Empty:
            return out
        out.append(event)


def sections(n, url="https://e/p", size=300, tag="x"):
    return [Section(text=f"{tag} section {i} of {url} " + "y" * size, url=url, title=f"T{i}", index=i)
            for i in range(n)]


class TestT01StartedArrivesFirst(unittest.TestCase):
    def test_batch_started_is_read_while_the_provider_is_still_blocking(self):
        returned = {}

        class Slow(FakeTransport):
            def post_json(self, path, payload):
                if path.endswith("/alpha/decisions") and "__needs_research__" in payload["questions"]:
                    time.sleep(0.6)
                    returned["at"] = time.monotonic()
                return super().post_json(path, payload)

        transport = Slow(decision_hook=lambda p: {k: noul(0.1) for k in p["questions"]},
                         completions=["done"])
        seen = {}
        for event in build(transport).stream(Task(prompt="Write a haiku about rain.")):
            if event.get("kind") == "batch.started" and event.get("stage") == "assess":
                seen["at"] = time.monotonic()
        self.assertIn("at", seen)
        self.assertLess(seen["at"], returned["at"], "started must be on the wire before the answer")


class TestT02OneCallManyQuestions(unittest.TestCase):
    def test_eight_questions_are_one_call_and_one_charge(self):
        questions = {f"q{i}": Noul(instructions=f"Question {i}?") for i in range(8)}
        transport = FakeTransport(decision_hook=lambda p: {k: noul(0.9) for k in p["questions"]})
        events = list(build(transport).stream(Task(prompt="triage", state="x", questions=questions)))
        self.assertEqual(len(transport.decision_calls), 1)
        started = [e for e in decision_events(events) if e["kind"] == "batch.started"]
        done = [e for e in decision_events(events) if e["kind"] == "batch.completed"]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["payload"]["count"], 8)
        self.assertEqual(len(done[0]["payload"]["results"]), 8, "all eight settle together")
        charged = {e["usage"]["call_id"] for e in decision_events(events) if e.get("usage")}
        self.assertEqual(len(charged), 1)
        final = [e for e in decision_events(events) if e["kind"] == "run.completed"][0]["payload"]
        self.assertEqual((final["jev_calls"], final["jev_questions"], final["llm_calls"]), (1, 8, 0))


class ResearchCase(unittest.TestCase):
    def setUp(self):
        self._search, self._fetch = agent.search, agent.fetch_all
        self.round = 0

        def search(term, limit=8):
            self.round += 1
            return [Hit(title=f"t{i}", url=f"https://e/{self.round}/{i}", snippet="s", rank=i) for i in range(3)]

        agent.search = search
        agent.fetch_all = lambda urls, workers=4: [
            Page(url=u, title="T", text="", sections=sections(4, url=u, tag=f"r{self.round}")) for u in urls]

    def tearDown(self):
        agent.search, agent.fetch_all = self._search, self._fetch

    def researcher(self, hook):
        transport = FakeTransport(decision_hook=hook)
        return Researcher(JevClient(transport, "j"), Thresholds()), transport


class TestT03SufficiencyUsesWhatWasKept(ResearchCase):
    def test_nothing_kept_is_never_enough_whatever_the_old_score(self):
        r, transport = self.researcher(lambda p: {k: noul(0.9 if k == "__enough__" else 0.1)
                                                  for k in p["questions"]})
        evidence, _ = r.run("q", Research(rounds=1, top_k=2), queries=lambda *a: ["w"])
        self.assertTrue(evidence.empty)
        self.assertEqual(evidence.sufficiency, "insufficient")
        self.assertFalse(any("__enough__" in c["questions"] for c in transport.decision_calls),
                         "an empty delivered set is not sent for a sufficiency check at all")
        self.assertNotEqual(evidence.stop_reason, "sufficient")


class TestT04UnknownIsNotYes(ResearchCase):
    def test_a_missing_answer_is_unknown(self):
        r, _ = self.researcher(lambda p: {k: noul(0.9) for k in p["questions"] if k != "__enough__"})
        evidence, _ = r.run("q", Research(rounds=2, top_k=2), queries=lambda q, ev, n: [f"w{n}"])
        self.assertEqual(evidence.sufficiency, "unknown")
        self.assertEqual(evidence.rounds, 2, "unknown does not end the search as if it were enough")
        self.assertNotEqual(evidence.stop_reason, "sufficient")
        self.assertTrue(evidence.gaps)

    def test_a_failed_check_is_unknown(self):
        def hook(p):
            if "__enough__" in p["questions"]:
                raise ProviderError("upstream HTTP 500")
            return {k: noul(0.9) for k in p["questions"]}

        r, _ = self.researcher(hook)
        evidence, _ = r.run("q", Research(rounds=1, top_k=2), queries=lambda *a: ["w"])
        self.assertEqual(evidence.sufficiency, "unknown")


class TestT05NotAssessedIsCounted(unittest.TestCase):
    def test_sections_that_never_reached_jev_are_not_assessed(self):
        transport = FakeTransport(decision_hook=lambda p: {k: noul(0.1) for k in p["questions"]})
        r = Researcher(JevClient(transport, "j"), Thresholds())
        big = [Section(text=f"s{i} " + "z" * 3900, url=f"https://e/{i}", title=f"S{i}", index=0) for i in range(74)]
        verdicts, _ = r._pick_sections("q", big)
        submitted = [v for v in verdicts.values() if v.get("submitted")]
        self.assertLess(len(submitted), 74)
        statuses = [verdicts.get(section_id(s), {"status": "not_assessed"})["status"] for s in big]
        self.assertEqual(statuses.count("not_assessed"), 74 - len(submitted))
        self.assertEqual(statuses.count("excluded"), len(submitted), "only what was judged is excluded")
        for state in (c["state"] for c in transport.decision_calls):
            self.assertLessEqual(len(state), MAX_STATE_CHARS + 200)

    def test_counts_add_up_per_round(self):
        events = RunEvents()
        transport = FakeTransport(decision_hook=lambda p: {k: noul([0.9, 0.45, 0.1][int(k[1:]) % 3])
                                                           if k.startswith("s") else noul(0.9)
                                                           for k in p["questions"] if k != "s5"})
        search, fetch = agent.search, agent.fetch_all
        agent.search = lambda term, limit=8: [Hit("t", "https://e/1", "s", 0)]
        agent.fetch_all = lambda urls, workers=4: [Page(url=u, title="T", text="", sections=sections(9, url=u))
                                                   for u in urls]
        try:
            Researcher(JevClient(transport, "j"), Thresholds()).run(
                "q", Research(rounds=1, top_k=1), queries=lambda *a: ["w"], events=events)
        finally:
            agent.search, agent.fetch_all = search, fetch
        filtered = [e for e in queued(events) if e["kind"] == "evidence.filtered"][0]["payload"]
        c = filtered["counts"]
        self.assertEqual(c["discovered"], c["kept"] + c["review"] + c["excluded"] + c["not_assessed"])
        self.assertGreaterEqual(c["not_assessed"], 1, "a missing answer is not a no")
        self.assertEqual(len({i["id"] for i in filtered["items"]}), c["discovered"])


class TestT06CumulativeSnapshot(ResearchCase):
    def test_the_second_check_sees_both_rounds_and_matches_the_prompt(self):
        checks = []

        def hook(p):
            if "__enough__" in p["questions"]:
                checks.append(p["state"])
                return {"__enough__": noul(0.3 if len(checks) == 1 else 0.9)}
            return {k: noul(0.9) for k in p["questions"]}

        r, _ = self.researcher(hook)
        evidence, _ = r.run("q", Research(rounds=2, top_k=1), queries=lambda q, ev, n: [f"w{n}"])
        self.assertEqual(len(checks), 2)
        self.assertIn("r1 section", checks[1])
        self.assertIn("r2 section", checks[1])
        self.assertEqual(evidence.sufficiency, "sufficient")
        self.assertTrue(evidence.covers(MAX_STATE_CHARS),
                        "the checked set is exactly what a full-budget prompt receives")


class TestT07TruncatedDeliveryIsFlagged(unittest.TestCase):
    def test_a_smaller_budget_is_reported_as_not_rechecked(self):
        evidence = Evidence(sections=sections(40, size=600))
        evidence.checked_set_id = evidence.snapshot_id(MAX_STATE_CHARS)
        evidence.sufficiency = "sufficient"
        events = RunEvents()
        ex = build(FakeTransport(), events=events)
        ex._delivery_check(evidence, SUBTASK_EVIDENCE_CHARS, "generation", "step-1")
        rows = [e for e in queued(events) if e["kind"] == "evidence.checked"]
        self.assertEqual(rows[0]["payload"]["result"], "not_rechecked")
        self.assertFalse(evidence.covers(SUBTASK_EVIDENCE_CHARS))


class TestT08T09AnswerFields(unittest.TestCase):
    def test_noul_keeps_p_yes_and_no_invented_confidence(self):
        row = result_row("n", noul(0.02))
        self.assertEqual(row["probability_yes"], 0.02)
        self.assertIsNone(row["confidence"])

    def test_choice_keeps_selected_probability_and_confidence_apart(self):
        row = result_row("c", choice("a", {"a": 0.6, "b": 0.4}, 0.3))
        self.assertEqual(row["probabilities"]["a"], 0.6)
        self.assertEqual(row["confidence"], 0.3)

    def test_a_missing_field_stays_missing(self):
        row = result_row("c", {"type": "choice", "choice": "a"})
        self.assertIsNone(row["probabilities"])
        self.assertIsNone(row["confidence"])


class TestT10NoQualifiedModel(unittest.TestCase):
    def specs(self):
        return (ModelSpec(id="a", model="v/a", capability=1, price_out=0.01),
                ModelSpec(id="b", model="v/b", capability=2, price_out=0.02))

    def test_the_requirement_stands_and_nothing_runs(self):
        transport = FakeTransport(decision_hook=lambda p: {
            k: (score(4.0, CAP, 0.95) if k == CAPABILITY_DECISION else noul(0.1)) for k in p["questions"]},
            completions=["should never be written"])
        events = list(build(transport, specs=self.specs()).stream(Task(prompt="Write a haiku.")))
        result = events[-1]["result_object"]
        self.assertEqual(result.status, "needs_review")
        self.assertIn("routing.unavailable", kinds(events))
        unavailable = [e for e in decision_events(events) if e["kind"] == "routing.unavailable"][0]
        self.assertEqual(unavailable["payload"]["required_capability"], 4)
        self.assertEqual(unavailable["payload"]["best_available"], 2)
        self.assertEqual(transport.chat_calls, [], "no weaker model is quietly lit up")

    def test_with_permission_it_runs_labelled(self):
        transport = FakeTransport(decision_hook=lambda p: {
            k: (score(4.0, CAP, 0.95) if k == CAPABILITY_DECISION else noul(0.1)) for k in p["questions"]},
            completions=["written"])
        events = list(build(transport, specs=self.specs(), downgrade=True).stream(Task(prompt="Write a haiku.")))
        selected = [e for e in decision_events(events) if e["kind"] == "routing.selected"][-1]["payload"]
        self.assertTrue(selected["downgraded"])
        self.assertEqual(selected["required_capability"], 4)


class TestT11SkippedStepAfterAPlanningCall(unittest.TestCase):
    PLAN = json.dumps({"strategy": "decide", "answer_from": "generation",
                       "decisions": {"refund": {"type": "noul", "instructions": "Refund?"}},
                       "generation": {"instruction": "Reply."}})

    def test_the_run_is_not_zero_llm_calls(self):
        def hook(p):
            out = {}
            for k in p["questions"]:
                out[k] = (score(0.2, CAP, 0.9) if k == CAPABILITY_DECISION
                          else noul(0.8) if k == "__multi_step__" else noul(0.9 if k == "refund" else 0.1))
            return out

        transport = FakeTransport(decision_hook=hook, completions=[self.PLAN])
        events = list(build(transport).stream(Task(prompt="Is this a refund request?", state="x")))
        skipped = [e for e in decision_events(events) if e["kind"] == "generation.skipped"]
        self.assertEqual(skipped[0]["payload"]["scope"], "run")
        final = [e for e in decision_events(events) if e["kind"] == "run.completed"][0]["payload"]
        self.assertEqual(final["llm_calls"], 1, "the plan was written by a model")
        self.assertEqual(final["generation"], "skipped")


class TestT12CacheKeys(unittest.TestCase):
    def test_numbers_and_direction_do_not_collide_and_repeats_do(self):
        key = lambda p: plan_cache_key(Task(prompt=p), language="en")
        self.assertNotEqual(key("Write 3 case studies"), key("Write 30 case studies"))
        self.assertNotEqual(key("Translate English to French"), key("Translate French to English"))
        self.assertEqual(key("Write 3 case studies"), key("write 3 case  studies."))


class TestT13Ordering(unittest.TestCase):
    def test_parallel_runs_number_their_own_events(self):
        def one(out):
            transport = FakeTransport(decision_hook=lambda p: {k: noul(0.9) for k in p["questions"]})
            qs = {f"q{i}": Noul(instructions=f"Q{i}?") for i in range(3)}
            out.extend(decision_events(build(transport).stream(Task(prompt="t", state="x", questions=qs))))

        a, b = [], []
        threads = [threading.Thread(target=one, args=(x,)) for x in (a, b)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        for run in (a, b):
            self.assertEqual([e["seq"] for e in run], list(range(1, len(run) + 1)))
            self.assertEqual(len({e["run_id"] for e in run}), 1)
            self.assertEqual(len({e["event_id"] for e in run}), len(run))
        self.assertNotEqual(a[0]["run_id"], b[0]["run_id"])

    def test_every_batch_has_exactly_one_final_state(self):
        transport = FakeTransport(decision_hook=lambda p: {k: noul(0.1) for k in p["questions"]},
                                  completions=["x"])
        events = decision_events(build(transport).stream(Task(prompt="Write a haiku.")))
        started = {e["batch_id"] for e in events if e["kind"] == "batch.started"}
        ended = [e["batch_id"] for e in events if e["kind"] in ("batch.completed", "batch.failed")]
        self.assertEqual(sorted(started), sorted(ended))


class TestT17FailureBeforeAndDuringOutput(unittest.TestCase):
    def hook(self, p):
        return {k: (score(2.0, CAP, 0.9) if k == CAPABILITY_DECISION else noul(0.1)) for k in p["questions"]}

    def test_failure_before_output_moves_to_another_model(self):
        class FailFirst(FakeTransport):
            def post_stream(self, path, payload):
                if payload["model"] == "v/mid":
                    self.requests.append((path, payload))
                    raise ProviderError("upstream HTTP 403", detail={"message": "region"})
                yield from super().post_stream(path, payload)

        transport = FailFirst(decision_hook=self.hook, completions=["from the substitute"])
        specs = (ModelSpec(id="mid", model="v/mid", capability=2, price_out=0.1),
                 ModelSpec(id="alt", model="v/alt", capability=2, price_out=0.2))
        events = list(build(transport, specs=specs).stream(Task(prompt="Write a haiku.")))
        result = events[-1]["result_object"]
        self.assertEqual(result.output, "from the substitute")
        rules = [e["payload"]["rule"] for e in decision_events(events) if e["kind"] == "policy.applied"]
        self.assertIn("failover.before_output/v1", rules)
        attempts = {e["attempt_id"] for e in decision_events(events) if e["kind"].startswith("llm.")}
        self.assertIn("attempt-2", attempts, "the retry is a new attempt, the first is kept")

    def test_failure_mid_output_keeps_the_words_and_does_not_retry(self):
        class Midway(FakeTransport):
            def post_stream(self, path, payload):
                self.requests.append((path, payload))
                yield 'data: {"choices":[{"delta":{"content":"half an ans"}}]}'
                raise ProviderError("upstream HTTP 502")

        transport = Midway(decision_hook=self.hook)
        events = list(build(transport).stream(Task(prompt="Write a haiku.")))
        result = events[-1]["result_object"]
        self.assertEqual(result.output, "half an ans")
        self.assertTrue(result.partial)
        streams = [p for path, p in transport.requests if path.endswith("/chat/completions") and p.get("stream")]
        self.assertEqual(len(streams), 1, "no automatic second attempt after words were shown")


class TestT18Cancel(unittest.TestCase):
    def test_no_new_step_after_a_cancel_and_no_claim_that_billing_stopped(self):
        events = RunEvents()

        def hook(p):
            if "__needs_research__" in p["questions"]:
                events.request_cancel("user")     # the user stops while Jev is answering
            return {k: noul(0.1) for k in p["questions"]}

        transport = FakeTransport(decision_hook=hook, completions=["never"])
        out = list(build(transport, events=events).stream(Task(prompt="Write a haiku.")))
        self.assertEqual(transport.chat_calls, [])
        self.assertIn("run.cancel_requested", kinds(out))
        cancelled = [e for e in decision_events(out) if e["kind"] == "run.cancelled"][0]["payload"]
        self.assertIn("may_bill", cancelled)
        self.assertEqual(out[-1]["result"]["status"], "cancelled")

    def test_check_raises_once_asked(self):
        events = RunEvents()
        events.request_cancel()
        with self.assertRaises(Cancelled):
            events.check()


class TestT20Redaction(unittest.TestCase):
    def test_keys_and_headers_never_reach_the_record(self):
        folder = Path(tempfile.mkdtemp())
        events = RunEvents(secrets=[SECRET], log_dir=folder)
        events.emit("policy.applied", "policy", "plan",
                    {"rule": "x", "detail": f"Authorization: Bearer {SECRET} cookie=abc123456",
                     "inputs": {"api_key": SECRET, "note": "key " + SECRET}, "raw_body": "dropped"})
        stamped = [events.stamp(e) for e in queued(events)]
        text = json.dumps(stamped) + (folder / f"{events.run_id}.jsonl").read_text()
        self.assertNotIn(SECRET, text)
        self.assertNotIn("abc123456", text)
        self.assertNotIn("raw_body", text, "fields outside the whitelist are dropped")
        self.assertNotIn("api_key", text)

    def test_a_provider_error_body_is_not_passed_on(self):
        exc = ProviderError("upstream HTTP 401", detail={"echo": f"Authorization: Bearer {SECRET}"})
        self.assertNotIn(SECRET, json.dumps(exc.public_dict()))
        self.assertNotIn("detail", exc.public_dict()["error"])

    def test_simulated_keys_are_masked_in_free_text(self):
        for sample in (SECRET, "sk-proj-AAAAAAAAAAAAAAAAAAAA", "Bearer abcdefghijklmnop",
                       'x-api-key: "zzzzzzzzzz"'):
            self.assertIn("[redacted]", redact(f"before {sample} after"))


class TestFixturesAreProtocolClean(unittest.TestCase):
    """The Demo replay fixtures come from the real executor and carry no key."""

    def test_every_fixture_event_is_versioned_ordered_and_whitelisted(self):
        from jevharness.events import EVENT_VERSION, KIND_FIELDS

        folder = Path(__file__).resolve().parent.parent / "jevharness" / "ui" / "fixtures"
        index = json.loads((folder / "index.json").read_text())
        self.assertGreaterEqual(len(index["fixtures"]), 5)
        for row in index["fixtures"]:
            data = json.loads((folder / row["file"]).read_text())
            self.assertTrue(data["synthetic"])
            seqs = [e["seq"] for e in data["events"]]
            self.assertEqual(seqs, list(range(1, len(seqs) + 1)), row["name"])
            for e in data["events"]:
                self.assertEqual(e["event_version"], EVENT_VERSION)
                self.assertTrue(set(e["payload"]) <= KIND_FIELDS[e["kind"]], (row["name"], e["kind"]))
            self.assertNotIn("sk-", json.dumps(data))


if __name__ == "__main__":
    unittest.main()


class TestStepsAreNotBlockedByTheWholeAnswer(unittest.TestCase):
    """Found in the offline browser check: an unqualified whole-answer pick held
    back steps that each had a qualified model of their own."""

    PLAN = json.dumps({"strategy": "parts", "answer_from": "generation",
                       "steps": [{"title": "one", "role": "writer"}, {"title": "two", "role": "writer"}],
                       "decisions": {}, "generation": {"instruction": "Write it."}})

    def test_steps_run_when_only_the_answer_sizing_finds_no_model(self):
        def hook(p):
            out = {}
            for k in p["questions"]:
                if k == CAPABILITY_DECISION:
                    out[k] = score(4.0, CAP, 0.95)          # the whole: level 4
                elif k.startswith("c"):
                    out[k] = score(2.0, CAP, 0.95)          # each step: level 2
                elif k == "__multi_step__":
                    out[k] = noul(0.9)
                else:
                    out[k] = noul(0.1)
            return out

        specs = (ModelSpec(id="a", model="v/a", capability=2, price_out=0.01),
                 ModelSpec(id="b", model="v/b", capability=3, price_out=0.02))
        transport = FakeTransport(decision_hook=hook, completions=[self.PLAN, "part one", "part two"])
        events = list(build(transport, specs=specs).stream(Task(prompt="Write two parts, then join them.")))
        result = events[-1]["result_object"]
        self.assertEqual([t.status for t in result.subtasks], ["done", "done"])
        self.assertNotEqual(result.status, "needs_review")
        self.assertNotIn("routing.unavailable", kinds(events))


class TestEvaluationRecords(unittest.TestCase):
    """P2 entry point: a record format filled from recorded runs, offline."""

    def test_a_fixture_summarises_without_calling_anything(self):
        from jevharness.evaluation import record_from_events

        folder = Path(__file__).resolve().parent.parent / "jevharness" / "ui" / "fixtures"
        events = json.loads((folder / "decide-no-generate.json").read_text())["events"]
        record = record_from_events(events)
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.llm_calls, 1)
        self.assertIsNone(record.usable, "usefulness is judged later, never guessed")


class TestSyncRewritesOnlyTheWhole(unittest.TestCase):
    """R02: syncing a stale whole runs the assembly step and nothing else."""

    def test_only_one_call_is_made_and_the_new_version_names_its_parts(self):
        transport = FakeTransport(completions=["the whole, rewritten"])
        ex = build(transport)
        parts = [{"id": "step-a", "title": "One", "output": "first part", "version": 2, "required": 2},
                 {"id": "step-b", "title": "Two", "output": "second part", "version": 1, "required": 2}]
        events = list(ex.sync_answer(Task(prompt="Write it up."), parts))
        done = [e for e in events if e.get("type") == "done"][0]
        self.assertEqual(done["output"], "the whole, rewritten")
        self.assertEqual(done["depends_on"], ["step-a@v2", "step-b@v1"])
        self.assertEqual(len(transport.chat_calls), 1, "no step is written again")
        self.assertEqual(transport.decision_calls, [], "nothing is judged again")
        version = [e for e in decision_events(events) if e["kind"] == "output.version"][0]
        self.assertFalse(version["payload"]["needs_sync"])
