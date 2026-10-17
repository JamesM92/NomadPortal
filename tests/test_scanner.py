"""The virus-scan client, tested against a small fake clamd that speaks the
real INSTREAM protocol over a Unix socket, so the bytes on the wire are
checked, not just the Python around them."""

import os
import socket
import struct
import threading

import pytest

from nomadnet_web.scanner import (ClamdScanner, NullScanner, ScanResult,
                                  build_scanner_from_config)


class FakeClamd:
    """Accepts one connection per scan, records what it received, and answers
    with ``reply`` (bytes, sent as-is), or hangs up if ``reply`` is None."""

    def __init__(self, path, reply=b"stream: OK\0"):
        self.path, self.reply = path, reply
        self.received = b""
        self.chunks = []
        self.command = b""
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _recv(self, conn, n):
        buf = b""
        while len(buf) < n:
            got = conn.recv(n - len(buf))
            if not got:
                raise EOFError
            buf += got
        return buf

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        with conn:
            try:
                self.command = self._recv(conn, len(b"zINSTREAM\0"))
                while True:
                    (n,) = struct.unpack("!I", self._recv(conn, 4))
                    if n == 0:
                        break
                    self.chunks.append(self._recv(conn, n))
                self.received = b"".join(self.chunks)
                if self.reply is not None:
                    conn.sendall(self.reply)
            except EOFError:
                pass

    def close(self):
        self.srv.close()


@pytest.fixture
def sock_path(tmp_path):
    return str(tmp_path / "clamd.sock")


def scan(sock_path, reply, content=b"hello", **kw):
    fake = FakeClamd(sock_path, reply)
    try:
        return ClamdScanner(socket_path=sock_path, timeout=3, **kw).scan(content, "f.bin"), fake
    finally:
        fake.thread.join(2)
        fake.close()


class TestVerdicts:
    def test_ok_is_clean_and_the_bytes_arrive_intact(self, sock_path):
        data = os.urandom(200_000)                       # several 64 KB chunks
        r, fake = scan(sock_path, b"stream: OK\0", data)
        assert r.verdict == "clean" and r.clean and not r.blocked
        assert fake.command == b"zINSTREAM\0" and fake.received == data
        assert len(fake.chunks) == 4 and all(len(c) <= 65536 for c in fake.chunks)

    def test_found_is_infected_with_the_signature(self, sock_path):
        r, _ = scan(sock_path, b"stream: Eicar-Test-Signature FOUND\0")
        assert r.verdict == "infected" and r.blocked
        assert r.signature == "Eicar-Test-Signature" and "FOUND" in r.detail

    def test_error_reply_is_unavailable_not_clean(self, sock_path):
        r, _ = scan(sock_path, b"INSTREAM size limit exceeded. ERROR\0")
        assert r.verdict == "unavailable" and not r.clean

    def test_a_reply_nobody_understands_is_unavailable_not_clean(self, sock_path):
        r, _ = scan(sock_path, b"banana\0")
        assert r.verdict == "unavailable" and "banana" in r.detail

    def test_hanging_up_without_an_answer_is_unavailable(self, sock_path):
        r, _ = scan(sock_path, None)
        assert r.verdict == "unavailable"

    def test_an_empty_file_is_scanned_not_skipped(self, sock_path):
        r, fake = scan(sock_path, b"stream: OK\0", b"")
        assert r.verdict == "clean" and fake.received == b""


class TestFailureModes:
    def test_no_daemon_is_unavailable_and_says_so(self, sock_path):
        r = ClamdScanner(socket_path=sock_path, timeout=1).scan(b"x")
        assert r.verdict == "unavailable" and "unreachable" in r.detail

    def test_over_the_size_limit_is_too_large_without_connecting(self, sock_path):
        r = ClamdScanner(socket_path=sock_path, max_bytes=10).scan(b"x" * 11)
        assert r.verdict == "too-large" and "10" in r.detail

    def test_exactly_the_limit_is_scanned(self, sock_path):
        r, _ = scan(sock_path, b"stream: OK\0", b"x" * 10, max_bytes=10)
        assert r.verdict == "clean"

    def test_needs_somewhere_to_connect(self):
        with pytest.raises(ValueError):
            ClamdScanner()


def test_the_null_scanner_says_skipped_never_clean():
    r = NullScanner().scan(b"anything")
    assert r.verdict == "skipped" and not r.clean and not r.blocked


def test_result_dict_shape():
    assert ScanResult("clean", "clamd").to_dict() == \
        {"verdict": "clean", "engine": "clamd", "signature": "", "detail": ""}


class TestBuildFromConfig:
    @pytest.mark.parametrize("mode", ["off", "", "false", "no", "0", "OFF", "banana"])
    def test_off_and_unknown_modes_disable_scanning(self, mode):
        s, required = build_scanner_from_config({"VIRUS_SCAN": mode})
        assert isinstance(s, NullScanner) and required is False

    def test_missing_setting_means_off(self):
        s, required = build_scanner_from_config({})
        assert isinstance(s, NullScanner) and required is False

    def test_clamd_mode_is_optional_scanning(self):
        s, required = build_scanner_from_config({"VIRUS_SCAN": "clamd", "CLAMD_SOCKET": "/x"})
        assert isinstance(s, ClamdScanner) and s.socket_path == "/x" and required is False

    def test_required_mode_marks_a_clean_scan_mandatory(self):
        s, required = build_scanner_from_config({"VIRUS_SCAN": " Required ", "CLAMD_HOST": "h", "CLAMD_PORT": "3311"})
        assert isinstance(s, ClamdScanner) and required is True
        assert (s.host, s.port, s.socket_path) == ("h", 3311, None)

    def test_the_standard_debian_socket_is_the_default(self):
        s, _ = build_scanner_from_config({"VIRUS_SCAN": "clamd"})
        assert s.socket_path == "/var/run/clamav/clamd.ctl"

    def test_size_limit_comes_from_config(self):
        s, _ = build_scanner_from_config({"VIRUS_SCAN": "clamd", "VIRUS_SCAN_MAX_BYTES": "5"})
        assert s.max_bytes == 5
