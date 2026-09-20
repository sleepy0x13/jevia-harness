"""A local HTTP server for the harness. stdlib only, binds to localhost.

Design notes that matter for safety:

* The default bind address is 127.0.0.1. Nothing is exposed to the network
  unless the operator overrides HOST deliberately.
* API keys arrive in the request body from the caller's own browser, are used
  for that request, and are never logged or written to disk. The access log
  prints method, path and status — never a body.
* One Harness is kept per distinct settings fingerprint so that compiled plans
  are reused across requests. The fingerprint is a hash; keys are not stored in
  it in recoverable form.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import os

from . import events as run_events
from . import roles, vendors
from .config import Config, load_config
from .errors import ConfigError, HarnessError, SchemaError
from .harness import RUNS_DIR, Harness, Settings, parse_task
from .redact import redact, scrub
from .planner import PlanCache
from .providers import Transport
from .roster import CAPABILITY_LEVELS
from .scheduler import Job, Scheduler
from .store import Store
from .skills import Library, SkillError

UI_DIR = Path(__file__).resolve().parent / "ui"
MAX_BODY = 4 * 1024 * 1024


class HarnessPool:
    """Keeps a Harness — and therefore a plan cache — per settings fingerprint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool: Dict[str, Harness] = {}
        self._caches: Dict[str, PlanCache] = {}

    @staticmethod
    def fingerprint(settings: Settings) -> str:
        parts = [settings.jev_model]
        parts.append(settings.workspace or "-")
        for spec in sorted(settings.roster.available(), key=lambda m: m.id):
            parts.append(f"{spec.id}:{spec.model}:{spec.capability}")
        for cred in sorted(settings.roster.credentials.values(), key=lambda c: c.ref):
            # Hash the key so two different keys get separate caches without the
            # fingerprint ever holding the key itself.
            digest = hashlib.sha256(cred.api_key.encode("utf-8")).hexdigest()[:16]
            parts.append(f"{cred.ref}:{digest}")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]

    def get(self, settings: Settings) -> Harness:
        key = self.fingerprint(settings)
        with self._lock:
            if key not in self._caches:
                self._caches[key] = PlanCache(
                    workspace=Path(settings.workspace).expanduser()
                    if settings.workspace else None
                )
            cache = self._caches[key]
            # Settings may carry new thresholds each call, so rebuild the
            # Harness but keep the cache that belongs to this fingerprint.
            harness = Harness(settings, cache=cache)
            self._pool[key] = harness
            return harness

    def stats(self) -> dict:
        with self._lock:
            return {
                "fingerprints": len(self._caches),
                "caches": {k: c.stats() for k, c in self._caches.items()},
            }


