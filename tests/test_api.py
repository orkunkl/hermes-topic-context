"""HTTP contract tests for the topic-context backend.

Hermes itself is not importable from the plugin's own test environment, so the
two host modules the API layer touches (``hermes_constants``, ``hermes_state``)
are stubbed. That keeps these tests hermetic: they pin the HTTP contract —
status codes, payload shape, and the fact that this plugin only ever reads.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from _helpers import StubDB, msg, session

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

PREFIX = "/api/plugins/topic-context"


@pytest.fixture
def make_client(monkeypatch):
    """Import ``plugin_api.py`` against a stubbed Hermes and build a TestClient."""
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    def build(db):
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path("/tmp/fake-hermes-home")
        monkeypatch.setitem(sys.modules, "hermes_constants", constants)

        state = types.ModuleType("hermes_state")

        def _session_db(**kwargs):
            # A class means the test wants to observe how the store is opened.
            return db(**kwargs) if isinstance(db, type) else db

        state.SessionDB = _session_db
        monkeypatch.setitem(sys.modules, "hermes_state", state)

        path = DASHBOARD_DIR / "plugin_api.py"
        spec = importlib.util.spec_from_file_location("topic_context_api_under_test", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["topic_context_api_under_test"] = module
        spec.loader.exec_module(module)

        app = fastapi.FastAPI()
        app.include_router(module.router, prefix=PREFIX)
        return TestClient(app), module

    return build


@pytest.fixture
def client(make_client):
    db = StubDB(
        session=session(),
        messages=[
            msg("user", "wire the pane into plugin.js and run /plan"),
            msg("assistant", "Working on plugin.js now."),
            msg("tool", "{}", tool_name="write_file"),
        ],
        hits=[{"session_id": "s9", "title": "Prior work", "snippet": "x", "session_started": 1}],
    )
    test_client, _ = make_client(db)
    return test_client


# ------------------------------------------------------------------- routes


def test_health_reports_ok_and_scope(client):
    body = client.get(f"{PREFIX}/health").json()
    assert body["ok"] is True
    assert body["plugin"] == "topic-context"
    assert body["hermes_home"] == "/tmp/fake-hermes-home"
    assert body["session_count"] == 1


def test_context_returns_every_section(client):
    response = client.get(f"{PREFIX}/context", params={"session_id": "s1"})
    assert response.status_code == 200
    body = response.json()

    assert body["session"]["id"] == "s1"
    assert body["brief_source"] == "compiled"
    assert "## Context" in body["brief"]
    assert any(t["label"] == "plugin.js" for t in body["topics"])
    assert body["sessions"][0]["session_id"] == "s9"
    assert body["cognee"] == {"enabled": False}
    assert body["version"] == "0.1.0"


def test_context_defaults_to_the_newest_session(client):
    body = client.get(f"{PREFIX}/context").json()
    assert body["session"]["id"] == "s1"


def test_context_404s_on_an_unknown_session(monkeypatch, make_client):
    test_client, _ = make_client(StubDB(session=None))
    response = test_client.get(f"{PREFIX}/context", params={"session_id": "ghost"})
    assert response.status_code == 404
    assert "ghost" in response.json()["detail"]


def test_context_404s_when_no_sessions_exist(make_client):
    test_client, _ = make_client(StubDB(session=None, sessions=[]))
    assert test_client.get(f"{PREFIX}/context").status_code == 404


def test_brief_is_markdown_and_declares_no_refresh_path(client):
    body = client.get(f"{PREFIX}/brief", params={"session_id": "s1"}).json()
    assert body["session_id"] == "s1"
    assert body["brief"].startswith("## Context")
    assert body["source"] == "compiled"
    assert body["refresh_supported"] is False


def test_topics_route(client):
    body = client.get(f"{PREFIX}/topics", params={"session_id": "s1", "limit": 3}).json()
    assert body["title"] == "Wire the topic-context pane into plugin.js"
    assert 0 < len(body["topics"]) <= 3


def test_memory_route_accepts_explicit_terms(client):
    body = client.get(f"{PREFIX}/memory", params={"q": "plugin, pane"}).json()
    assert body["terms"] == ["plugin", "pane"]


def test_memory_route_defaults_terms_to_session_topics(client):
    body = client.get(f"{PREFIX}/memory", params={"session_id": "s1"}).json()
    assert body["session_id"] == "s1"
    assert body["terms"]


def test_sessions_route_excludes_the_active_session(client, make_client):
    db = StubDB(
        session=session(),
        messages=[msg("user", "plugin.js")],
        hits=[{"session_id": "s1", "snippet": "self", "session_started": 1}],
    )
    test_client, _ = make_client(db)
    body = test_client.get(f"{PREFIX}/sessions", params={"session_id": "s1"}).json()
    assert body["sessions"] == []


# ------------------------------------------------------- read-only contract


def test_every_route_is_a_get(make_client):
    """A write route would break the plugin's read-only promise.

    Asserted against the plugin's own router: newer FastAPI keeps an included
    router as one opaque entry on ``app.routes``, so the app-level list is not
    the source of truth for what this plugin declares.
    """
    _, module = make_client(StubDB(session=session(), messages=[msg("user", "hi")]))

    declared = [(route.path, sorted(route.methods)) for route in module.router.routes]
    assert declared, "expected the router to expose routes"
    for path, verbs in declared:
        assert verbs == ["GET"], f"{path} exposes {verbs}"


def test_every_declared_route_answers_a_get(client, make_client):
    """GET must be the right verb for each route (no 405)."""
    _, module = make_client(StubDB(session=session(), messages=[msg("user", "hi")]))
    for route in module.router.routes:
        response = client.get(f"{PREFIX}{route.path}", params={"session_id": "s1"})
        assert response.status_code != 405, f"{route.path} rejects GET"


def test_db_is_always_opened_read_only(make_client):
    """Guard the one thing that makes this plugin safe to leave enabled."""
    opened: list[dict] = []

    class RecordingDB(StubDB):
        def __init__(self, **kwargs):
            opened.append(kwargs)
            super().__init__(session=session(), messages=[msg("user", "plugin.js")])

    test_client, _ = make_client(RecordingDB)
    test_client.get(f"{PREFIX}/context", params={"session_id": "s1"})
    test_client.get(f"{PREFIX}/health")

    assert opened, "expected the API layer to have opened the store"
    assert all(kwargs.get("read_only") is True for kwargs in opened), opened
