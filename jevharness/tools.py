"""Tools the harness can run before it reasons.

Everything here is a plugin: a Tool declares what it does, how to pull its
arguments out of a task without spending a token, and how to run. The registry
is what a plan picks from, and what Jev is shown when it is asked to choose.

The division of labour is the same as everywhere else in this harness. Jev
decides *which* tool — a typed choice with a calibrated probability. Arguments
are extracted by ordinary code from the text the user already wrote: a URL, a
path, an arithmetic expression. Neither job needs an LLM.

Safety is not optional here, because tools touch the filesystem and the network:

* File tools are confined to one workspace directory, and the resolved path is
  checked to be inside it. Symlinks are resolved before the check.
* The fetch tool refuses anything but http/https, resolves the host first and
  refuses private, loopback and link-local addresses, and caps the body.
* The shell tool is off unless the operator sets JEVIA_ENABLE_SHELL=1. It is
  not a sandbox and it is not pretending to be one.
"""
from __future__ import annotations

import ast
import ipaddress
import operator
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import HarnessError, SchemaError

MAX_OUTPUT = 40_000          # characters handed back into the state
MAX_FETCH_BYTES = 2_000_000
FETCH_TIMEOUT = 20.0
SHELL_TIMEOUT = 20.0


class ToolError(HarnessError):
    status = 400
    code = "tool_error"


@dataclass(frozen=True)
class ToolContext:
    """What tools are allowed to touch."""

    workspace: Path
    allow_network: bool = True
    allow_shell: bool = False
    # The checklist the model keeps for this run. The context itself is frozen
    # — it is a permission object — so this list is updated in place.
    todo: List[dict] = field(default_factory=list)
    # How to put a question to the person running the task, when a deployment
    # wired one up. Without it, the ask tool says so instead of guessing.
    ask: Optional[Callable[..., Optional[str]]] = None
    # How to hand a piece of work to a model of its own. The run supplies it;
    # without it the delegate tool says so rather than pretending.
    delegate: Optional[Callable[..., Tuple[str, str]]] = None
    # Commands still running in the background, for the life of this run.
    jobs: "Jobs" = field(default_factory=lambda: Jobs())

    @classmethod
    def from_env(cls, workspace: Optional[Path] = None) -> "ToolContext":
        root = workspace or Path(
            os.environ.get("JEVIA_WORKSPACE") or Path.cwd() / "workspace"
        )
        return cls(
            workspace=root.expanduser().resolve(),
            allow_network=os.environ.get("JEVIA_ALLOW_NETWORK", "1") not in ("0", "false"),
            allow_shell=os.environ.get("JEVIA_ENABLE_SHELL", "") in ("1", "true", "yes"),
        )

    def resolve(self, raw: str) -> Path:
        """A path inside the workspace, or an error. No traversal, no symlink escape."""
        if not raw or not raw.strip():
            raise ToolError("a path is required")
        candidate = (self.workspace / raw.strip()).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ToolError(f"cannot resolve path: {exc}") from exc
        root = self.workspace.resolve()
        if resolved != root and root not in resolved.parents:
            raise ToolError("path is outside the workspace")
        return resolved


@dataclass(frozen=True)
class ToolResult:
    name: str
    ok: bool
    output: str
    detail: str = ""
    ms: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "chars": len(self.output),
            "ms": self.ms,
        }


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[{len(text) - limit} more characters]"


class Tool:
    """Base class. Subclasses declare a name, a summary and a run().

    A tool reaches a model two ways. ``summary`` is the criterion Jev is shown
    when it picks one tool for a step; ``parameters`` is the JSON Schema a
    model sees when it may call tools itself. A tool with no parameters
    schema is never offered to a model — it can still be chosen by Jev, whose
    arguments come from ``extract``.
    """

    name = ""
    summary = ""            # shown to Jev as the criterion for choosing it
    parameters: Optional[Dict[str, Any]] = None   # JSON Schema, for model-facing use
    needs_network = False
    needs_shell = False
    # Anything that changes the machine or reaches outside it goes past Jev
    # first. Arguments are read out of the material, and the material may be
    # something a stranger wrote.
    side_effects = False

    def extract(self, prompt: str, state: str) -> Optional[Dict[str, Any]]:
        """Deterministic argument extraction, or None when nothing fits."""
        return None

    def schema(self) -> Optional[dict]:
        """What a model is told about this tool, or None when it is not offered."""
        if not self.parameters:
            return None
        return {"name": self.name, "description": self.summary, "parameters": self.parameters}

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:  # pragma: no cover
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------ #

    def _ok(self, output: str, detail: str, started: float) -> ToolResult:
        return ToolResult(self.name, True, _clip(output), detail,
                          int((time.perf_counter() - started) * 1000))

    def _fail(self, detail: str, started: float) -> ToolResult:
        return ToolResult(self.name, False, "", detail,
                          int((time.perf_counter() - started) * 1000))