class Schedules:
    """One scheduler per workspace, started the first time it is asked for."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self._lock = threading.Lock()
        self._by_workspace: Dict[str, Scheduler] = {}
        self.config = config

    def _run_job(self, job: Job) -> dict:
        # The same fallback a live request gets: a key in .env counts at 3am too.
        settings = Settings.from_request(job.settings, fallback=self.config)
        harness = Harness(settings)
        state = job.state or ""
        if job.is_loop and job.last_output:
            # Each round builds on the last; otherwise a loop just redoes the
            # same work and hopes for a different answer.
            state = (state + "\n\n" if state else "") + \
                f"PREVIOUS ROUND (#{job.runs - 1}) — improve on this:\n{job.last_output}"
        task = parse_task({"prompt": job.prompt, "state": state})
        result = harness.run(task)
        saved = None
        if settings.workspace:
            saved = Store(Path(settings.workspace)).save_turn(
                chat_id=f"loop-{job.id}", title=job.name, prompt=job.prompt,
                state=state, result=result.to_dict(),
            )
        checked = harness.check_goal(job.until, result.output) if job.is_loop else None
        return {
            "output": result.output,
            "goal_met": (checked["score"] if checked else None),
            "goal_all_met": (checked["met"] if checked else None),
            "conditions": (checked["conditions"] if checked else None),
            "saved": saved.to_dict(Path(settings.workspace))["chat"] if saved else "",
        }

    def for_workspace(self, workspace: Optional[str]) -> Optional[Scheduler]:
        if not workspace:
            return None
        key = str(Path(workspace).expanduser().resolve())
        with self._lock:
            scheduler = self._by_workspace.get(key)
            if scheduler is None:
                scheduler = Scheduler(self._run_job, Path(key))
                scheduler.start()
                self._by_workspace[key] = scheduler
            return scheduler


class Handler(BaseHTTPRequestHandler):
    server_version = "JevHarness/1.0"
    protocol_version = "HTTP/1.1"

    config: Config
    pool: HarnessPool
    schedules: Schedules

    # -- plumbing ----------------------------------------------------------- #

    def log_message(self, fmt: str, *args: Any) -> None:
        # Method, path and status only. Never bodies, never headers.
        print(f"  {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}")

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, exc: BaseException) -> None:
        # Never an upstream body, never a key: the message only, scrubbed.
        if isinstance(exc, HarnessError):
            self._json(exc.status, exc.public_dict())
            return
        traceback.print_exc()
        self._json(
            500,
            {"error": {"code": "internal_error", "message": redact(f"{type(exc).__name__}: {exc}")}},
        )

    def _same_origin(self) -> None:
        """Refuse a cross-site POST: another page open in the browser must not
        be able to start, stop, read or export this user's runs.

        A request without an Origin (curl, a test) is local by construction:
        the server binds to localhost.
        """
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            raise PermissionDenied("cross-site requests are refused")
        if origin:
            host = self.headers.get("Host") or ""
            allowed = {f"http://{host}", f"https://{host}"}
            if origin not in allowed:
                raise PermissionDenied("requests from another origin are refused")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise SchemaError("request body is too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaError(f"body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SchemaError("body must be a JSON object")
        return payload

    def _settings(self, payload: dict) -> Settings:
        return Settings.from_request(payload.get("settings"), fallback=self.config)

    # -- routes ------------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path == "/" or path == "/index.html":
                self._serve_file(UI_DIR / "index.html")
            elif path == "/api/health":
                self._json(
                    200,
                    {
                        "ok": True,
                        "env_key_present": self.config.has_key,
                        "jev_model": self.config.jev_model,
                        "capability_levels": list(CAPABILITY_LEVELS),
                        "pool": self.pool.stats(),
                    },
                )
            elif path == "/api/catalogue":
                language = "zh" if "lang=zh" in self.path else "en"
                self._json(200, {
                    "vendors": [v.to_public() for v in vendors.VENDORS],
                    "skills": Library().catalogue(),
                    "roles": roles.roster(language),
                    "capability_levels": list(CAPABILITY_LEVELS),
                })
            elif path.startswith("/ui/"):
                self._serve_file(UI_DIR / path[len("/ui/") :])
            else:
                self._json(404, {"error": {"code": "not_found", "message": path}})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            self._same_origin()
            payload = self._read_json()
            if path == "/api/run":
                self._run_stream(payload)
            elif path == "/api/runs":
                self._runs(payload)
            elif path == "/api/subagent":
                self._subagent(payload)
            elif path == "/api/sync":
                self._sync(payload)
            elif path == "/api/plan":
                self._plan(payload)
            elif path == "/api/decide":
                self._decide(payload)
            elif path == "/api/describe":
                self._json(200, self.pool.get(self._settings(payload)).describe())
            elif path in ("/api/models", "/api/prices"):
                self._models(payload)
            elif path == "/api/workspace":
                self._workspace(payload)
            elif path == "/api/memory":
                self._memory(payload)
            elif path == "/api/mcp":
                self._mcp(payload)
            elif path == "/api/skills":
                self._skills(payload)
            elif path == "/api/schedule":
                self._schedule(payload)
            else:
                self._json(404, {"error": {"code": "not_found", "message": path}})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    # -- handlers ----------------------------------------------------------- #

    def _serve_file(self, path: Path) -> None:
        path = path.resolve()
        root = UI_DIR.resolve()
        # A string prefix would also match a sibling folder whose name starts
        # with "ui"; this asks the path itself whether it is inside.
        if (path != root and root not in path.parents) or not path.is_file():
            self._json(404, {"error": {"code": "not_found", "message": "no such file"}})
            return
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        extra = None
        if path.suffix == ".ttf":
            kind = "font/ttf"
            # App previews run in a sandboxed frame with no origin of its own,
            # and fonts are only handed across origins when the server says so.
            extra = {"Access-Control-Allow-Origin": "*"}
        elif kind.startswith("text/") or kind in ("application/javascript", "application/json"):
            # Without this the UI's typographic characters arrive as mojibake.
            kind += "; charset=utf-8"
        self._send(200, path.read_bytes(), kind, extra)

    def _plan(self, payload: dict) -> None:
        harness = self.pool.get(self._settings(payload))
        task = parse_task(payload.get("task"))
        plan = harness.plan_only(task)
        self._json(200, {"plan": plan.to_dict(), "shape_key": task.shape_key()})

    def _decide(self, payload: dict) -> None:
        """Typed decisions only: one Jev call, guaranteed no LLM."""
        harness = self.pool.get(self._settings(payload))
        task = parse_task(payload.get("task"))
        if not task.questions:
            raise SchemaError("/api/decide requires task.questions")
        result = harness.run(task)
        self._json(200, result.to_dict())

    def _models(self, payload: dict) -> None:
        """What one key can run, priced where OpenRouter's public list knows it.

        Asked per credential, because every vendor answers differently: the
        vendor's own list when it gives one, the catalogue's ids when it will
        not, and a flag saying which of the two this is.
        """
        settings = self._settings(payload)
        ref = str(payload.get("ref") or "")
        credential = settings.roster.credentials.get(ref)
        if credential is None:
            credential = next(
                (c for c in settings.roster.credentials.values() if vendors.can_chat(c)),
                None,
            )
        if credential is None:
            raise ConfigError("no key that can chat has been added")
        vendor = vendors.get(credential.vendor)
        models, source = vendors.list_models(vendor, credential.base_url, credential.api_key)
        self._json(200, {"ref": credential.ref, "vendor": vendor.id,
                         "source": source, "models": models})

    def _workspace(self, payload: dict) -> None:
        """Inspect a folder so the user can pick one, and say what is in it.

        Only directory names are returned, never file contents: this endpoint
        exists to choose a workspace, not to read the disk through the browser.
        """
        raw = str(payload.get("path") or "").strip()
        home = Path.home()
        if not raw:
            current = Path(os.environ.get("JEVIA_WORKSPACE") or (Path.cwd() / "workspace"))
        else:
            current = Path(raw).expanduser()
        try:
            resolved = current.expanduser().resolve()
        except OSError as exc:
            raise SchemaError(f"cannot read that path: {exc}") from exc

        if payload.get("create") and not resolved.exists():
            try:
                resolved.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SchemaError(f"cannot create that folder: {exc}") from exc

        folders: list = []
        if resolved.is_dir():
            try:
                for entry in sorted(resolved.iterdir(), key=lambda e: e.name.lower()):
                    if entry.is_dir() and not entry.name.startswith("."):
                        folders.append(entry.name)
                    if len(folders) >= 200:
                        break
            except OSError:
                folders = []
        self._json(200, {
            "path": str(resolved),
            "exists": resolved.exists(),
            "is_dir": resolved.is_dir(),
            "writable": os.access(str(resolved), os.W_OK) if resolved.is_dir() else False,
            "parent": str(resolved.parent) if resolved.parent != resolved else None,
            "folders": folders,
            "suggestions": [
                str(p) for p in (home, home / "Desktop", home / "Documents", Path.cwd())
                if p.is_dir()
            ],
        })

    def _skills(self, payload: dict) -> None:
        """List, read, save, install or delete skills."""
        settings = self._settings(payload)
        library = Library(Path(settings.workspace).expanduser() if settings.workspace else None)
        action = str(payload.get("action") or "list")
        skill_id = str(payload.get("id") or "")
        try:
            if action == "get":
                skill = library.get(skill_id)
                if skill is None:
                    raise SchemaError(f"no skill {skill_id!r}")
                self._json(200, {"skill": skill.to_dict(), "text": skill.raw()})
                return
            if action == "save":
                saved = library.save(str(payload.get("text") or ""), skill_id or None)
                self._json(200, {"skill": saved.to_dict(), "skills": library.catalogue()})
                return
            if action == "upload":
                # A file the user picked in their own browser: markdown, or a
                # zip of markdown. Read, never run.
                import base64
                import binascii

                try:
                    data = base64.b64decode(str(payload.get("data") or ""), validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise SchemaError("that upload could not be read") from exc
                added, ignored = library.install_file(str(payload.get("name") or ""), data)
                self._json(200, {"skills": library.catalogue(),
                                 "installed": [s.to_dict() for s in added],
                                 "ignored": ignored[:20]})
                return
            if action == "install":
                saved = library.install(str(payload.get("url") or ""))
                self._json(200, {"skill": saved.to_dict(), "skills": library.catalogue()})
                return
            if action == "delete":
                if not library.delete(skill_id):
                    raise SchemaError("only skills in your workspace can be deleted")
            elif action != "list":
                raise SchemaError(f"unknown skills action {action!r}")
        except SkillError as exc:
            raise SchemaError(str(exc)) from exc
        self._json(200, {"skills": library.catalogue(),
                         "folder": str(library.folder) if library.folder else None})

    def _mcp(self, payload: dict) -> None:
        """List, add, remove or toggle the MCP servers of this workspace.

        A server is a program that will run as the user, so the list lives in
        a file in the workspace and only ever changes from here — never from a
        model's output, and never from another site (the origin check above).
        """
        from . import mcp

        settings = self._settings(payload)
        workspace = self._require_workspace(settings)
        action = str(payload.get("action") or "list")
        servers = mcp.read_config(workspace)
        name = str(payload.get("name") or "").strip()
        if action == "add":
            try:
                spec = mcp.ServerSpec.from_dict(payload.get("server") or {})
            except SchemaError as exc:
                raise SchemaError(str(exc)) from exc
            servers = [s for s in servers if s.name != spec.name] + [spec]
            mcp.write_config(workspace, servers)
        elif action == "remove":
            mcp.write_config(workspace, [s for s in servers if s.name != name])
            servers = [s for s in servers if s.name != name]
        elif action == "toggle":
            for server in servers:
                if server.name == name:
                    server.enabled = not server.enabled
            mcp.write_config(workspace, servers)
        elif action != "list":
            raise SchemaError(f"unknown mcp action {action!r}")
        # Starting them is how their tools are discovered; a broken one reports.
        fleet = mcp.Fleet.start(workspace, servers)
        try:
            self._json(200, {"servers": fleet.status(),
                             "configured": [s.to_dict() for s in servers],
                             "path": str(workspace / mcp.CONFIG)})
        finally:
            fleet.stop()

    def _memory(self, payload: dict) -> None:
        """Read, forget or clear what the harness remembers about the user."""
        harness = self.pool.get(self._settings(payload))
        memory = harness.memory()
        action = str(payload.get("action") or "list")
        if action == "forget":
            memory.forget(str(payload.get("text") or ""))
        elif action == "clear":
            memory.clear()
        elif action == "add":
            memory.add(str(payload.get("text") or ""), source="typed")
        elif action != "list":
            raise SchemaError(f"unknown memory action {action!r}")
        self._json(200, memory.to_dict())

    def _schedule(self, payload: dict) -> None:
        """Create, list, toggle, run or remove a scheduled task."""
        raw_settings = payload.get("settings") or {}
        settings = Settings.from_request(raw_settings, fallback=self.config)
        scheduler = self.schedules.for_workspace(settings.workspace)
        if scheduler is None:
            raise ConfigError(
                "scheduling needs a workspace: the task has to be stored somewhere"
            )
        action = str(payload.get("action") or "list")
        if action == "add":
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt:
                raise SchemaError("a scheduled task needs a prompt")
            try:
                scheduler.add(
                    name=str(payload.get("name") or prompt),
                    prompt=prompt,
                    every_minutes=int(payload.get("every_minutes") or 60),
                    state=str(payload.get("state") or ""),
                    # The job carries the settings it will run under, key
                    # included; nothing else is available at 3am.
                    settings=raw_settings,
                    until=str(payload.get("until") or ""),
                    max_runs=int(payload.get("max_runs") or 0),
                    start_now=bool(payload.get("start_now")),
                )
            except ValueError as exc:
                raise SchemaError(str(exc)) from exc
        elif action == "remove":
            scheduler.remove(str(payload.get("id") or ""))
        elif action == "toggle":
            scheduler.toggle(str(payload.get("id") or ""))
        elif action == "run":
            # Run in the background: a loop round can take a minute, and the
            # request should not hang for it.
            job_id = str(payload.get("id") or "")
            threading.Thread(target=scheduler.run_now, args=(job_id,), daemon=True).start()
        elif action != "list":
            raise SchemaError(f"unknown schedule action {action!r}")
        self._json(200, {"jobs": scheduler.listing(), "workspace": settings.workspace})

    def _require_workspace(self, settings) -> Path:
        """A run has to have somewhere to put what it produces."""
        if not settings.workspace:
            raise ConfigError(
                "choose a workspace first — that is where the conversation and "
                "everything it produces are written"
            )
        path = Path(settings.workspace).expanduser()
        if not path.is_dir():
            raise ConfigError(f"the workspace {path} does not exist")
        return path

    def _subagent(self, payload: dict) -> None:
        """One worker from a finished run, spoken to directly. Streamed.

        It is a new operation on the old run: same run id, its own operation
        id, so its events never overwrite the original record.
        """
        settings = self._settings(payload)
        harness = self.pool.get(settings)
        task = parse_task(payload.get("task"))
        outlet = harness.run_events(payload.get("run_id"),
                                    operation_id="revise-" + run_events.new_run_id()[4:12])
        events = harness.revise(task, payload.get("step") or {}, str(payload.get("message") or ""),
                                thread=payload.get("thread"), outline=payload.get("outline"),
                                others=payload.get("others"), events=outlet,
                                answer_mode=str(payload.get("answer_mode") or ""))
        self._sse(events, harness.secrets())

    def _sync(self, payload: dict) -> None:
        """Rewrite a stale whole from its current parts. Streamed."""
        settings = self._settings(payload)
        harness = self.pool.get(settings)
        task = parse_task(payload.get("task"))
        parts = [p for p in (payload.get("parts") or []) if isinstance(p, dict)][:12]
        outlet = harness.run_events(payload.get("run_id"),
                                    operation_id="sync-" + run_events.new_run_id()[4:12])
        self._sse(harness.sync(task, parts, outlet), harness.secrets())

    def _sse(self, events, secrets=()) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for event in events:
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except HarnessError as exc:
            body = {"type": "error", **exc.public_dict(secrets)["error"]}
            self.wfile.write(f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode())
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            body = {"type": "error", "code": "internal_error",
                    "message": redact(f"{type(exc).__name__}: {exc}", secrets)}
            self.wfile.write(f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode())
        finally:
            close = getattr(events, "close", None)
            if close:
                close()     # a reader that went away stops the run's next step

    # -- recorded runs --------------------------------------------------------- #

    def _run_file(self, workspace: Path, run_id: Any) -> Path:
        """The record for ``run_id`` — and only ever inside this workspace."""
        valid = run_events.valid_run_id(run_id)
        if not valid:
            raise SchemaError("that is not a run id")
        folder = (workspace / RUNS_DIR).resolve()
        path = (folder / f"{valid}.jsonl").resolve()
        if path.parent != folder:
            raise SchemaError("that is not a run id")
        return path

    def _runs(self, payload: dict) -> None:
        """Cancel a run in flight, or list, read and export recorded runs.

        Reads are confined to ``<workspace>/.jevia/runs``; ids are checked
        against a strict pattern, never used as paths. A cancel only reaches a
        run started for the same workspace. Nothing here calls a model.
        """
        settings = self._settings(payload)
        workspace = self._require_workspace(settings).resolve()
        action = str(payload.get("action") or "list")
        secrets = [c.api_key for c in settings.roster.credentials.values()]
        if action == "cancel":
            run = run_events.active(str(payload.get("run_id") or ""))
            if run is None or not run.workspace or \
                    Path(run.workspace).expanduser().resolve() != workspace:
                self._json(200, {"status": "not_running"})
                return
            run.request_cancel("user")
            # The flag stops the next model or tool call. A request already
            # with a provider may still finish and may still be billed.
            self._json(200, {"status": "cancel_requested", "run_id": run.run_id})
            return
        if action == "answer":
            # The run is blocked on a question; this is the person answering.
            run = run_events.active(str(payload.get("run_id") or ""))
            if run is None or not run.workspace or \
                    Path(run.workspace).expanduser().resolve() != workspace:
                self._json(200, {"status": "not_running"})
                return
            delivered = run.answer(str(payload.get("question_id") or ""),
                                   str(payload.get("answer") or "")[:2000])
            self._json(200, {"status": "answered" if delivered else "not_waiting"})
            return
        if action == "list":
            folder = workspace / RUNS_DIR
            rows = []
            if folder.is_dir():
                files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                for path in files[:100]:
                    if run_events.valid_run_id(path.stem):
                        stat = path.stat()
                        rows.append({"run_id": path.stem, "bytes": stat.st_size,
                                     "modified": int(stat.st_mtime)})
            self._json(200, {"runs": rows})
            return
        if action in ("get", "export"):
            path = self._run_file(workspace, payload.get("run_id"))
            if not path.is_file():
                raise SchemaError("no record for that run")
            events = []
            for line in path.read_text(encoding="utf-8").splitlines()[:run_events.MAX_LOGGED]:
                try:
                    events.append(scrub(json.loads(line), secrets))
                except json.JSONDecodeError:
                    continue
            body = {"format": "jevia.decision-events", "event_version": run_events.EVENT_VERSION,
                    "run_id": path.stem, "source": "recorded", "events": events}
            if action == "export":
                # Scrubbed a second time on the way out, whatever the file holds.
                text = redact(json.dumps(body, ensure_ascii=False, indent=1), secrets)
                self._send(200, text.encode("utf-8"), "application/json; charset=utf-8",
                           {"Content-Disposition": f'attachment; filename="{path.stem}.json"'})
                return
            self._json(200, body)
            return
        raise SchemaError(f"unknown runs action {action!r}")

    def _run_stream(self, payload: dict) -> None:
        """Server-sent events: plan, decisions, selection, deltas, done."""
        settings = self._settings(payload)
        workspace = self._require_workspace(settings)
        harness = self.pool.get(settings)
        task = parse_task(payload.get("task"))
        chat = payload.get("chat") or {}
        outlet = harness.run_events(payload.get("run_id"))
        secrets = harness.secrets()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        def emit(event: dict) -> None:
            chunk = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        stream = harness.stream(task, outlet)
        try:
            for event in stream:
                outcome = event.pop("result_object", None)
                emit(event)
                if event.get("type") == "done":
                    # The work belongs to the user, so it lands in their folder
                    # as plain files before the request is over.
                    saved = Store(workspace).save_turn(
                        chat_id=str(chat.get("id") or "session"),
                        title=str(chat.get("title") or task.prompt),
                        prompt=task.prompt,
                        state=task.state_text,
                        result=event.get("result") or {},
                        created=float(chat.get("created") or 0) or None,
                        evidence=outcome.evidence.as_text() if outcome and outcome.evidence else "",
                    )
                    if saved:
                        emit({"type": "saved", **saved.to_dict(workspace)})
        except HarnessError as exc:
            emit({"type": "error", **exc.public_dict(secrets)["error"]})
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            emit({"type": "error", "code": "internal_error",
                  "message": redact(f"{type(exc).__name__}: {exc}", secrets)})
        finally:
            # A reader that left stops the run at its next step; the request
            # already with a provider is not claimed to be stopped.
            stream.close()


class PermissionDenied(HarnessError):
    status = 403
    code = "forbidden"


def serve(config: Optional[Config] = None) -> None:
    config = config or load_config()
    Handler.config = config
    Handler.pool = HarnessPool()
    Handler.schedules = Schedules(config)
    address = (config.host, config.port)
    httpd = ThreadingHTTPServer(address, Handler)
    httpd.daemon_threads = True
    url = f"http://{config.host}:{config.port}"
    print(f"\n  Jev Harness — {url}")
    print(f"  key from .env : {'yes, ' + config.redacted_key() if config.has_key else 'no (enter one in the UI)'}")
    print(f"  jev model     : {config.jev_model}")
    print("  ctrl-c to stop\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        httpd.server_close()
