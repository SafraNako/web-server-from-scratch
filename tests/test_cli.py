"""The ``kserve`` command line, run as a real subprocess."""
import os
import signal
import subprocess
import sys
import time
import urllib.request

import pytest

ENV = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))


def test_it_serves_a_directory_and_stops_on_sigint(tmp_path):
    (tmp_path / "hello.txt").write_text("hi there\n")
    proc = subprocess.Popen([sys.executable, "-m", "kendi_sunucu", str(tmp_path), "-p", "0"], env=ENV, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stderr.readline()                                         # "Serving http://127.0.0.1:PORT/ ..."
        assert line.startswith("Serving http://"), line
        url = line.split()[1]
        with urllib.request.urlopen(url.rstrip("/") + "/hello.txt", timeout=5) as res:
            assert res.read() == b"hi there\n"
            assert res.headers["Server"].startswith("kendi-sunucu/")
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stderr.close()


def test_a_missing_directory_is_an_error(tmp_path):
    done = subprocess.run([sys.executable, "-m", "kendi_sunucu", str(tmp_path / "nope")], env=ENV, capture_output=True, text=True, timeout=10)
    assert done.returncode == 1
    assert "not a directory" in done.stderr


def test_a_taken_port_is_an_error():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        done = subprocess.run([sys.executable, "-m", "kendi_sunucu", ".", "-p", str(s.getsockname()[1])], env=ENV, capture_output=True, text=True, timeout=10)
    finally:
        s.close()
    assert done.returncode == 1
    assert "cannot listen" in done.stderr
