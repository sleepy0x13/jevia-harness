import tempfile
import threading
import time
import unittest
from pathlib import Path

from jevharness.scheduler import Scheduler


class TestLoops(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def loop(self, scores, **kw):
        values = iter(scores)
        scheduler = Scheduler(lambda job: {"output": f"round {job.runs}",
                                           "goal_met": next(values, 0.0)}, self.dir)
        job = scheduler.add(name="n", prompt="p", every_minutes=1, settings={},
                            until="done", start_now=True, **kw)
        return scheduler, job

    def spin(self, scheduler, job, times):
        for _ in range(times):
            job.next_run = 0
            scheduler.tick()

    def test_a_loop_stops_when_the_goal_is_met(self):
        scheduler, job = self.loop([0.2, 0.9, 0.9])
        self.spin(scheduler, job, 5)
        self.assertEqual(job.runs, 2)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.history[-1]["stopped"], "goal met")

    def test_a_lean_is_not_a_yes(self):
        scheduler, job = self.loop([0.6, 0.6, 0.6], max_runs=3)
        self.spin(scheduler, job, 5)
        self.assertEqual(job.history[-1]["stopped"], "run limit")

    def test_a_loop_without_a_cap_gets_one(self):
        _, job = self.loop([0.1])
        self.assertGreater(job.max_runs, 0)

    def test_a_plain_schedule_has_no_cap_and_no_goal(self):
        scheduler = Scheduler(lambda job: "x", self.dir)
        job = scheduler.add(name="n", prompt="p", every_minutes=5, settings={})
        self.assertFalse(job.is_loop)
        self.assertEqual(job.max_runs, 0)

    def test_a_failure_stops_the_job_and_is_recorded(self):
        def boom(job):
            raise RuntimeError("no key")
        scheduler = Scheduler(boom, self.dir)
        job = scheduler.add(name="n", prompt="p", every_minutes=1, settings={}, start_now=True)
        scheduler.tick()
        self.assertEqual(job.status, "failed")
        self.assertIn("no key", job.last_error)

    def test_resuming_a_finished_loop_starts_it_over(self):
        scheduler, job = self.loop([0.95, 0.1])
        self.spin(scheduler, job, 2)
        self.assertEqual(job.status, "done")
        scheduler.toggle(job.id)
        self.assertEqual(job.status, "active")

    def test_the_same_job_never_runs_twice_at_once(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow(job):
            calls.append(1)
            started.set()
            release.wait(2)
            return {"output": "x", "goal_met": 0.0}

        scheduler = Scheduler(slow, self.dir)
        job = scheduler.add(name="n", prompt="p", every_minutes=1, settings={}, start_now=True)
        worker = threading.Thread(target=scheduler.run_now, args=(job.id,))
        worker.start()
        started.wait(2)
        scheduler.tick()                 # the clock arrives mid-run
        scheduler.run_now(job.id)        # and so does a second click
        release.set()
        worker.join(2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(job.runs, 1)

    def test_the_key_is_never_listed(self):
        scheduler = Scheduler(lambda job: "x", self.dir)
        scheduler.add(name="n", prompt="p", every_minutes=5, settings={"secret": "sk"})
        self.assertNotIn("settings", scheduler.listing()[0])

    def test_jobs_survive_a_restart(self):
        scheduler, job = self.loop([0.1])
        again = Scheduler(lambda job: "x", self.dir)
        self.assertIn(job.id, again.jobs)
        self.assertEqual(again.jobs[job.id].until, "done")


if __name__ == "__main__":
    unittest.main()
