"""The table of page-fetch jobs: start, progress, finish, polling, eviction.

Ported from the old ``drop_job`` grace-window tests (a download manager that
re-requests a file URL must still find it for a short while) and extended to
the rest of the lifecycle. The clock is injected so nothing sleeps."""

import threading

import pytest

from nomadnet_web.fetch_jobs import FetchJobs


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def jobs(clock):
    return FetchJobs(clock=clock)


NODE = "AB" * 16


class TestStartAndFinish:
    def test_a_started_job_is_fetching_with_no_result(self, jobs):
        jid = jobs.start(NODE, "/page/x.mu")
        j = jobs.get(jid)
        assert j["status"] == "fetching" and j["progress"] == 0.0
        assert j["node_hash"] == NODE.lower() and j["path"] == "/page/x.mu"
        assert j["content"] is None and j["error"] is None and j["completed"] is None

    def test_ids_are_unique_and_unguessable(self, jobs):
        ids = {jobs.start(NODE, "/") for _ in range(50)}
        assert len(ids) == 50 and all(len(i) == 16 for i in ids)

    def test_progress_and_sizes_update_the_job(self, jobs):
        jid = jobs.start(NODE, "/")
        jobs.set_progress(jid, 0.4)
        jobs.set_sizes(jid, response_size=100, transfer_size=None)
        jobs.set_sizes(jid, response_size=None, transfer_size=60)
        j = jobs.get(jid)
        assert j["progress"] == 0.4 and j["response_size"] == 100 and j["transfer_size"] == 60

    def test_finishing_with_content_is_done(self, jobs, clock):
        jid = jobs.start(NODE, "/")
        clock.now += 2
        jobs.finish(jid, content=b"hi", error=None, scan_result={"verdict": "clean"})
        j = jobs.get(jid)
        assert j["status"] == "done" and j["content"] == b"hi" and j["progress"] == 1.0
        assert j["completed"] == clock.now and j["scan_result"] == {"verdict": "clean"}

    def test_finishing_with_an_error_is_error_and_keeps_progress(self, jobs):
        jid = jobs.start(NODE, "/")
        jobs.set_progress(jid, 0.3)
        jobs.finish(jid, content=None, error="timeout")
        j = jobs.get(jid)
        assert j["status"] == "error" and j["error"] == "timeout" and j["progress"] == 0.3

    def test_a_crashed_worker_is_reported_as_an_internal_error(self, jobs):
        jid = jobs.start(NODE, "/")
        jobs.crash(jid, RuntimeError("boom"))
        j = jobs.get(jid)
        assert j["status"] == "error" and "Internal error" in j["error"] and "boom" in j["error"]
        assert j["completed"] is not None

    def test_scanning_is_a_visible_state(self, jobs):
        jid = jobs.start(NODE, "/file/a")
        jobs.mark_scanning(jid)
        assert jobs.get(jid)["status"] == "scanning"

    def test_updates_to_a_vanished_job_are_ignored(self, jobs):
        jid = jobs.start(NODE, "/")
        jobs.drop(jid)
        jobs.set_progress(jid, 0.5)
        jobs.finish(jid, content=b"x", error=None)
        jobs.crash(jid, RuntimeError("late"))
        jobs.mark_scanning(jid)
        assert jobs.get(jid) is None


class TestAlreadyFinished:
    def test_a_result_known_at_once_is_a_done_job(self, jobs):
        jid = jobs.add_finished(NODE, "/page/x.mu", content=b"hello")
        j = jobs.get(jid)
        assert j["status"] == "done" and j["content"] == b"hello" and j["error"] is None
        assert j["progress"] == 1.0 and j["completed"] is not None and j["node_hash"] == NODE.lower()

    def test_an_error_known_at_once_is_an_error_job(self, jobs):
        jid = jobs.add_finished(NODE, "/file/a", content=None, error="blocked")
        j = jobs.get(jid)
        assert j["status"] == "error" and j["error"] == "blocked"

    def test_extra_fields_ride_along(self, jobs):
        jid = jobs.add_finished(NODE, "/file/a", content=b"abc", response_size=3, transfer_size=3,
                                scan_result={"verdict": "skipped"})
        j = jobs.get(jid)
        assert j["response_size"] == 3 and j["scan_result"] == {"verdict": "skipped"}


