"""Tasks that run on their own schedule, and loops that run until they are done.

A job repeats on an interval. Give it a goal and it becomes a loop: after every
run Jev is asked whether the latest output meets the goal, and a clear yes ends
it. A cap on the number of runs stops a goal that is never going to be met from
running forever.

A scheduled run has no browser behind it, so the job has to carry its own
settings — including the API key. That is a real trade and the UI says so: the
file is written with owner-only permissions inside the workspace, and a user who
does not want a key on disk simply does not schedule anything.

The loop is deliberately dull. One thread, a tick every half minute, jobs run
one at a time, and a job that throws is recorded and disabled rather than
retried into a hole.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

TICK = 20.0
MIN_INTERVAL = 1          # minutes
MAX_JOBS = 40
KEEP_OUTPUT = 4000
KEEP_HISTORY = 20
GOAL_MET = 0.75           # a loop stops on a clear yes, not a lean
DEFAULT_LOOP_RUNS = 10


@dataclass
class Job:
    id: str
    name: str
    prompt: str
    every_minutes: int = 60
    state: str = ""
    enabled: bool = True
    settings: Dict[str, Any] = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    last_run: float = 0.0
    next_run: float = 0.0
    last_ok: Optional[bool] = None
    last_error: str = ""
    last_output: str = ""
    runs: int = 0
    until: str = ""                       # a goal makes this a loop
    max_runs: int = 0                     # 0 = no cap
    status: str = "active"                # active | paused | done | failed
    history: List[Dict[str, Any]] = field(default_factory=list)
    last_saved: str = ""

    @property
    def is_loop(self) -> bool:
        return bool(self.until.strip())

    def due(self, now: float) -> bool:
        return self.enabled and self.status == "active" and now >= (self.next_run or 0)

    def schedule_next(self, now: float) -> None:
        self.next_run = now + max(MIN_INTERVAL, self.every_minutes) * 60

    def to_public(self) -> dict:
        row = asdict(self)
        row.pop("settings", None)          # never hand the key back out
        row["last_output"] = (self.last_output or "")[:1200]
        row["is_loop"] = self.is_loop
        return row


class Scheduler:
    """Owns the job file and the thread that walks it."""

    FILE = Path(".jevia") / "schedule.json"

    def __init__(self, runner: Callable[[Job], str], workspace: Optional[Path] = None) -> None:
        self.runner = runner
        self.path: Optional[Path] = (Path(workspace) / self.FILE) if workspace else None
        self.jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Jobs mid-run. "Run now" and the clock can both reach the same job at
        # once; without this it runs twice and bills twice.
        self._busy: set = set()
        self._load()

    # -- storage ------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for row in rows if isinstance(rows, list) else []:
            try:
                job = Job(**row)
            except TypeError:
                continue
            self.jobs[job.id] = job

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                rows = [asdict(j) for j in self.jobs.values()]
            self.path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            os.chmod(self.path, 0o600)     # it holds a key
        except OSError:
            return

    # -- jobs --------------------------------------------------------------- #

    def add(self, *, name: str, prompt: str, every_minutes: int,
            settings: Dict[str, Any], state: str = "", until: str = "",
            max_runs: int = 0, start_now: bool = False) -> Job:
        if len(self.jobs) >= MAX_JOBS:
            raise ValueError("too many scheduled tasks")
        until = (until or "").strip()
        job = Job(
            id=uuid.uuid4().hex[:10],
            name=(name or prompt)[:60].strip(),
            prompt=prompt.strip(),
            every_minutes=max(MIN_INTERVAL, int(every_minutes)),
            state=state or "",
            settings=settings or {},
            until=until,
            # A loop without a cap is a bill without a ceiling.
            max_runs=int(max_runs) if max_runs else (DEFAULT_LOOP_RUNS if until else 0),
        )
        if start_now:
            job.next_run = 0.0
        else:
            job.schedule_next(time.time())
        with self._lock:
            self.jobs[job.id] = job
        self._save()
        return job

    def remove(self, job_id: str) -> bool:
        with self._lock:
            gone = self.jobs.pop(job_id, None) is not None
        if gone:
            self._save()
        return gone

    def toggle(self, job_id: str, enabled: Optional[bool] = None) -> Optional[Job]:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            job.enabled = (not job.enabled) if enabled is None else bool(enabled)
            if job.enabled:
                # Resuming a finished or failed job starts it over.
                if job.status in ("done", "failed", "paused"):
                    job.status = "active"
                    if job.max_runs and job.runs >= job.max_runs:
                        job.runs = 0
                job.schedule_next(time.time())
            else:
                job.status = "paused"
        self._save()
        return job

    def listing(self) -> List[dict]:
        with self._lock:
            jobs = sorted(self.jobs.values(), key=lambda j: -j.created)
            busy = set(self._busy)
        return [dict(j.to_public(), running=j.id in busy) for j in jobs]

    # -- running ------------------------------------------------------------ #

    def run_now(self, job_id: str) -> Optional[Job]:
        with self._lock:
            job = self.jobs.get(job_id)
        if job is None:
            return None
        self._run(job)
        return job

    def _claim(self, job: Job) -> bool:
        with self._lock:
            if job.id in self._busy:
                return False
            self._busy.add(job.id)
            return True

    def _run(self, job: Job) -> None:
        if not self._claim(job):
            return
        try:
            self._run_claimed(job)
        finally:
            with self._lock:
                self._busy.discard(job.id)

    def _run_claimed(self, job: Job) -> None:
        now = time.time()
        job.last_run = now
        job.runs += 1
        entry: Dict[str, Any] = {"at": now, "run": job.runs}
        try:
            outcome = self.runner(job)
            all_met = None
            if isinstance(outcome, dict):
                output = outcome.get("output") or ""
                met = outcome.get("goal_met")
                all_met = outcome.get("goal_all_met")
                if outcome.get("conditions") is not None:
                    entry["conditions"] = outcome["conditions"]
                job.last_saved = outcome.get("saved") or job.last_saved
            else:
                output, met = outcome or "", None
            job.last_output = output[:KEEP_OUTPUT]
            job.last_ok = True
            job.last_error = ""
            entry.update(ok=True, preview=output[:280], goal_met=met)
            # Every condition must pass when they were checked one by one; the
            # single number is only for records that predate that.
            reached = all_met if all_met is not None else (met is not None and met >= GOAL_MET)
            if job.is_loop and reached:
                job.status = "done"
                job.enabled = False
                entry["stopped"] = "goal met"
        except Exception as exc:  # noqa: BLE001 - a bad job must not stop the rest
            job.last_ok = False
            job.last_error = f"{type(exc).__name__}: {exc}"[:400]
            job.enabled = False
            job.status = "failed"
            entry.update(ok=False, error=job.last_error)
        if job.status == "active" and job.max_runs and job.runs >= job.max_runs:
            job.status = "done"
            job.enabled = False
            entry["stopped"] = "run limit"
        job.history = (job.history + [entry])[-KEEP_HISTORY:]
        job.schedule_next(now)
        self._save()

    def tick(self, now: Optional[float] = None) -> int:
        now = now if now is not None else time.time()
        with self._lock:
            due = [j for j in self.jobs.values() if j.due(now) and j.id not in self._busy]
        for job in due:
            self._run(job)
        return len(due)

    # -- the thread ---------------------------------------------------------- #

    def start(self) -> None:
        if self._thread or not self.enabled:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                continue