# --------------------------------------------------------------------------- #
# Deterministic, no side effects
# --------------------------------------------------------------------------- #

class Clock(Tool):
    name = "now"
    summary = "The current date and time. Choose this when the task depends on today's date."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def extract(self, prompt: str, state: str) -> Optional[Dict[str, Any]]:
        return {}

    def run(self, args, ctx):
        started = time.perf_counter()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
        return self._ok(stamp, stamp, started)


_MATH_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}
EXPRESSION = re.compile(r"[-+(]?\s*\d[\d\s.,]*(?:[-+*/^%()]\s*\d[\d\s.,]*)+")


class Calculator(Tool):
    """Arithmetic, evaluated over a parsed AST — never eval()."""

    name = "calculate"
    summary = (
        "Exact arithmetic on numbers written in the task. Choose this when the "
        "answer requires a sum, product, percentage or other calculation."
    )
    parameters = {"type": "object", "required": ["expression"], "additionalProperties": False,
                  "properties": {"expression": {"type": "string",
                                                "description": "An arithmetic expression, e.g. (1200 * 0.93) / 4"}}}

    def extract(self, prompt: str, state: str) -> Optional[Dict[str, Any]]:
        match = EXPRESSION.search(prompt)
        return {"expression": match.group(0).strip()} if match else None

    def run(self, args, ctx):
        started = time.perf_counter()
        raw = str(args.get("expression", "")).replace(",", "").replace("^", "**").strip()
        if not raw:
            return self._fail("no expression", started)
        try:
            tree = ast.parse(raw, mode="eval")
            value = self._eval(tree.body)
        except (SyntaxError, ValueError, TypeError, KeyError, ZeroDivisionError,
                OverflowError, RecursionError) as exc:
            return self._fail(f"cannot evaluate: {exc}", started)
        text = f"{raw} = {value}"
        return self._ok(text, text, started)

    def _eval(self, node: ast.AST) -> float:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return node.value
            raise ValueError("only numbers are allowed")
        if isinstance(node, ast.BinOp):
            op = _MATH_OPS.get(type(node.op))
            if op is None:
                raise ValueError("unsupported operator")
            left, right = self._eval(node.left), self._eval(node.right)
            if op is operator.pow and (abs(right) > 64 or abs(left) > 1e6):
                raise ValueError("exponent out of range")
            return op(left, right)
        if isinstance(node, ast.UnaryOp):
            op = _MATH_OPS.get(type(node.op))
            if op is None:
                raise ValueError("unsupported operator")
            return op(self._eval(node.operand))
        raise ValueError("unsupported expression")


# --------------------------------------------------------------------------- #
# Filesystem, confined to the workspace
# --------------------------------------------------------------------------- #

PATH_HINT = re.compile(r"(?:^|[\s\"'`(])([\w./\-]+\.[A-Za-z0-9]{1,8})(?=$|[\s\"'`),])")


class ReadFile(Tool):
    name = "read_file"
    summary = "Read a text file from the workspace. Choose this when the task names a file to look at."
    parameters = {"type": "object", "required": ["path"], "additionalProperties": False,
                  "properties": {"path": {"type": "string",
                                          "description": "Path relative to the workspace root."}}}

    def extract(self, prompt: str, state: str) -> Optional[Dict[str, Any]]:
        match = PATH_HINT.search(prompt)
        return {"path": match.group(1)} if match else None

    def run(self, args, ctx):
        started = time.perf_counter()
        try:
            path = ctx.resolve(str(args.get("path", "")))
            if not path.is_file():
                return self._fail(f"no such file: {args.get('path')}", started)
            text = path.read_text(encoding="utf-8", errors="replace")
        except (ToolError, OSError) as exc:
            return self._fail(str(exc), started)
        return self._ok(text, f"{path.name}, {len(text)} chars", started)


