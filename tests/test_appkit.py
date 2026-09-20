import json
import tempfile
import unittest
from pathlib import Path

from jevharness.appkit import CATALOGUE, Composition, _parse_spec, compose, render
from jevharness.config import Thresholds
from jevharness.executor import Executor
from jevharness.plan import Task
from jevharness.planner import PlanCache, Planner
from jevharness.providers import JevClient, LLMClient
from jevharness.roster import Credential, ModelSpec, Roster
from jevharness.store import Store
from tests.fakes import FakeTransport, choice, noul, score

TRAIN = json.dumps({
    "title": "Train tickets",
    "components": [
        {"type": "hero", "props": {"title": "Shanghai → Beijing", "subtitle": "Pick a train"}},
        {"type": "form", "props": {"title": "Search", "submit": "Find",
                                   "fields": [{"name": "date", "label": "Date", "type": "date"}]}},
        {"type": "table", "props": {"title": "Trains", "columns": ["No.", "Departs"],
                                    "rows": [["G1", "07:00"], ["G3", "08:00"]]}},
    ],
})


def jev_for(use, layout="single", side=(), positions=None, wants_app=0.95, kind="compose"):
    """Answers the compose call from a set of wanted components."""
    positions = positions or {}

    def respond(payload):
        out = {}
        for name in payload.get("questions") or {}:
            if name == "__wants_app__":
                out[name] = noul(wants_app)
            elif name == "__needs_research__":
                out[name] = noul(0.05)
            elif name.startswith("use_"):
                out[name] = noul(0.9 if name[4:] in use else 0.1)
            elif name.startswith("pos_"):
                out[name] = score(positions.get(name[4:], 2.0), 5, 0.9)
            elif name == "layout":
                out[name] = choice(layout, {layout: 0.9}, 0.9)
            elif name == "kind":
                out[name] = choice(kind, {kind: 0.9}, 0.9)
            elif name.startswith("side_"):
                out[name] = noul(0.9 if name[5:] in side else 0.1)
            else:
                out[name] = noul(0.5)
        return out

    return respond


def executor_with(transport, app_mode="auto"):
    roster = Roster(
        models=[ModelSpec(id="tiny", model="v/tiny", capability=1, price_out=0.01, label="tiny"),
                ModelSpec(id="mid", model="v/mid", capability=2, price_out=0.10, label="mid"),
                ModelSpec(id="big", model="v/big", capability=4, price_out=1.00, label="big")],
        credentials={"default": Credential("default", "sk-fake-000000000000")})
    roster.validate()
    return Executor(
        jev=JevClient(transport, "fake/jev"),
        llm_factory=lambda spec: LLMClient(transport, spec.model),
        planner=Planner(llm=LLMClient(transport, "v/mid"), cache=PlanCache()),
        roster=roster,
        thresholds=Thresholds(),
        app_mode=app_mode,
    )


class TestCompose(unittest.TestCase):
    def test_every_component_is_decided_in_one_call(self):
        transport = FakeTransport(decision_hook=jev_for({"hero", "form", "table"}))
        composition, calls = compose(JevClient(transport, "j"), "a train booking page")
        self.assertEqual(len(calls), 1)
        asked = transport.decision_calls[0]["questions"]
        self.assertEqual(len(asked), len(CATALOGUE) * 2 + 2, "use+where per part, layout, kind")
        self.assertEqual(set(composition.components), {"hero", "form", "table"})

    def test_position_orders_the_page_and_the_title_leads(self):
        transport = FakeTransport(decision_hook=jev_for(
            {"hero", "form", "table"}, positions={"hero": 4.0, "table": 0.0, "form": 3.0}))
        composition, _ = compose(JevClient(transport, "j"), "x")
        self.assertEqual(composition.components, ["hero", "table", "form"])

    def test_a_second_call_only_for_a_split_layout(self):
        transport = FakeTransport(decision_hook=jev_for(
            {"hero", "form", "table"}, layout="split", side={"form"}))
        composition, calls = compose(JevClient(transport, "j"), "x")
        self.assertEqual(len(calls), 2)
        self.assertEqual(composition.side, ["form"])
        self.assertEqual(composition.layout, "split")

    def test_a_split_with_one_piece_is_a_single_column(self):
        transport = FakeTransport(decision_hook=jev_for({"hero", "form"}, layout="split"))
        composition, calls = compose(JevClient(transport, "j"), "x")
        self.assertEqual(len(calls), 1)
        self.assertEqual(composition.layout, "single")

    def test_everything_in_the_side_panel_keeps_something_in_the_main_area(self):
        transport = FakeTransport(decision_hook=jev_for(
            {"form", "table"}, layout="split", side={"form", "table"}))
        composition, _ = compose(JevClient(transport, "j"), "x")
        self.assertEqual(len(composition.side), 1)

    def test_nothing_chosen_still_yields_a_page(self):
        transport = FakeTransport(decision_hook=jev_for(set()))
        composition, _ = compose(JevClient(transport, "j"), "x")
        self.assertEqual(len(composition.components), 2)


