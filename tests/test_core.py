"""Unit tests for the topic-context core.

These run against a stub session store, so they assert the *contract* of the
aggregation — what makes it into a topic, what a brief must contain — without
depending on a live Hermes install or a real ``state.db``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import topic_context_core as core  # noqa: E402

from _helpers import StubDB, msg  # noqa: E402
from _helpers import session as session_row  # noqa: E402


@pytest.fixture
def session():
    return session_row()


# --------------------------------------------------------------------- topics


def test_extracts_commands_files_tools_and_phrases():
    messages = [
        msg("user", "run /plan then edit ~/.hermes/plugins/topic-context/dashboard/plugin_api.py"),
        msg("assistant", "Calling `session_search` next."),
        msg("tool", '{"ok": true}', tool_name="write_file"),
    ]
    topics = core.extract_topics(None, messages)
    labels = {t["label"] for t in topics}
    kinds = {t["label"]: t["kind"] for t in topics}

    assert "/plan" in labels
    assert "plugin_api.py" in labels
    assert "session_search" in labels
    assert "write_file" in labels

    assert kinds["/plan"] == core.KIND_COMMAND
    assert kinds["plugin_api.py"] == core.KIND_FILE
    assert kinds["write_file"] == core.KIND_TOOL


def test_tool_payloads_do_not_contribute_terms():
    """Tool output is machine noise; only the tool *name* should surface."""
    messages = [msg("tool", "unmistakablepayloadtoken " * 30, tool_name="terminal")]
    labels = {t["label"] for t in core.extract_topics(None, messages)}
    assert "terminal" in labels
    assert not any("unmistakablepayloadtoken" in label for label in labels)


def test_title_terms_outrank_body_terms():
    messages = [msg("assistant", "alpha alpha alpha alpha alpha")]
    topics = core.extract_topics("alpha", messages)
    scores = {t["label"]: t["score"] for t in topics}
    # 5 body hits (5.0) vs 1 title hit + 5 body hits with the title bonus.
    assert scores["alpha"] > 5.0


def test_strong_topics_suppress_redundant_bare_words():
    messages = [msg("user", "open plugin.js and check the plugin wiring for the plugin")]
    labels = {t["label"] for t in core.extract_topics(None, messages)}
    assert "plugin.js" in labels
    assert "plugin" not in labels, "bare word should fold into the stronger 'plugin.js'"


def test_long_paths_fall_back_to_basename():
    deep = "~/.hermes/plugins/topic-context/dashboard/topic_context_core.py"
    assert len(deep) > core.MAX_TOPIC_CHARS
    labels = {t["label"] for t in core.extract_topics(None, [msg("user", deep)])}
    assert "topic_context_core.py" in labels


def test_stopwords_and_numbers_are_not_topics():
    messages = [msg("user", "the and 2026 that with")]
    labels = {t["label"] for t in core.extract_topics(None, messages)}
    assert not labels & {"the", "and", "2026", "that", "with"}


def test_respects_limit():
    messages = [msg("user", " ".join(f"token{i}" for i in range(60)))]
    assert len(core.extract_topics(None, messages, limit=7)) <= 7


# ------------------------------------------------------------------ todos


TODO_PAYLOAD = json.dumps({
    "todos": [
        {"id": "1", "content": "Write the core module", "status": "completed"},
        {"id": "2", "content": "Wire the desktop pane", "status": "in_progress"},
        {"id": "3", "content": "Add the README", "status": "pending"},
    ],
    "revision": 4,
    "summary": {"total": 3},
})


def test_parse_todos_reads_newest_todo_result():
    messages = [
        msg("tool", TODO_PAYLOAD, tool_name="todo_list"),
        msg("tool", json.dumps({"todos": [{"id": "9", "content": "stale", "status": "pending"}]}),
            tool_name="todo_list"),
    ]
    buckets = core._parse_todos(messages)
    assert [t["content"] for t in buckets["pending"]] == ["stale"]


def test_parse_todos_ignores_malformed_payloads():
    assert core._parse_todos([msg("tool", "not json", tool_name="todo_list")]) is None
    assert core._parse_todos([msg("tool", TODO_PAYLOAD, tool_name="something_else")]) is None
    assert core._parse_todos([]) is None


# -------------------------------------------------------------------- brief


def test_brief_has_the_three_fixed_sections(session):
    messages = [msg("user", "build the pane"), msg("assistant", "Starting on the pane now.")]
    brief = core.render_brief(session, messages, core.extract_topics(session["title"], messages))

    assert "## Context" in brief
    assert "## Current state" in brief
    assert "## Direction" in brief
    assert "Wire the topic-context pane into plugin.js" in brief
    assert "2000-01-01" not in brief  # sanity: dates are real, not placeholders


def test_brief_prefers_open_todos_for_direction(session):
    messages = [
        msg("user", "keep going"),
        msg("tool", TODO_PAYLOAD, tool_name="todo_list"),
    ]
    brief = core.render_brief(session, messages, [])
    assert "Wire the desktop pane" in brief
    assert "Add the README" in brief
    assert "`[>]`" in brief


def test_brief_falls_back_to_last_request_without_todos(session):
    messages = [msg("user", "older ask"), msg("assistant", "ok"), msg("user", "newest ask")]
    brief = core.render_brief(session, messages, [])
    assert "newest ask" in brief
    assert "older ask" not in brief


def test_brief_reports_no_data_honestly():
    brief = core.render_brief({"id": "s"}, [], [])
    assert "## Context" in brief
    assert "Not enough activity recorded yet." in brief
    assert "No open task list" in brief


# ------------------------------------------------------------------- memory


MEMORY_DOC = """Server runs Tailscale and ufw.
§
The desktop pane registers through ctx.register with a placement hint.
§
Personal site lives in ~/.hermes/orkun-site/.
"""


def test_related_memory_matches_and_ranks_by_hit_count():
    entries = [
        {"source": "MEMORY.md", "text": chunk.strip(), "path": "MEMORY.md"}
        for chunk in MEMORY_DOC.split("\n§\n")
    ]
    hits = core.related_memory(["desktop", "pane"], limit=5, entries=entries)
    assert len(hits) == 1
    assert "ctx.register" in hits[0]["text"]
    assert set(hits[0]["matches"]) == {"desktop", "pane"}


def test_related_memory_ignores_short_terms():
    entries = [{"source": "MEMORY.md", "text": "the plugin pane", "path": "M"}]
    assert core.related_memory(["th", "e"], entries=entries) == []


def test_read_memory_entries_splits_on_section_sign(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "memories").mkdir(parents=True)
    (home / "memories" / "MEMORY.md").write_text("first entry\n§\nsecond entry\n", encoding="utf-8")
    (home / "memories" / "USER.md").write_text("user entry\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    entries = core.read_memory_entries()
    assert {e["source"] for e in entries} == {"MEMORY.md", "USER.md"}
    assert [e["text"] for e in entries if e["source"] == "MEMORY.md"] == ["first entry", "second entry"]


# ----------------------------------------------------------------- sessions


def test_related_sessions_dedupes_and_excludes_active():
    hits = [
        {"session_id": "s1", "snippet": "active", "session_started": 1},
        {"session_id": "s2", "title": "Older work", "snippet": "a>>>plugin<<<b", "session_started": 1},
        {"session_id": "s2", "title": "Older work", "snippet": "dup", "session_started": 1},
        {"session_id": "s3", "snippet": "another", "session_started": 1},
    ]
    db = StubDB(hits=hits)
    out = core.related_sessions(db, ["plugin"], exclude_session_id="s1", limit=5)

    assert [r["session_id"] for r in out] == ["s2", "s3"]
    assert out[0]["snippet"] == "a**plugin**b", "FTS markers become bold"
    assert db.queries and db.queries[0] == '"plugin"'


def test_related_sessions_survives_a_broken_query():
    class Boom(StubDB):
        def search_messages(self, query, **kwargs):
            raise ValueError("fts syntax error")

    assert core.related_sessions(Boom(), ["plugin"]) == []


def test_related_sessions_skips_short_terms():
    db = StubDB(hits=[{"session_id": "s2"}])
    assert core.related_sessions(db, ["ab"]) == []
    assert db.queries == []


# ---------------------------------------------------------------- aggregate


def test_build_context_assembles_every_section(session, monkeypatch):
    messages = [
        msg("user", "wire the pane into plugin.js"),
        msg("assistant", "Working on plugin.js now."),
        msg("tool", "{}", tool_name="write_file"),
    ]
    db = StubDB(
        session=session,
        messages=messages,
        hits=[{"session_id": "s9", "title": "Prior work", "snippet": "x", "session_started": 1}],
    )
    monkeypatch.setattr(core, "read_memory_entries", lambda: [
        {"source": "MEMORY.md", "text": "plugin.js lives under desktop-plugins", "path": "M"},
    ])

    payload = core.build_context(db, "s1")

    assert payload["session"]["id"] == "s1"
    assert payload["brief_source"] == "compiled"
    assert payload["cognee"] == {"enabled": False}
    assert any(t["label"] == "plugin.js" for t in payload["topics"])
    assert payload["memory"] and payload["memory"][0]["source"] == "MEMORY.md"
    assert payload["sessions"][0]["session_id"] == "s9"


def test_build_context_accepts_an_authored_brief(session):
    db = StubDB(session=session, messages=[msg("user", "hi")])
    payload = core.build_context(db, "s1", brief_text="## Context\nhand written\n")
    assert payload["brief_source"] == "authored"
    assert payload["brief"] == "## Context\nhand written\n"


def test_build_context_raises_for_unknown_session():
    with pytest.raises(KeyError):
        core.build_context(StubDB(session=None), "nope")


def test_json_serialisable(session, monkeypatch):
    monkeypatch.setattr(core, "read_memory_entries", lambda: [])
    payload = core.build_context(StubDB(session=session, messages=[msg("user", "hi")]), "s1")
    json.dumps(payload)  # must not raise — the API layer returns this as JSON
