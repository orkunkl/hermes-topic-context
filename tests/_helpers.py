"""Shared test doubles.

Underscore-prefixed so pytest does not try to collect this file as a test
module; both test files import from it.
"""

from __future__ import annotations


class StubDB:
    """Minimal stand-in for ``hermes_state.SessionDB``'s read surface."""

    def __init__(self, session=None, messages=None, hits=None, sessions=None):
        self._session = session
        self._messages = messages or []
        self._hits = hits or []
        self._sessions = sessions if sessions is not None else ([session] if session else [])
        self.queries: list[str] = []

    def get_session(self, session_id):
        if self._session is None:
            return None
        return self._session

    def get_messages(self, session_id, **kwargs):
        return list(self._messages)

    def search_messages(self, query, **kwargs):
        self.queries.append(query)
        return list(self._hits)

    def list_sessions_rich(self, **kwargs):
        return list(self._sessions)


def msg(role, content="", tool_name=None):
    """Build one transcript row shaped like the real message dict."""
    return {"role": role, "content": content, "tool_name": tool_name}


def session(**overrides):
    """A plausible session row, overridable per test."""
    base = {
        "id": "s1",
        "title": "Wire the topic-context pane into plugin.js",
        "source": "desktop",
        "model": "test-model",
        "message_count": 12,
        "started_at": 1792227629.15,
        "last_activity_description": "reading references/desktop-plugins.md",
        "cwd": "/home/ubuntu/.hermes",
        "profile_name": "default",
    }
    base.update(overrides)
    return base