class TestWrittenApps(unittest.TestCase):
    GAME = ("<!doctype html><html><head><title>Flappy</title><style>body{margin:0}</style></head>"
            "<body><canvas></canvas><script>" + "let x=0;" * 60 + "</script></body></html>")

    def test_a_game_is_written_not_assembled_and_never_shown_as_code(self):
        transport = FakeTransport(decision_hook=jev_for(set(), kind="code"),
                                  completions=["```html\n" + self.GAME + "\n```"])
        result = executor_with(transport).run(Task(prompt="做一个 flappy bird 游戏"))
        self.assertEqual([s.code for s in result.steps], ["assess", "plan_free", "compose", "app_write"])
        self.assertEqual(result.app["title"], "Flappy")
        self.assertTrue(result.app["html"].startswith("<!doctype html>"))
        self.assertNotIn("<script", result.output)
        self.assertEqual(transport.chat_calls[0]["model"], "v/big", "code goes to the rung that can write it")

    def test_a_page_in_an_ordinary_answer_becomes_an_app(self):
        transport = FakeTransport(decision_hook=jev_for(set(), wants_app=0.1),
                                  completions=['{"strategy":"s","answer_from":"generation",'
                                               '"generation":{"instruction":"answer"}}',
                                               "Here it is:\n```html\n" + self.GAME + "\n```\nEnjoy."])
        result = executor_with(transport).run(Task(prompt="snake game please"))
        self.assertIsNotNone(result.app)
        self.assertNotIn("<canvas", result.output)
        self.assertIn("Enjoy.", result.output)

    def test_the_parts_of_a_written_page_are_named_as_they_arrive(self):
        from jevharness.appkit import find_modules
        code = "<style>x</style><canvas></canvas><script>function drawBird(){} const tick = () => 1;"
        self.assertEqual(find_modules(code), ["style", "canvas", "drawBird", "tick"])
        self.assertEqual(find_modules(code, ["style", "canvas"]), ["drawBird", "tick"])

    def test_writing_streams_part_names_but_never_code(self):
        big = self.GAME.replace("</script>", "function drawBird(){}" + " " * 700 + "function flap(){}</script>")
        transport = FakeTransport(decision_hook=jev_for(set(), kind="code"), completions=[big])
        events = list(executor_with(transport).stream(Task(prompt="make a game")))
        names = [e["name"] for e in events if e["type"] == "app_module"]
        self.assertIn("drawBird", names)
        self.assertFalse(any(e["type"] == "delta" for e in events), "the code is never streamed to the reader")

    def test_rendered_blocks_know_their_order(self):
        comp = Composition(components=["hero", "table"])
        page = render(_parse_spec(TRAIN, comp), comp)
        self.assertIn('style="--i:0"', page)
        self.assertIn('style="--i:1"', page)

    def test_a_snippet_is_not_mistaken_for_an_app(self):
        from jevharness.appkit import extract_page
        self.assertEqual(extract_page("Use `<div class=x>` and a `<script>` tag."), "")


class TestFill(unittest.TestCase):
    def test_code_fences_and_prose_around_the_json_are_tolerated(self):
        comp = Composition(components=["hero"])
        spec = _parse_spec('Sure!\n```json\n{"title":"T","components":[{"type":"hero","props":{"title":"Hi"}}]}\n```', comp)
        self.assertEqual(spec["title"], "T")
        self.assertEqual(spec["components"][0]["props"]["title"], "Hi")

    def test_components_nobody_asked_for_are_dropped_and_order_is_jevs(self):
        comp = Composition(components=["table", "hero"])
        spec = _parse_spec(TRAIN, comp)
        self.assertEqual([c["type"] for c in spec["components"]], ["table", "hero"])

    def test_broken_json_gives_an_empty_page_not_a_crash(self):
        spec = _parse_spec("{not json", Composition(components=["hero"]))
        self.assertEqual(spec["components"], [])


