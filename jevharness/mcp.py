"""Tools from somewhere else: an MCP client, in the standard library.

Everything in ``tools.py`` was written here. This module is how a run uses
tools that were not: an MCP server is a program that speaks JSON-RPC over its
own stdin and stdout, and answers ``tools/list`` with the tools it offers and
``tools/call`` when one is run. Hundreds already exist, and none of them has
to know anything about this harness.

What the harness insists on, because an MCP server is a program running as the
user:

* A server is configured in a file in the workspace, never by a model and
  never by a page. Whatever the model says, it cannot conjure a new command.
* Its tools pass through the same gate as the built-in ones. A server's own
  claim that a tool is read-only is a hint that can lower the question, never
  a permission that skips it.
* A server that is slow, broken or noisy is dropped rather than allowed to
  hang the run: every call has a timeout and every failure is a tool result
  the model can read.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .errors import SchemaError
from .tools import MAX_OUTPUT, Tool, ToolContext, ToolError, ToolResult, _clip

CONFIG = Path(".jevia") / "mcp.json"
PROTOCOL = "2025-06-18"
START_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0
MAX_TOOLS = 60
# A server's name prefixes its tools, so two servers may both offer "search".
SEPARATOR = "__"


@dataclass
class ServerSpec:
    """One MCP server, as the workspace file describes it."""

    name: str
    command: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    enabled: bool = True

    def validate(self) -> None:
        if not self.name or not self.name.replace("-", "").replace("_", "").isalnum():
            raise SchemaError("an MCP server needs a plain name (letters, digits, - and _)")
        if not self.command or not all(isinstance(part, str) for part in self.command):
            raise SchemaError(f"MCP server {self.name!r} needs a command, as a list of strings")

    def to_dict(self) -> dict:
        return {"name": self.name, "command": list(self.command), "env": dict(self.env),
                "cwd": self.cwd, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ServerSpec":
        command = raw.get("command")
        if isinstance(command, str):
            command = command.split()
        spec = cls(
            name=str(raw.get("name") or "").strip(),
            command=[str(part) for part in (command or [])],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            cwd=str(raw.get("cwd") or ""),
            enabled=bool(raw.get("enabled", True)),
        )
        spec.validate()
        return spec

    def to_public_dict(self, status: str = "", tools: Sequence[str] = (), error: str = "") -> dict:
        return {**self.to_dict(), "status": status, "tools": list(tools), "error": error}


def read_config(workspace: Optional[Path]) -> List[ServerSpec]:
    """The servers configured for this workspace. A bad entry is skipped, not fatal."""
    if not workspace:
        return []
    path = Path(workspace) / CONFIG
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("servers", raw) if isinstance(raw, Mapping) else raw
    if isinstance(entries, Mapping):      # {"name": {...}} is the common shorthand
        entries = [{**value, "name": key} for key, value in entries.items()]
    out: List[ServerSpec] = []
    for entry in entries or []:
        if not isinstance(entry, Mapping):
            continue
        try:
            out.append(ServerSpec.from_dict(entry))
        except SchemaError:
            continue
    return out


def write_config(workspace: Path, servers: Sequence[ServerSpec]) -> Path:
    """Save the server list, owner-readable: it can hold tokens for other services."""
    path = Path(workspace) / CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": [s.to_dict() for s in servers]},
                               ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


class Client:
    """One server process, and the JSON-RPC conversation with it."""

    def __init__(self, spec: ServerSpec, workspace: Optional[Path] = None) -> None:
        self.spec = spec
        self.workspace = Path(workspace) if workspace else None
        self.process: Optional[subprocess.Popen] = None
        self.tools: List[dict] = []
        self.error = ""
        self._id = 0
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------- #

    def start(self, timeout: float = START_TIMEOUT) -> bool:
        """Start the process and ask what it offers. False when it will not run."""
        if self.process is not None:
            return not self.error
        program = shutil.which(self.spec.command[0]) if self.spec.command else None
        if program is None:
            self.error = f"{self.spec.command[0] if self.spec.command else 'command'} is not installed"
            return False
        cwd = self.spec.cwd or (str(self.workspace) if self.workspace else None)
        try:
            self.process = subprocess.Popen(
                [program, *self.spec.command[1:]], cwd=cwd,
                env={**os.environ, **self.spec.env},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
        except OSError as exc:
            self.error = str(exc)
            return False
        try:
            self._request("initialize", {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "JEVia", "version": "1.0"},
            }, timeout=timeout)
            self._notify("notifications/initialized")
            listed = self._request("tools/list", {}, timeout=timeout)
            self.tools = [t for t in (listed.get("tools") or []) if isinstance(t, Mapping)][:MAX_TOOLS]
        except (ToolError, OSError) as exc:
            self.error = str(exc)
            self.stop()
            return False
        return True

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    # -- the wire ------------------------------------------------------------ #

    def _send(self, message: dict) -> None:
        if self.process is None or self.process.stdin is None:
            raise ToolError("the MCP server is not running")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _request(self, method: str, params: dict, timeout: float = CALL_TIMEOUT) -> dict:
        """One call. Replies that are not ours are skipped, not queued for ever."""
        with self._lock:
            self._id += 1
            request_id = self._id
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                line = self._readline(deadline)
                if line is None:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") != request_id:
                    continue                      # a notification, or another call's reply
                if message.get("error"):
                    raise ToolError(str(message["error"].get("message") or "the server refused"))
                result = message.get("result")
                return result if isinstance(result, Mapping) else {}
        raise ToolError(f"{self.spec.name} did not answer in {timeout:.0f}s")

    def _readline(self, deadline: float) -> Optional[str]:
        if self.process is None or self.process.stdout is None:
            return None
        # readline blocks; the process dying closes the pipe and returns "".
        line = self.process.stdout.readline()
        if line == "":
            self.error = self.error or f"{self.spec.name} stopped"
            return None
        return line.strip() or self._readline(deadline)

    def call(self, name: str, arguments: Mapping[str, Any], timeout: float = CALL_TIMEOUT) -> dict:
        return self._request("tools/call", {"name": name, "arguments": dict(arguments)},
                             timeout=timeout)


class RemoteTool(Tool):
    """One tool from an MCP server, in this harness's own shape."""

    def __init__(self, client: Client, declared: Mapping[str, Any]) -> None:
        self.client = client
        self.remote_name = str(declared.get("name") or "")
        self.name = f"{client.spec.name}{SEPARATOR}{self.remote_name}"
        summary = str(declared.get("description") or declared.get("title") or self.remote_name)
        self.summary = f"{summary.strip()[:300]} (from {client.spec.name})"
        schema = declared.get("inputSchema") or declared.get("input_schema")
        self.parameters = dict(schema) if isinstance(schema, Mapping) else {
            "type": "object", "properties": {}}
        hints = declared.get("annotations") or {}
        # A server may say a tool only reads. That lowers what is asked of the
        # user; it never skips the harness's own gate for the rest.
        self.side_effects = not bool(hints.get("readOnlyHint"))

    def run(self, args, ctx: ToolContext) -> ToolResult:
        started = time.perf_counter()
        if self.client.process is None and not self.client.start():
            return self._fail(self.client.error or "the server is not running", started)
        try:
            result = self.client.call(self.remote_name, args)
        except ToolError as exc:
            return self._fail(str(exc), started)
        text = _text_of(result)
        if result.get("isError"):
            return self._fail(text or "the tool reported an error", started)
        return self._ok(text, f"{self.client.spec.name}: {self.remote_name}", started)


