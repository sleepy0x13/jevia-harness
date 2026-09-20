import tempfile
import unittest
from pathlib import Path

from jevharness.store import Store, slug

RESULT = {
    "output": "## Overview\n\nThe answer.",
    "decisions": {"needs_reply": {"summary": "yes (p=0.91)"}},
    "subtasks": [
        {"title": "the criteria", "status": "done", "role": "analyst",
         "model_label": "Qwen3.7 Flash", "output": "criteria body"},
        {"title": "already covered", "status": "skipped", "output": ""},
    ],
    "evidence": {"sources": [{"title": "Docs", "url": "https://example.test/a"}]},
    "comparison": {"actual_cost": 0.00074, "verdict": "cheaper"},
    "elapsed_ms": 5123,
}


class TestSlug(unittest.TestCase):
    def test_it_keeps_chinese_and_drops_punctuation(self):
        self.assertEqual(slug("把这封邮件总结成决定和待办!"), "把这封邮件总结成决定和待办")

    def test_it_never_returns_an_empty_name(self):
        self.assertEqual(slug("!!!"), "task")
        self.assertEqual(slug(""), "task")

    def test_it_is_bounded(self):
        self.assertLessEqual(len(slug("word " * 60)), 40)


class TestSaving(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.store = Store(self.dir)

    def save(self, **over):
        kwargs = dict(chat_id="abc123", title="Compare two things",
                      prompt="Compare them.", state="the material", result=RESULT)
        kwargs.update(over)
        return self.store.save_turn(**kwargs)

    def test_the_transcript_lands_in_the_workspace(self):
        saved = self.save()
        self.assertTrue(saved.chat_path.is_file())
        self.assertEqual(saved.chat_path.parent.name, "chats")
        self.assertIn("compare-two-things", saved.chat_path.name)

    def test_each_step_and_the_answer_become_files(self):
        saved = self.save()
        names = sorted(p.name for p in saved.output_paths)
        self.assertEqual(names, ["01-the-criteria.md", "answer.md"])
        body = next(p for p in saved.output_paths if p.name.startswith("01")).read_text()
        self.assertIn("criteria body", body)

    def test_a_skipped_step_writes_no_file(self):
        self.assertTrue(all("already" not in p.name for p in self.save().output_paths))

    def test_the_transcript_carries_the_judgements_and_sources(self):
        text = self.save().chat_path.read_text(encoding="utf-8")
        self.assertIn("needs_reply", text)
        self.assertIn("yes (p=0.91)", text)
        self.assertIn("https://example.test/a", text)
        self.assertIn("analyst, Qwen3.7 Flash", text)

    def test_a_second_turn_appends_rather_than_replacing(self):
        first = self.save()
        self.save(prompt="And again.")
        text = first.chat_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("**Task**"), 2)
        self.assertEqual(text.count("# Compare two things"), 1, "one title, at the top")

    def test_long_material_is_truncated_not_dumped(self):
        text = self.save(state="x" * 9000).chat_path.read_text(encoding="utf-8")
        self.assertIn("more characters", text)
        self.assertLess(len(text), 6000)

    def test_chinese_titles_survive_into_the_filename(self):
        saved = self.save(title="把这封邮件总结成决定和待办")
        self.assertIn("把这封邮件总结成决定和待办", saved.chat_path.name)
        self.assertTrue(saved.chat_path.is_file())

    def test_without_a_workspace_nothing_is_written(self):
        self.assertIsNone(Store(None).save_turn(
            chat_id="a", title="t", prompt="p", state="", result=RESULT))

    def test_paths_are_reported_relative_to_the_workspace(self):
        row = self.save().to_dict(self.dir)
        self.assertTrue(row["chat"].startswith("chats/"))
        self.assertTrue(all(p.startswith("outputs/") for p in row["outputs"]))


class TestFileNamesFromTheCaller(unittest.TestCase):
    """The conversation id arrives from the browser, so it is reduced to
    something that can only be a file name before it reaches the disk."""

    def test_a_traversing_id_stays_inside_the_workspace(self):
        import tempfile
        from pathlib import Path

        workspace = Path(tempfile.mkdtemp())
        store = Store(workspace)
        saved = store.save_turn(chat_id="../../../../tmp/escaped", title="notes",
                                prompt="hi", state="", result={"output": "there"})
        self.assertIsNotNone(saved)
        self.assertEqual(saved.chat_path.parent, workspace / "chats")
        self.assertNotIn("..", str(saved.chat_path))
        self.assertFalse((workspace.parent / "tmp" / "escaped.md").exists())

    def test_an_empty_id_still_gets_a_name(self):
        import tempfile
        from pathlib import Path

        store = Store(Path(tempfile.mkdtemp()))
        self.assertTrue(store.chat_path("", "t").name.endswith("-session.md"))