class TestRender(unittest.TestCase):
    def test_model_text_cannot_inject_markup(self):
        comp = Composition(components=["hero", "table"])
        spec = {"title": "<b>t</b>", "components": [
            {"type": "hero", "props": {"title": "<script>alert(1)</script>"}},
            {"type": "table", "props": {"columns": ['"><img src=x onerror=1>'], "rows": [["<i>"]]}},
        ]}
        page = render(spec, comp)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;", page)

    def test_split_layout_has_a_main_and_a_side_column(self):
        comp = Composition(components=["hero", "form", "table"], layout="split", side=["form"])
        page = render(_parse_spec(TRAIN, comp), comp)
        self.assertEqual(page.count('class="col"'), 2)
        self.assertLess(page.index("<table>"), page.index("<form"), "main column comes first")

    def test_unknown_or_malformed_blocks_are_skipped(self):
        comp = Composition(components=["chart", "stats"])
        spec = {"title": "x", "components": [
            {"type": "chart", "props": {"series": "not a list"}},
            {"type": "stats", "props": {"items": [{"label": "Users", "value": "12"}]}},
            {"type": "iframe", "props": {"src": "http://evil"}},
        ]}
        page = render(spec, comp)
        self.assertIn("Users", page)
        self.assertNotIn("evil", page)

    def test_every_catalogue_entry_has_a_renderer(self):
        from jevharness.appkit import RENDERERS
        self.assertEqual({c.id for c in CATALOGUE}, set(RENDERERS))


class TestAppRuns(unittest.TestCase):
    def test_jev_spots_an_app_and_the_planner_is_never_called(self):
        transport = FakeTransport(decision_hook=jev_for({"hero", "form", "table"}),
                                  completions=[TRAIN])
        result = executor_with(transport).run(Task(prompt="Build me a train ticket booking page"))
        codes = [s.code for s in result.steps]
        self.assertEqual(codes, ["assess", "plan_free", "compose", "app_fill"])
        self.assertEqual(len(transport.chat_calls), 1, "only the words cost a model call")
        self.assertEqual(transport.chat_calls[0]["model"], "v/mid")
        self.assertIn("<form", result.app["html"])
        self.assertIn("Train tickets", result.output)

    def test_an_ordinary_question_is_not_an_app(self):
        transport = FakeTransport(decision_hook=jev_for(set(), wants_app=0.1),
                                  completions=['{"strategy":"s","answer_from":"generation",'
                                               '"generation":{"instruction":"answer"}}', "42"])
        result = executor_with(transport).run(Task(prompt="What is six times seven?"))
        self.assertIsNone(result.app)

    def test_never_does_not_even_ask(self):
        transport = FakeTransport(decision_hook=jev_for(set(), wants_app=0.99),
                                  completions=["plain text answer"])
        executor_with(transport, "never").run(Task(prompt="Build me a booking page"))
        self.assertNotIn("__wants_app__", transport.decision_calls[0]["questions"])

    def test_always_builds_one_whatever_jev_thinks(self):
        transport = FakeTransport(decision_hook=jev_for({"hero", "form", "table"}, wants_app=0.0),
                                  completions=[TRAIN])
        result = executor_with(transport, "always").run(Task(prompt="train times"))
        self.assertIsNotNone(result.app)

    def test_the_app_lands_in_the_workspace_as_a_file(self):
        transport = FakeTransport(decision_hook=jev_for({"hero", "table"}), completions=[TRAIN])
        result = executor_with(transport).run(Task(prompt="Build a train timetable app"))
        with tempfile.TemporaryDirectory() as folder:
            saved = Store(Path(folder)).save_turn(chat_id="c1", title="trains", prompt="p",
                                                   state="", result=result.to_dict())
            pages = [p for p in saved.output_paths if p.name == "app.html"]
            self.assertEqual(len(pages), 1)
            self.assertIn("<table>", pages[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