def _text_of(result: Mapping[str, Any]) -> str:
    """The readable part of an MCP result: text blocks, then anything structured."""
    parts: List[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") in ("image", "audio"):
            parts.append(f"[{block['type']} returned, {len(str(block.get('data') or ''))} bytes]")
        elif block.get("type") == "resource":
            resource = block.get("resource") or {}
            parts.append(str(resource.get("text") or resource.get("uri") or ""))
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False)[:MAX_OUTPUT])
    return _clip("\n".join(p for p in parts if p).strip())


# Servers are processes, and starting one costs seconds. They are kept per
# workspace for the life of the harness process and replaced when the
# configuration changes, so a run never pays the start-up cost twice and a
# run that ends does not leave a process behind.
_SHARED: Dict[str, "Fleet"] = {}
_SHARED_LOCK = threading.Lock()


@dataclass
class Fleet:
    """Every configured server, and the tools they are offering right now."""

    clients: List[Client] = field(default_factory=list)
    # What the configuration looked like when these were started.
    fingerprint: str = ""

    @classmethod
    def start(cls, workspace: Optional[Path], specs: Optional[Sequence[ServerSpec]] = None) -> "Fleet":
        specs = list(specs if specs is not None else read_config(workspace))
        clients = []
        for spec in specs:
            if not spec.enabled:
                continue
            client = Client(spec, workspace)
            client.start()
            clients.append(client)
        return cls(clients=clients)

    def tools(self) -> List[RemoteTool]:
        out: List[RemoteTool] = []
        for client in self.clients:
            for declared in client.tools:
                if declared.get("name"):
                    out.append(RemoteTool(client, declared))
        return out

    def status(self) -> List[dict]:
        return [c.spec.to_public_dict(
            status="ready" if c.tools else "failed" if c.error else "no tools",
            tools=[t.get("name") for t in c.tools], error=c.error) for c in self.clients]

    def stop(self) -> None:
        for client in self.clients:
            client.stop()

    @classmethod
    def shared(cls, workspace: Optional[Path]) -> "Fleet":
        """The running fleet for this workspace, started once and reused."""
        specs = read_config(workspace)
        key = str(Path(workspace).expanduser().resolve()) if workspace else "-"
        fingerprint = json.dumps([s.to_dict() for s in specs], sort_keys=True)
        with _SHARED_LOCK:
            existing = _SHARED.get(key)
            if existing is not None and existing.fingerprint == fingerprint:
                return existing
            if existing is not None:
                existing.stop()
            fleet = cls.start(workspace, specs)
            fleet.fingerprint = fingerprint
            _SHARED[key] = fleet
            return fleet

    @classmethod
    def stop_shared(cls) -> None:
        with _SHARED_LOCK:
            for fleet in _SHARED.values():
                fleet.stop()
            _SHARED.clear()
