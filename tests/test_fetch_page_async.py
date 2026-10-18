"""NodeBrowser.fetch_page_async: the worker that runs a fetch on a thread and
reports into the job table, including the virus-scan step for files. The
network fetch itself is replaced; __init__ (which starts Reticulum) is skipped."""

import time


from nomadnet_web.browser import NodeBrowser
from nomadnet_web.fetch_jobs import FetchJobs
from nomadnet_web.scanner import ScanResult

NODE = "ab" * 16


class Scanner:
    engine_name = "fake"

    def __init__(self, result=None, boom=False):
        self.result, self.boom, self.seen = result, boom, []

    def scan(self, content, filename=""):
        self.seen.append(filename)
        if self.boom:
            raise RuntimeError("scanner exploded")
        return self.result


def make(fetch, scanner=None, required=False):
    b = NodeBrowser.__new__(NodeBrowser)          # skip __init__: it starts Reticulum
    b._fetch_jobs = FetchJobs()
    b.fetch_page = fetch
    b.scanner, b.scan_required = scanner, required
    return b


def wait_done(b, jid, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        j = b.get_job(jid)
        if j and j["status"] not in ("fetching", "scanning"):
            return j
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_a_successful_page_fetch_is_done_and_reports_progress_and_sizes():
    def fetch(node, path, fields, progress_cb=None, sizes_cb=None, identify_with=None):
        progress_cb(0.5)
        sizes_cb(10, 8)
        return b">page", None
    b = make(fetch)
    j = wait_done(b, b.fetch_page_async(NODE, "/page/index.mu"))
    assert j["status"] == "done" and j["content"] == b">page" and j["progress"] == 1.0
    assert (j["response_size"], j["transfer_size"]) == (10, 8) and j["scan_result"] is None


def test_a_failed_fetch_is_an_error_with_the_reason():
    b = make(lambda *a, **k: (None, "Link timed out"))
    j = wait_done(b, b.fetch_page_async(NODE, "/page/index.mu"))
    assert j["status"] == "error" and j["error"] == "Link timed out" and j["content"] is None


def test_the_fetch_gets_its_arguments():
    seen = {}

    def fetch(node, path, fields, **kw):
        seen.update(node=node, path=path, fields=fields, identify=kw["identify_with"])
        return b"x", None
    b = make(fetch)
    wait_done(b, b.fetch_page_async(NODE, "/page/f.mu", field_data={"a": "1"}, identify_with="ID"))
    assert seen == {"node": NODE, "path": "/page/f.mu", "fields": {"a": "1"}, "identify": "ID"}


def test_a_crash_in_the_fetch_becomes_an_internal_error_not_a_hang():
    def fetch(*a, **k):
        raise RuntimeError("kaboom")
    b = make(fetch)
    j = wait_done(b, b.fetch_page_async(NODE, "/"))
    assert j["status"] == "error" and "Internal error" in j["error"] and "kaboom" in j["error"]


class TestFileScanning:
    def go(self, scanner, required=False, path="/file/doc.pdf"):
        b = make(lambda *a, **k: (b"filebytes", None), scanner, required)
        return wait_done(b, b.fetch_page_async(NODE, path))

    def test_pages_are_never_scanned(self):
        s = Scanner(ScanResult("infected", "fake", "Evil"))
        j = self.go(s, path="/page/index.mu")
        assert j["status"] == "done" and s.seen == []

    def test_a_clean_file_passes_with_the_verdict_recorded(self):
        j = self.go(Scanner(ScanResult("clean", "fake")))
        assert j["status"] == "done" and j["content"] == b"filebytes"
        assert j["scan_result"]["verdict"] == "clean"

    def test_an_infected_file_is_blocked_and_its_bytes_dropped(self):
        s = Scanner(ScanResult("infected", "fake", signature="Eicar"))
        j = self.go(s)
        assert j["status"] == "error" and "Eicar" in j["error"] and j["content"] is None
        assert s.seen == ["doc.pdf"]

    def test_an_unavailable_scanner_lets_the_file_through_unless_required(self):
        s = Scanner(ScanResult("unavailable", "fake", detail="down"))
        assert self.go(s)["status"] == "done"

    def test_an_unavailable_scanner_blocks_when_a_scan_is_required(self):
        s = Scanner(ScanResult("unavailable", "fake", detail="down"))
        j = self.go(s, required=True)
        assert j["status"] == "error" and "required" in j["error"] and j["content"] is None

    def test_a_scanner_that_raises_counts_as_unavailable(self):
        j = self.go(Scanner(boom=True), required=True)
        assert j["status"] == "error" and "exploded" in j["scan_result"]["detail"]

    def test_no_scanner_means_no_scan(self):
        j = self.go(None)
        assert j["status"] == "done" and j["scan_result"] is None