class ListDir(Tool):
    name = "list_dir"
    summary = "List the files in a workspace directory. Choose this when the task asks what is there."
    parameters = {"type": "object", "additionalProperties": False,
                  "properties": {"path": {"type": "string",
                                          "description": "Directory relative to the workspace root. Defaults to the root."}}}

    def extract(self, prompt: str, state: str) -> Optional[Dict[str, Any]]:
        return {"path": "."}

    def run(self, args, ctx):
        started = time.perf_counter()
        try:
            path = ctx.resolve(str(args.get("path", ".")) or ".")
            if not path.is_dir():
                return self._fail("not a directory", started)
            rows = []
            for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
                size = entry.stat().st_size if entry.is_file() else 0
                rows.append(f"{'d' if entry.is_dir() else '-'} {entry.name}  {size}")
        except (ToolError, OSError) as exc:
            return self._fail(str(exc), started)
        return self._ok("\n".join(rows) or "(empty)", f"{len(rows)} entries", started)


class WriteFile(Tool):
    side_effects = True
    name = "write_file"
    summary = "Write text to a file in the workspace. Choose this only when the task asks to save something."
    parameters = {"type": "object", "required": ["path", "content"], "additionalProperties": False,
                  "properties": {"path": {"type": "string", "description": "Path relative to the workspace root."},
                                 "content": {"type": "string", "description": "The whole new contents of the file."}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        try:
            path = ctx.resolve(str(args.get("path", "")))
            content = str(args.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except (ToolError, OSError) as exc:
            return self._fail(str(exc), started)
        return self._ok(f"wrote {path.name}", f"{path.name}, {len(content)} chars", started)


class SearchFiles(Tool):
    name = "search_files"
    summary = "Find which workspace files contain a phrase. Choose this when the task asks where something is."
    parameters = {"type": "object", "required": ["query"], "additionalProperties": False,
                  "properties": {"query": {"type": "string", "description": "The text to look for."},
                                 "glob": {"type": "string", "description": "Optional filename pattern, e.g. *.py"}}}

    def extract(self, prompt: str, state: str) -> Optional[Dict[str, Any]]:
        match = re.search(r"[\"'“](.{2,80}?)[\"'”]", prompt)
        return {"query": match.group(1)} if match else None

    def run(self, args, ctx):
        started = time.perf_counter()
        query = str(args.get("query", "")).strip()
        if not query:
            return self._fail("no query", started)
        hits: List[str] = []
        try:
            root = ctx.workspace
            if not root.is_dir():
                return self._fail("workspace does not exist", started)
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.stat().st_size > 2_000_000:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if query.lower() in line.lower():
                        hits.append(f"{path.relative_to(root)}:{number}: {line.strip()[:200]}")
                        if len(hits) >= 200:
                            break
                if len(hits) >= 200:
                    break
        except OSError as exc:
            return self._fail(str(exc), started)
        return self._ok("\n".join(hits) or "(no matches)", f"{len(hits)} matches", started)


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
ANY_TAG = re.compile(r"<[^>]+>")


# Names that must never be reachable, whatever DNS says. These checks do not
# depend on resolution, so they hold even where it cannot be trusted.
BLOCKED_NAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "metadata",
    "metadata.google.internal", "instance-data",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
BLOCKED_LITERALS = {"169.254.169.254", "100.100.100.200", "::1"}


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Check every hop before it is followed, not after it has been made.

    Validating only the final URL still lets the first redirect reach a
    private address — which is the whole trick behind server-side request
    forgery. This refuses the hop instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in ("http", "https") or not parsed.hostname \
                or not _is_public(parsed.hostname):
            raise urllib.error.HTTPError(newurl, code, "refusing a redirect to a private host",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_opener() -> urllib.request.OpenerDirector:
    """An opener that will not follow a redirect off the public internet."""
    return urllib.request.build_opener(_SafeRedirect)


def _literal_is_private(host: str) -> bool:
    """True when the URL spells out an address that must not be reached."""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return (address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified)


def _resolution_check_enabled() -> bool:
    """Whether to trust DNS for the private-address check.

    Behind a proxy — a corporate one, a container network, a sandbox — every
    name resolves to an internal address, so the check rejects the entire web
    and tells you nothing. In that case the name-based rules above still apply,
    and the resolution check is opt-out.
    """
    if os.environ.get("JEVIA_ALLOW_PRIVATE_HOSTS", "") in ("1", "true", "yes"):
        return False
    if any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")):
        return False
    return True


def _is_public(host: str) -> bool:
    """Refuse hosts that point into the machine or its private network."""
    if not host:
        return False
    name = host.strip("[]").lower().rstrip(".")
    if name in BLOCKED_NAMES or name in BLOCKED_LITERALS:
        return False
    if any(name.endswith(suffix) for suffix in BLOCKED_SUFFIXES):
        return False
    if _literal_is_private(name):
        return False
    if not _resolution_check_enabled():
        return True
    try:
        infos = socket.getaddrinfo(name, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified):
            return False
    return True


class FetchUrl(Tool):
    side_effects = True
    name = "fetch_url"
    summary = "Fetch a web page and read its text. Choose this when the task points at a link."
    parameters = {"type": "object", "required": ["url"], "additionalProperties": False,
                  "properties": {"url": {"type": "string", "description": "An http or https URL."}}}
    needs_network = True

    def extract(self, prompt: str, state: str) -> Optional[Dict[str, Any]]:
        match = URL_RE.search(prompt) or URL_RE.search(state or "")
        return {"url": match.group(0)} if match else None

    def run(self, args, ctx):
        started = time.perf_counter()
        if not ctx.allow_network:
            return self._fail("network tools are disabled", started)
        url = str(args.get("url", "")).strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return self._fail("only http and https are allowed", started)
        if not parsed.hostname or not _is_public(parsed.hostname):
            return self._fail("refusing a private or unresolvable host", started)
        request = urllib.request.Request(
            url, headers={"User-Agent": "JEVia/1.0", "Accept": "text/*, application/json"}
        )
        try:
            with safe_opener().open(request, timeout=FETCH_TIMEOUT) as response:
                if response.geturl() != url:
                    final = urllib.parse.urlparse(response.geturl())
                    if final.hostname and not _is_public(final.hostname):
                        return self._fail("redirected to a private host", started)
                raw = response.read(MAX_FETCH_BYTES)
                charset = response.headers.get_content_charset() or "utf-8"
        except Exception as exc:  # noqa: BLE001 - urllib raises many shapes
            return self._fail(f"{type(exc).__name__}: {exc}", started)
        body = raw.decode(charset, errors="replace")
        text = ANY_TAG.sub(" ", TAGS.sub(" ", body))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
        return self._ok(text, f"{parsed.netloc}, {len(text)} chars", started)


# --------------------------------------------------------------------------- #
# Shell — opt-in, and not a sandbox
# --------------------------------------------------------------------------- #

class EditFile(Tool):
    """Replace one exact passage in a file.

    Rewriting a whole file to change a line is how a model loses the rest of
    it. This asks for the old text and refuses unless it appears exactly once,
    so an edit either lands where it was meant to or does not happen.
    """

    side_effects = True
    name = "edit_file"
    summary = "Replace an exact passage in a workspace file. Choose this to change part of a file."
    parameters = {"type": "object", "required": ["path", "old_text", "new_text"],
                  "additionalProperties": False,
                  "properties": {
                      "path": {"type": "string", "description": "Path relative to the workspace root."},
                      "old_text": {"type": "string",
                                   "description": "The exact text to replace, unique in the file, with enough context to be unambiguous."},
                      "new_text": {"type": "string", "description": "What replaces it."}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        old_text, new_text = str(args.get("old_text", "")), str(args.get("new_text", ""))
        if not old_text:
            return self._fail("old_text is required; use write_file for a new file", started)
        try:
            path = ctx.resolve(str(args.get("path", "")))
            if not path.is_file():
                return self._fail(f"no such file: {args.get('path')}", started)
            text = path.read_text(encoding="utf-8")
        except (ToolError, OSError, UnicodeDecodeError) as exc:
            return self._fail(str(exc), started)
        found = text.count(old_text)
        if found == 0:
            return self._fail("old_text is not in the file; read it again and copy the passage exactly",
                              started)
        if found > 1:
            return self._fail(f"old_text appears {found} times; include more surrounding lines to "
                              "identify the one you mean", started)
        try:
            path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        except OSError as exc:
            return self._fail(str(exc), started)
        return self._ok(f"edited {path.name}", f"{path.name}, {len(old_text)} → {len(new_text)} chars",
                        started)


class RunCode(Tool):
    """Run a short Python program in the workspace, in a process of its own.

    Off by the same switch as the shell, because it is the same power: it can
    do anything the user can. What it buys is the step a writing model cannot
    take on its own — checking a number, parsing a file, proving the thing
    works before it is handed over.
    """

    side_effects = True
    name = "run_code"
    summary = "Run a short Python program in the workspace and read its output."
    needs_shell = True
    parameters = {"type": "object", "required": ["code"], "additionalProperties": False,
                  "properties": {"code": {"type": "string", "description": "Python 3 source. Print what you need to see."},
                                 "timeout": {"type": "number", "description": "Seconds to allow, up to 60."}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        if not ctx.allow_shell:
            return self._fail("running code is off; set JEVIA_ENABLE_SHELL=1 to enable it", started)
        code = str(args.get("code", "")).strip()
        if not code:
            return self._fail("no code", started)
        timeout = max(1.0, min(float(args.get("timeout") or SHELL_TIMEOUT), 60.0))
        try:
            done = subprocess.run(
                [sys.executable, "-I", "-c", code], cwd=str(ctx.workspace),
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return self._fail(f"timed out after {timeout:.0f}s", started)
        except OSError as exc:
            return self._fail(str(exc), started)
        output = (done.stdout or "") + (("\n" + done.stderr) if done.stderr else "")
        detail = f"exit {done.returncode}"
        return (self._ok(output.strip() or detail, detail, started) if done.returncode == 0
                else self._fail(f"{detail}\n{output.strip()[:2000]}", started))


class Todo(Tool):
    """The model's own checklist for a long task.

    It is not decoration: a list the model wrote and keeps updating is what
    stops a ten-step job from quietly becoming a six-step one. The list lives
    in the run, is shown to the reader, and costs nothing to keep.
    """

    name = "todo_write"
    summary = "Record or update the checklist for this task, so nothing is quietly dropped."
    parameters = {"type": "object", "required": ["items"], "additionalProperties": False,
                  "properties": {"items": {
                      "type": "array", "description": "The whole list, in order, every time.",
                      "items": {"type": "object", "required": ["title", "status"],
                                "additionalProperties": False,
                                "properties": {
                                    "title": {"type": "string"},
                                    "status": {"type": "string",
                                               "enum": ["pending", "running", "done", "dropped"]},
                                    "note": {"type": "string"}}}}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        items = [i for i in (args.get("items") or []) if isinstance(i, Mapping)][:40]
        if not items:
            return self._fail("items is required", started)
        rows = []
        for item in items:
            status = str(item.get("status") or "pending")
            mark = {"done": "x", "running": ">", "dropped": "-"}.get(status, " ")
            rows.append(f"[{mark}] {str(item.get('title') or '')[:200]}"
                        + (f" — {str(item.get('note'))[:120]}" if item.get("note") else ""))
        ctx.todo[:] = [{"title": str(i.get("title") or "")[:200],
                        "status": str(i.get("status") or "pending"),
                        "note": str(i.get("note") or "")[:200]} for i in items]
        done = sum(1 for i in items if i.get("status") == "done")
        return self._ok("\n".join(rows), f"{done}/{len(items)} done", started)


class Jobs:
    """Commands still running, kept for the life of one run.

    A test suite or a build takes minutes; waiting for it inside one tool call
    burns the step budget and blocks everything else. Started here, the model
    can go and do something else and come back for the output.
    """

    MAX = 8

    def __init__(self) -> None:
        self.jobs: Dict[str, dict] = {}
        self._next = 0

    def start(self, command: str, cwd: Path) -> dict:
        if sum(1 for j in self.jobs.values() if j["process"].poll() is None) >= self.MAX:
            raise ToolError(f"too many jobs are already running (max {self.MAX})")
        self._next += 1
        job_id = f"job{self._next}"
        process = subprocess.Popen(command, shell=True, cwd=str(cwd), text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        job = {"id": job_id, "command": command, "process": process, "output": [],
               "started": time.time()}
        self.jobs[job_id] = job

        def drain() -> None:
            for line in process.stdout:                       # ends when the process does
                job["output"].append(line)
                del job["output"][:-2000]                     # keep the tail, bounded

        threading.Thread(target=drain, daemon=True).start()
        return job

    def status(self, job: dict) -> str:
        code = job["process"].poll()
        return "running" if code is None else f"exited {code}"

    def stop_all(self) -> None:
        for job in self.jobs.values():
            if job["process"].poll() is None:
                job["process"].kill()


class StartJob(Tool):
    side_effects = True
    needs_shell = True
    name = "job_start"
    summary = "Start a long command in the background and keep working while it runs."
    parameters = {"type": "object", "required": ["command"], "additionalProperties": False,
                  "properties": {"command": {"type": "string", "description": "The command to run in the workspace."}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        if not ctx.allow_shell:
            return self._fail("background jobs need the shell; set JEVIA_ENABLE_SHELL=1", started)
        command = str(args.get("command", "")).strip()
        if not command:
            return self._fail("no command", started)
        try:
            job = ctx.jobs.start(command, ctx.workspace)
        except (ToolError, OSError) as exc:
            return self._fail(str(exc), started)
        return self._ok(f"{job['id']} started", job["id"], started)


class ListJobs(Tool):
    needs_shell = True
    name = "job_list"
    summary = "List the background jobs and whether they are still running."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def run(self, args, ctx):
        started = time.perf_counter()
        if not ctx.jobs.jobs:
            return self._ok("no jobs", "none", started)
        rows = [f"{j['id']}  {ctx.jobs.status(j)}  {j['command'][:80]}"
                for j in ctx.jobs.jobs.values()]
        return self._ok("\n".join(rows), f"{len(rows)} job(s)", started)


class JobOutput(Tool):
    needs_shell = True
    name = "job_output"
    summary = "Read what a background job has printed so far."
    parameters = {"type": "object", "required": ["id"], "additionalProperties": False,
                  "properties": {"id": {"type": "string", "description": "The job id, e.g. job1."},
                                 "tail": {"type": "integer", "description": "How many last lines, default 100."}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        job = ctx.jobs.jobs.get(str(args.get("id", "")))
        if job is None:
            return self._fail("no such job", started)
        tail = max(1, min(int(args.get("tail") or 100), 500))
        text = "".join(job["output"][-tail:]).strip()
        return self._ok(text or "(nothing yet)", ctx.jobs.status(job), started)


class KillJob(Tool):
    side_effects = True
    needs_shell = True
    name = "job_kill"
    summary = "Stop a background job."
    parameters = {"type": "object", "required": ["id"], "additionalProperties": False,
                  "properties": {"id": {"type": "string"}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        job = ctx.jobs.jobs.get(str(args.get("id", "")))
        if job is None:
            return self._fail("no such job", started)
        if job["process"].poll() is None:
            job["process"].kill()
        return self._ok(f"{job['id']} stopped", job["id"], started)


class WebSearch(Tool):
    """Search the web and get the results as text.

    The harness already searches on its own when a plan calls for research;
    this is for the model that has started work and finds it needs one more
    fact. Results are titles, links and snippets — the page itself is a
    separate decision, made with fetch_url.
    """

    name = "web_search"
    summary = "Search the web and read the results. Choose this when a fact is missing."
    needs_network = True
    parameters = {"type": "object", "required": ["query"], "additionalProperties": False,
                  "properties": {"query": {"type": "string", "description": "Search keywords, not a sentence."},
                                 "limit": {"type": "integer", "description": "How many results, up to 10."}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        if not ctx.allow_network:
            return self._fail("network tools are disabled", started)
        query = str(args.get("query", "")).strip()
        if not query:
            return self._fail("query is required", started)
        from .research import search

        limit = max(1, min(int(args.get("limit") or 6), 10))
        hits = search(query, limit=limit)
        if not hits:
            return self._fail(f"nothing came back for {query!r}", started)
        rows = [f"{i}. {h.title} — {h.url}\n   {h.snippet}" for i, h in enumerate(hits[:limit], 1)]
        return self._ok("\n".join(rows), f"{len(rows)} results for {query!r}", started)


class Delegate(Tool):
    """Hand one self-contained piece of work to a model of its own.

    The point is not parallelism, it is context: a long search through files,
    or a survey of something tangential, fills the caller's context with
    material it will never need again. Delegated, the work happens somewhere
    else and only its conclusion comes back.
    """

    name = "delegate"
    summary = "Hand a self-contained piece of work to another model and get back only its result."
    parameters = {"type": "object", "required": ["task"], "additionalProperties": False,
                  "properties": {
                      "task": {"type": "string",
                               "description": "The whole job, written so someone with no other context could do it."},
                      "context": {"type": "string", "description": "Anything they need that they cannot look up."},
                      "difficulty": {"type": "integer", "minimum": 1, "maximum": 4,
                                     "description": "1 mechanical, 2 routine, 3 hard, 4 frontier. The cheapest model that clears it is used."}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        if ctx.delegate is None:
            return self._fail("there is nobody to delegate to in this run; do it yourself", started)
        task = str(args.get("task", "")).strip()
        if not task:
            return self._fail("task is required", started)
        try:
            text, model = ctx.delegate(task[:4000], str(args.get("context") or "")[:8000],
                                       int(args.get("difficulty") or 2))
        except ToolError as exc:
            return self._fail(str(exc), started)
        if not text.strip():
            return self._fail("the delegate came back with nothing", started)
        return self._ok(text, f"done by {model}", started)


class AskUser(Tool):
    """Ask the person a question, and wait for the answer.

    The alternative is guessing, and a guess made early is a whole task spent
    on the wrong thing. Worth one question: which of two readings of the task
    is meant, which file, whether to go ahead with something expensive. Not
    worth one: anything the material already answers.
    """

    name = "ask_user_question"
    summary = "Ask the person one short question and wait, when the task is genuinely ambiguous."
    parameters = {"type": "object", "required": ["question"], "additionalProperties": False,
                  "properties": {
                      "question": {"type": "string",
                                   "description": "One specific question, in the language of the task."},
                      "options": {"type": "array", "maxItems": 5, "items": {"type": "string"},
                                  "description": "Two to five concrete choices, when the answer is a choice."}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        question = str(args.get("question", "")).strip()
        if not question:
            return self._fail("question is required", started)
        if ctx.ask is None:
            return self._fail("there is no one to ask in this run; decide and say what you assumed",
                              started)
        options = [str(o)[:80] for o in (args.get("options") or [])][:5]
        answer = ctx.ask(question[:400], options=options, kind="question")
        if answer is None:
            return self._fail("nobody answered; carry on and say what you assumed", started)
        return self._ok(answer, f"answered in {int(time.perf_counter() - started)}s", started)


class Shell(Tool):
    side_effects = True
    name = "shell"
    summary = "Run a shell command in the workspace. Only for tasks that explicitly ask to run something."
    needs_shell = True
    parameters = {"type": "object", "required": ["command"], "additionalProperties": False,
                  "properties": {"command": {"type": "string",
                                             "description": "The command, run with the workspace as its directory."}}}

    def run(self, args, ctx):
        started = time.perf_counter()
        if not ctx.allow_shell:
            return self._fail(
                "the shell tool is off; set JEVIA_ENABLE_SHELL=1 to enable it", started
            )
        command = str(args.get("command", "")).strip()
        if not command:
            return self._fail("no command", started)
        try:
            ctx.workspace.mkdir(parents=True, exist_ok=True)
            done = subprocess.run(
                command, shell=True, cwd=str(ctx.workspace), capture_output=True,
                text=True, timeout=SHELL_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return self._fail(f"timed out after {SHELL_TIMEOUT:.0f}s", started)
        except OSError as exc:
            return self._fail(str(exc), started)
        output = (done.stdout or "") + (("\n" + done.stderr) if done.stderr else "")
        return self._ok(output.strip(), f"exit {done.returncode}", started)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

BUILTIN: Tuple[Tool, ...] = (
    Clock(), Calculator(), ReadFile(), ListDir(), SearchFiles(), WriteFile(),
    FetchUrl(), Shell(), EditFile(), RunCode(), Todo(), AskUser(), WebSearch(), Delegate(),
    StartJob(), ListJobs(), JobOutput(), KillJob(),
)
# Presets, because "which tools" is a permission decision and should be one
# choice a person can hold in their head — not a list of checkboxes.
#
#   read   looks at things, changes nothing
#   write  also writes and edits inside the workspace, each change judged first
#   full   also runs commands and code, which is off unless the operator says so
PRESETS: Dict[str, Tuple[str, ...]] = {
    "read": ("ask_user_question", "delegate", "web_search", "now", "calculate", "read_file",
             "list_dir", "search_files", "fetch_url",
             "todo_write"),
    "write": ("ask_user_question", "delegate", "web_search", "now", "calculate", "read_file",
             "list_dir", "search_files", "fetch_url",
              "todo_write", "write_file", "edit_file"),
    "full": ("ask_user_question", "delegate", "web_search", "now", "calculate", "read_file",
             "list_dir", "search_files", "fetch_url",
             "todo_write", "write_file", "edit_file", "shell", "run_code",
             "job_start", "job_list", "job_output", "job_kill"),
}
DEFAULT_PRESET = "write"
DEFAULT_ENABLED = PRESETS[DEFAULT_PRESET]


def preset(name: str) -> List[str]:
    """The tools a named preset switches on; unknown names fall back to write."""
    return list(PRESETS.get(str(name or "").strip().lower(), PRESETS[DEFAULT_PRESET]))

NO_TOOL = "none"
# Tools from an MCP server carry their server's name; see mcp.py.
SEPARATOR = "__"


@dataclass
class Registry:
    """The tools a run may use, and the choice Jev is offered."""

    tools: Dict[str, Tool] = field(default_factory=dict)
    enabled: List[str] = field(default_factory=lambda: list(DEFAULT_ENABLED))
    context: ToolContext = field(default_factory=ToolContext.from_env)

    @classmethod
    def builtin(cls, enabled: Optional[Sequence[str]] = None,
                context: Optional[ToolContext] = None,
                extra: Sequence[Tool] = ()) -> "Registry":
        """The built-in tools, plus any brought in from elsewhere (MCP).

        Tools from outside are enabled by being present: a user who configured
        a server wants its tools, and each one still goes past the gate.
        """
        tools = {tool.name: tool for tool in BUILTIN}
        tools.update({tool.name: tool for tool in extra})
        registry = cls(
            tools=tools,
            enabled=list(enabled if enabled is not None else DEFAULT_ENABLED)
            + [tool.name for tool in extra],
            context=context or ToolContext.from_env(),
        )
        registry.validate()
        return registry

    def validate(self) -> None:
        unknown = [name for name in self.enabled if name not in self.tools]
        if unknown:
            raise SchemaError(f"unknown tools: {sorted(set(unknown))}")

    def available(self) -> List[Tool]:
        out = []
        for name in self.enabled:
            tool = self.tools.get(name)
            if tool is None:
                continue
            if tool.needs_shell and not self.context.allow_shell:
                continue
            if tool.needs_network and not self.context.allow_network:
                continue
            out.append(tool)
        return out

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    def options(self) -> Dict[str, str]:
        """The criteria map for a Jev choice question."""
        options = {NO_TOOL: "No tool is needed; the material already contains everything."}
        for tool in self.available():
            options[tool.name] = tool.summary
        return options

    def schemas(self) -> List[dict]:
        """Every available tool a model may call, as JSON Schema."""
        return [schema for schema in (t.schema() for t in self.available()) if schema]

    def catalogue(self) -> str:
        """The list handed to the planner model."""
        return "\n".join(f"  {t.name}: {t.summary}" for t in self.available())

    def run(self, name: str, args: Mapping[str, Any]) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool {name!r}")
        if tool not in self.available():
            raise ToolError(f"tool {name!r} is not enabled")
        return tool.run(args, self.context)

    def to_public_dict(self) -> dict:
        return {
            "workspace": str(self.context.workspace),
            "allow_network": self.context.allow_network,
            "allow_shell": self.context.allow_shell,
            "external": [t.name for t in self.available() if SEPARATOR in t.name],
            "tools": [
                {
                    "name": tool.name,
                    "summary": tool.summary,
                    "enabled": tool.name in self.enabled,
                    "needs_network": tool.needs_network,
                    "needs_shell": tool.needs_shell,
                    "blocked": (tool.needs_shell and not self.context.allow_shell)
                    or (tool.needs_network and not self.context.allow_network),
                }
                for tool in self.tools.values()
            ],
        }
