import tempfile
import unittest
from pathlib import Path

from jevharness.skills import Library, SkillError, fetch_text, parse

GOOD = "---\nname: Meeting notes\ndescription: Turning a call into notes.\n---\n\nKeep it short."


class TestManaging(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.lib = Library(self.dir)

    def test_builtins_are_listed_and_not_editable(self):
        rows = {r["id"]: r for r in self.lib.catalogue()}
        self.assertIn("code-review", rows)
        self.assertFalse(rows["code-review"]["editable"])

    def test_a_saved_skill_lands_in_the_workspace(self):
        skill = self.lib.save(GOOD)
        self.assertEqual(skill.id, "meeting-notes")
        self.assertTrue((self.dir / "skills" / "meeting-notes.md").is_file())
        self.assertTrue(Library(self.dir).get("meeting-notes").to_dict()["editable"])

    def test_editing_a_builtin_saves_a_copy_that_wins(self):
        text = self.lib.get("code-review").raw() + "\nExtra rule."
        self.lib.save(text, "code-review")
        reloaded = Library(self.dir).get("code-review")
        self.assertEqual(reloaded.source, "workspace")
        self.assertIn("Extra rule.", reloaded.body)

    def test_deleting_the_copy_restores_the_builtin(self):
        self.lib.save(self.lib.get("code-review").raw(), "code-review")
        self.assertTrue(self.lib.delete("code-review"))
        self.assertEqual(self.lib.get("code-review").source, "builtin")

    def test_a_builtin_itself_cannot_be_deleted(self):
        self.assertFalse(self.lib.delete("code-review"))

    def test_a_file_without_a_header_is_refused(self):
        with self.assertRaises(SkillError):
            self.lib.save("just some text")

    def test_without_a_workspace_nothing_can_be_saved(self):
        with self.assertRaises(SkillError):
            Library(None).save(GOOD)

    def test_the_skill_md_folder_layout_is_recognised(self):
        folder = self.dir / "skills" / "pr-helper"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            "---\nname: PR helper\ndescription: Writing PR descriptions.\n---\nBe specific.")
        self.assertIsNotNone(Library(self.dir).get("pr-helper"))

    def test_raw_round_trips_through_parse(self):
        raw = self.lib.get("summarise").raw()
        again = parse(raw, "summarise")
        self.assertEqual(again.name, self.lib.get("summarise").name)


class TestInstalling(unittest.TestCase):
    def test_private_and_non_http_links_are_refused(self):
        for url in ("http://localhost/skill.md", "file:///etc/passwd", "ftp://x.test/a.md",
                    "http://169.254.169.254/latest"):
            with self.assertRaises(SkillError, msg=url):
                fetch_text(url)

    def opener(self, body, final="https://raw.githubusercontent.com/x"):
        """A stand-in for the redirect-checking opener every fetch now uses."""
        from unittest import mock

        response = mock.MagicMock()
        inner = response.__enter__.return_value
        inner.read.return_value = body
        inner.geturl.return_value = final
        opener = mock.MagicMock()
        opener.open.return_value = response
        return opener

    def test_github_page_links_become_raw_links(self):
        from unittest import mock

        opener = self.opener(GOOD.encode())
        with mock.patch("jevharness.tools._is_public", return_value=True), \
                mock.patch("jevharness.tools.safe_opener", return_value=opener):
            text = fetch_text("https://github.com/acme/tools/blob/main/skills/x/SKILL.md")
        request = opener.open.call_args[0][0]
        self.assertEqual(request.full_url,
                         "https://raw.githubusercontent.com/acme/tools/main/skills/x/SKILL.md")
        self.assertIn("Meeting notes", text)

    def test_an_oversized_download_is_refused(self):
        from unittest import mock

        with mock.patch("jevharness.tools._is_public", return_value=True), \
                mock.patch("jevharness.tools.safe_opener", return_value=self.opener(b"x" * 70_000)):
            with self.assertRaises(SkillError):
                fetch_text("https://example.test/huge.md")

    def test_a_redirect_onto_a_private_host_is_refused(self):
        from unittest import mock

        opener = self.opener(GOOD.encode(), final="http://169.254.169.254/latest/meta-data")
        with mock.patch("jevharness.tools.safe_opener", return_value=opener):
            with self.assertRaises(SkillError):
                fetch_text("https://example.test/skill.md")


