"""The demo application in examples/, driven with a real HTTP client (http.client)."""
import http.client
import importlib.util
import json
import os

import pytest

from helpers import running

spec = importlib.util.spec_from_file_location("demo", os.path.join(os.path.dirname(__file__), "..", "examples", "demo.py"))
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@pytest.fixture
def client():
    with running(demo.build_app()) as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        yield conn
        conn.close()


def call(conn, method, path, body=None, headers=None):
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    return res, res.read()


def test_notes_can_be_created_read_listed_and_deleted_over_one_connection(client):
    res, body = call(client, "POST", "/api/notes", json.dumps({"text": " buy milk "}), {"Content-Type": "application/json"})
    assert res.status == 201 and res.getheader("Location") == "/api/notes/1"
    assert json.loads(body) == {"id": 1, "text": "buy milk"}
    assert res.getheader("Server-Timing").startswith("app;dur=")                    # the middleware ran
    assert json.loads(call(client, "GET", "/api/notes/1")[1]) == {"id": 1, "text": "buy milk"}
    assert json.loads(call(client, "GET", "/api/notes")[1]) == {"notes": [{"id": 1, "text": "buy milk"}]}
    res, body = call(client, "DELETE", "/api/notes/1")
    assert res.status == 204 and body == b""
    assert call(client, "GET", "/api/notes/1")[0].status == 404


def test_bad_notes_are_refused(client):
    assert call(client, "POST", "/api/notes", "{not json")[0].status == 400
    assert call(client, "POST", "/api/notes", json.dumps({"text": "  "}))[0].status == 422
    assert call(client, "POST", "/api/notes", json.dumps([1, 2]))[0].status == 422
    res, _ = call(client, "PUT", "/api/notes")
    assert res.status == 405 and "GET" in res.getheader("Allow")


def test_the_visit_cookie_counts_up(client):
    res, body = call(client, "GET", "/api/visits")
    assert json.loads(body) == {"visits": 1} and "visits=1" in res.getheader("Set-Cookie")
    res, body = call(client, "GET", "/api/visits", headers={"Cookie": "visits=41"})
    assert json.loads(body) == {"visits": 42}


def test_static_files_and_the_index_come_from_the_public_directory(client):
    res, body = call(client, "GET", "/")
    assert res.status == 200 and b"It works" in body and res.getheader("Cache-Control") == "max-age=60"
    assert call(client, "GET", "/nope.html")[0].status == 404