class TestFlags:
    def test_a_flag_can_be_set_on_a_running_job(self, jobs):
        jid = jobs.start(NODE, "/")
        assert jobs.flag(jid, kiosk_cacheable=True) is True
        assert jobs.get(jid)["kiosk_cacheable"] is True

    def test_flagging_an_unknown_job_says_so(self, jobs):
        assert jobs.flag("nope", kiosk_cacheable=True) is False


class TestGet:
    def test_get_returns_a_copy(self, jobs):
        jid = jobs.start(NODE, "/")
        jobs.get(jid)["status"] = "hacked"
        assert jobs.get(jid)["status"] == "fetching"

    def test_unknown_is_none(self, jobs):
        assert jobs.get("nope") is None


class TestDropGrace:
    """A download manager that re-requests the URL must not hit a 404."""

    def finished(self, jobs):
        return jobs.add_finished(NODE, "/file/x.pdf", content=b"payload")

    def test_default_drop_evicts_immediately(self, jobs):
        jid = self.finished(jobs)
        jobs.drop(jid)
        assert jobs.get(jid) is None

    def test_explicit_zero_grace_evicts_immediately(self, jobs):
        jid = self.finished(jobs)
        jobs.drop(jid, grace_seconds=0)
        assert jobs.get(jid) is None

    def test_a_positive_grace_leaves_the_job_serveable(self, jobs):
        jid = self.finished(jobs)
        jobs.drop(jid, grace_seconds=60)
        assert jobs.get(jid)["content"] == b"payload"

    def test_cleanup_keeps_it_inside_the_grace_window(self, jobs, clock):
        jid = self.finished(jobs)
        jobs.drop(jid, grace_seconds=60)
        clock.now += 59
        assert jobs.cleanup() == 0 and jobs.get(jid) is not None

    def test_cleanup_evicts_it_after_the_grace_window(self, jobs, clock):
        jid = self.finished(jobs)
        jobs.drop(jid, grace_seconds=60)
        clock.now += 61
        assert jobs.cleanup() == 1 and jobs.get(jid) is None

    def test_dropping_an_unknown_job_is_harmless(self, jobs):
        jobs.drop("nope")
        jobs.drop("nope", grace_seconds=60)


class TestCleanup:
    def test_finished_jobs_expire_by_age(self, jobs, clock):
        jid = jobs.add_finished(NODE, "/", content=b"x")
        clock.now += 301
        assert jobs.cleanup(max_age=300) == 1 and jobs.get(jid) is None

    def test_recent_ones_stay(self, jobs, clock):
        jid = jobs.add_finished(NODE, "/", content=b"x")
        clock.now += 299
        assert jobs.cleanup(max_age=300) == 0 and jobs.get(jid) is not None

    def test_running_jobs_are_never_aged_out(self, jobs, clock):
        jid = jobs.start(NODE, "/")
        clock.now += 10_000
        assert jobs.cleanup() == 0 and jobs.get(jid) is not None

    def test_starting_a_job_cleans_up_abandoned_ones(self, jobs, clock):
        old = jobs.add_finished(NODE, "/", content=b"x")
        clock.now += 1000
        jobs.start(NODE, "/new")
        assert jobs.get(old) is None


class TestSummary:
    def test_empty(self, jobs):
        assert jobs.summary() == {"count": 0, "oldest_started_seconds_ago": 0}

    def test_counts_and_oldest_age(self, jobs, clock):
        jobs.start(NODE, "/a")
        clock.now += 30
        jobs.start(NODE, "/b")
        clock.now += 5
        assert jobs.summary() == {"count": 2, "oldest_started_seconds_ago": 35}


def test_it_is_safe_under_concurrent_use(jobs):
    errors = []

    def worker(n):
        try:
            for i in range(200):
                jid = jobs.start(NODE, f"/{n}/{i}")
                jobs.set_progress(jid, 0.5)
                jobs.finish(jid, content=b"x", error=None)
                jobs.get(jid)
                jobs.drop(jid)
                jobs.summary()
        except Exception as exc:                     # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and jobs.summary()["count"] == 0