if __name__ == "__main__":
    unittest.main()


class TestUploads(unittest.TestCase):
    """A skill file the user picked: markdown, or a zip of markdown. Read, never run."""

    def library(self):
        import tempfile
        from pathlib import Path

        return Library(Path(tempfile.mkdtemp()))

    def zip_of(self, entries):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for name, body in entries.items():
                archive.writestr(name, body)
        return buf.getvalue()

    def test_a_markdown_file_becomes_a_skill(self):
        library = self.library()
        added, ignored = library.install_file(
            "tone.md", b"---\nname: Tone\ndescription: Checking tone.\n---\n\nBe kind.\n")
        self.assertEqual([s.id for s in added], ["tone"])
        self.assertEqual(ignored, [])

    def test_a_zip_installs_every_skill_in_it_and_reports_the_rest(self):
        library = self.library()
        added, ignored = library.install_file("pack.zip", self.zip_of({
            "pack/one.md": "---\nname: One\ndescription: First.\n---\nBody",
            "pack/two.md": "---\nname: Two\ndescription: Second.\n---\nBody",
            "pack/logo.png": "not markdown",
        }))
        self.assertEqual(sorted(s.id for s in added), ["one", "two"])
        self.assertEqual(ignored, ["logo.png"])

    def test_a_path_in_the_archive_cannot_write_outside_the_skills_folder(self):
        library = self.library()
        added, _ = library.install_file("evil.zip", self.zip_of({
            "../../../evil.md": "---\nname: Evil\ndescription: x\n---\nBody",
            "/tmp/also-evil.md": "---\nname: Also\ndescription: x\n---\nBody",
        }))
        for skill in added:
            self.assertEqual(skill.path.parent, library.folder)
        self.assertFalse((library.workspace.parent / "evil.md").exists())

    def test_a_zip_that_unpacks_to_too_much_is_refused(self):
        library = self.library()
        with self.assertRaises(SkillError):
            library.install_file("bomb.zip", self.zip_of({"big.md": "x" * 9_000_000}))

    def test_a_file_that_is_not_text_is_refused(self):
        library = self.library()
        with self.assertRaises(SkillError):
            library.install_file("skill.md", b"\xff\xfe\x00binary")


class TestHeadersFromOtherHarnesses(unittest.TestCase):
    """Packs written elsewhere put several paragraphs under one YAML key and
    name the skill with its own id. Reading only the first line left them with
    no description, so they appeared nameless in the library and the picker."""

    BLOCK = """---
name: hv-analysis
description: |
  横纵分析法深度研究 Skill。
  当用户想要系统性研究一个产品、公司或概念时使用。
when: >
  A product or company
  to research in depth.
---

The instructions.
"""

    def test_a_block_description_is_read_whole(self):
        skill = parse(self.BLOCK, "hv-analysis", "workspace")
        self.assertIn("横纵分析法深度研究 Skill。", skill.description)
        self.assertIn("当用户想要系统性研究", skill.description)
        self.assertEqual(skill.when, "A product or company to research in depth.")

    def test_an_id_shaped_name_is_written_out_for_display(self):
        self.assertEqual(parse(self.BLOCK, "hv-analysis", "workspace").name, "Hv Analysis")

    def test_a_long_description_gets_a_one_line_summary(self):
        row = parse(self.BLOCK, "hv-analysis", "workspace").to_dict()
        self.assertTrue(row["summary"])
        self.assertLessEqual(len(row["summary"]), len(row["description"]))

    def test_a_plain_header_still_reads_as_before(self):
        skill = parse(GOOD, "meeting-notes", "workspace")
        self.assertEqual(skill.name, "Meeting notes")
        self.assertEqual(skill.description, "Turning a call into notes.")
