import json
import tempfile
import unittest
from pathlib import Path

from jevharness.agent import assess
from jevharness.memory import Memory
from jevharness.providers import JevClient
from tests.fakes import FakeTransport, noul


class TestCandidates(unittest.TestCase):
    def test_english_statements_about_the_user_are_caught(self):
        for text in ("I always write in British English.",
                     "We prefer short replies.",
                     "My company is Acme and we use Go.",
                     "Remember that our customers are hospitals."):
            self.assertTrue(Memory.candidates(text), text)

    def test_chinese_statements_are_caught(self):
        for text in ("我们团队用的是 Go", "我一般喜欢简短的回复", "以后都用正式语气", "记住我叫 Sleepy"):
            self.assertTrue(Memory.candidates(text), text)

    def test_plain_task_wording_is_not_a_candidate(self):
        for text in ("Summarise this email.", "总结这封邮件", "Review this diff"):
            self.assertEqual(Memory.candidates(text), [], text)

    def test_candidates_are_capped_and_deduplicated(self):
        text = " ".join(["I prefer short replies."] * 9)
        self.assertEqual(len(Memory.candidates(text)), 1)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.memory = Memory(self.dir)

    def test_a_fact_survives_a_restart(self):
        self.memory.add("I write in British English.")
        self.assertEqual(len(Memory(self.dir).facts), 1)

    def test_duplicates_are_not_stored_twice(self):
        self.assertTrue(self.memory.add("Our stack is Go."))
        self.assertFalse(self.memory.add("our stack is go."))
        self.assertEqual(len(self.memory.facts), 1)

    def test_it_is_a_readable_file_the_user_can_edit(self):
        self.memory.add("Our stack is Go.")
        path = self.dir / ".jevia" / "memory.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        self.assertEqual(rows[0]["text"], "Our stack is Go.")

    def test_forgetting_works(self):
        self.memory.add("Our stack is Go.")
        self.assertTrue(self.memory.forget("Our stack is Go."))
        self.assertEqual(Memory(self.dir).facts, [])

    def test_without_a_workspace_nothing_is_stored(self):
        loose = Memory(None)
        self.assertFalse(loose.enabled)
        self.assertFalse(loose.add("anything"))
        self.assertEqual(loose.block(), "")

    def test_the_block_prefers_facts_that_get_used(self):
        self.memory.add("A")
        self.memory.add("B")
        self.memory.facts[1].used = 5
        self.assertTrue(self.memory.block().startswith("- B"))


class TestJudging(unittest.TestCase):
    """Remembering rides in the assess call, so it costs nothing of its own."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.memory = Memory(self.dir)

    def hook(self, keep):
        def respond(payload):
            return {name: noul(keep if name.startswith("m") else 0.1)
                    for name in payload["questions"]}

        return respond

    def test_a_durable_fact_is_kept(self):
        transport = FakeTransport(decision_hook=self.hook(0.95))
        assess(JevClient(transport, "j"), "I always write in British English. Summarise this.",
               "", None, self.memory)
        self.assertEqual(len(transport.decision_calls), 1, "no extra call for memory")
        self.assertEqual([f.text for f in self.memory.facts], ["I always write in British English"])

    def test_a_one_off_instruction_is_not_kept(self):
        transport = FakeTransport(decision_hook=self.hook(0.2))
        assess(JevClient(transport, "j"), "I need this by Friday. Summarise this.",
               "", None, self.memory)
        self.assertEqual(self.memory.facts, [])

    def test_a_task_with_nothing_to_remember_asks_nothing_extra(self):
        transport = FakeTransport(decision_hook=self.hook(0.9))
        assess(JevClient(transport, "j"), "Summarise this email.", "", None, self.memory)
        self.assertFalse(any(q.startswith("m") for q in transport.decision_calls[0]["questions"]))


if __name__ == "__main__":
    unittest.main()
