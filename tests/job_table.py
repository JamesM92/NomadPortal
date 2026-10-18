"""Gives a test double the browser's job-table methods, backed by the real
``FetchJobs``, so route tests exercise the same lifecycle the app uses."""

from nomadnet_web.fetch_jobs import FetchJobs


class JobsMixin:
    def __init_jobs__(self):
        self._fetch_jobs = FetchJobs()

    def get_job(self, job_id):
        return self._fetch_jobs.get(job_id)

    def drop_job(self, job_id, grace_seconds=0):
        self._fetch_jobs.drop(job_id, grace_seconds)

    def add_finished_job(self, node_hash, path, content, error=None, **extra):
        return self._fetch_jobs.add_finished(node_hash, path, content, error, **extra)

    def flag_job(self, job_id, **flags):
        return self._fetch_jobs.flag(job_id, **flags)

    def jobs_summary(self):
        return self._fetch_jobs.summary()
