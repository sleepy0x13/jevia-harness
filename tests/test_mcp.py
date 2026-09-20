"""Tools from somewhere else, over the standard MCP conversation.

These run a real server process — tests/fixtures/mcp_echo_server.py — so the
client is checked against initialize / tools/list / tools/call rather than a
mock of what those might look like.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from jevharness import mcp
from jevharness.loop import Gate
from jevharness.providers import ToolCall
from jevharness.tools import Registry, ToolContext

SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_echo_server.py"


def spec(name="echo", enabled=True):
    return mcp.ServerSpec(name=name, command=[sys.executable, str(SERVER)], enabled=enabled)


class TestTalkingToAServer(unittest.TestCase):
    def client(self):
        client = mcp.Client(spec())
        self.addCleanup(client.stop)
        self.assertTrue(client.start(), client.error)
        return client

    def test_a_server_is_started_and_says_what_it_offers(self):
        client = self.client()
        self.assertEqual({t["name"] for t in client.tools}, {"echo", "shout"})

    def test_its_tools_arrive_named_after_the_server(self):
        fleet = mcp.Fleet(clients=[self.client()])
        names = {t.name for t in fleet.tools()}
        self.assertEqual(names, {"echo__echo", "echo__shout"})

    def test_a_read_only_hint_lowers_the_question_but_a_write_still_asks(self):
        fleet = mcp.Fleet(clients=[self.client()])
        tools = {t.name: t for t in fleet.tools()}
        self.assertFalse(tools["echo__echo"].side_effects, "the server said it only reads")
        self.assertTrue(tools["echo__shout"].side_effects, "anything else is judged first")

    def test_calling_one_returns_its_text(self):
        fleet = mcp.Fleet(clients=[self.client()])
        tools = {t.name: t for t in fleet.tools()}
        context = ToolContext(workspace=Path(tempfile.mkdtemp()))
        self.assertEqual(tools["echo__echo"].run({"text": "hello"}, context).output, "hello")
        self.assertEqual(tools["echo__shout"].run({"text": "hello"}, context).output, "HELLO")

    def test_an_error_from_the_server_is_a_result_not_a_crash(self):
        client = self.client()
        tool = mcp.RemoteTool(client, {"name": "missing", "description": "x"})
        result = tool.run({}, ToolContext(workspace=Path(tempfile.mkdtemp())))
        self.assertFalse(result.ok)

    def test_a_server_that_will_not_start_is_reported_and_skipped(self):
        broken = mcp.ServerSpec(name="ghost", command=["definitely-not-a-program-xyz"])
        fleet = mcp.Fleet.start(None, [broken])
        self.assertEqual(fleet.tools(), [])
        self.assertIn("not installed", fleet.status()[0]["error"])


class TestConfiguration(unittest.TestCase):
    """A server is configured in the workspace, never by a model or a page."""

    def workspace(self, config=None):
        root = Path(tempfile.mkdtemp())
        if config is not None:
            (root / ".jevia").mkdir(parents=True, exist_ok=True)
            (root / ".jevia" / "mcp.json").write_text(json.dumps(config), encoding="utf-8")
        return root

    def test_the_list_is_read_from_the_workspace(self):
        root = self.workspace({"servers": [spec().to_dict()]})
        self.assertEqual([s.name for s in mcp.read_config(root)], ["echo"])

    def test_the_shorthand_object_form_is_read_too(self):
        root = self.workspace({"echo": {"command": ["node", "server.js"]}})
        servers = mcp.read_config(root)
        self.assertEqual(servers[0].name, "echo")
        self.assertEqual(servers[0].command, ["node", "server.js"])

    def test_a_disabled_server_is_not_started(self):
        fleet = mcp.Fleet.start(None, [spec(enabled=False)])
        self.assertEqual(fleet.clients, [])

    def test_a_broken_entry_is_skipped_not_fatal(self):
        root = self.workspace({"servers": [{"name": "", "command": []}, spec().to_dict()]})
        self.assertEqual([s.name for s in mcp.read_config(root)], ["echo"])

    def test_no_config_means_no_servers(self):
        self.assertEqual(mcp.read_config(self.workspace()), [])

    def test_saving_keeps_the_file_to_its_owner(self):
        root = Path(tempfile.mkdtemp())
        path = mcp.write_config(root, [spec()])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([s.name for s in mcp.read_config(root)], ["echo"])


class TestInTheHarness(unittest.TestCase):
    def test_external_tools_reach_the_registry_and_the_gate(self):
        fleet = mcp.Fleet.start(None, [spec()])
        self.addCleanup(fleet.stop)
        registry = Registry.builtin(enabled=["read_file"],
                                    context=ToolContext(workspace=Path(tempfile.mkdtemp())),
                                    extra=fleet.tools())
        names = [s["name"] for s in registry.schemas()]
        self.assertIn("echo__echo", names)
        self.assertEqual(registry.run("echo__echo", {"text": "hi"}).output, "hi")
        # And the one that changes things is judged like any other.
        asked = []
        gate = Gate(registry, judge=lambda actions: (asked.extend(actions) or
                                                     [(True, 0.9) for _ in actions], None))
        gate.screen([ToolCall(id="c1", name="echo__shout", arguments={"text": "hi"})])
        self.assertEqual([name for name, _ in asked], ["echo__shout"])


if __name__ == "__main__":
    unittest.main()


class TestServerLifetime(unittest.TestCase):
    """Servers are processes: started once per workspace, and not left behind."""

    def test_the_same_workspace_reuses_the_running_fleet(self):
        root = Path(tempfile.mkdtemp())
        mcp.write_config(root, [spec()])
        first = mcp.Fleet.shared(root)
        self.addCleanup(mcp.Fleet.stop_shared)
        second = mcp.Fleet.shared(root)
        self.assertIs(first, second, "a second run does not start the server again")
        self.assertTrue(first.tools())

    def test_changing_the_configuration_replaces_it(self):
        root = Path(tempfile.mkdtemp())
        mcp.write_config(root, [spec()])
        first = mcp.Fleet.shared(root)
        self.addCleanup(mcp.Fleet.stop_shared)
        mcp.write_config(root, [spec(name="echo2")])
        second = mcp.Fleet.shared(root)
        self.assertIsNot(first, second)
        self.assertIsNone(first.clients[0].process, "the old one was stopped")
