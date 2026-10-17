"""topic-context backend, mounted at ``/api/plugins/topic-context/``.

Everything here is read-only: the routes open the Hermes state store in
read-only mode, aggregate what is already on disk, and return it. There is no
write path, no LLM call and no outbound network request — which is what makes
the desktop pane free to refresh on every turn.

The state store is opened per request (``SessionDB(read_only=True)`` resolves the
calling profile's home itself), so one process serving several profiles answers
each request from the right one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from hermes_constants import get_hermes_home

__version__ = "0.1.0"

router = APIRouter()


def _load_core() -> Any:
    """Import the sibling core module without touching the process ``sys.path``.

    The host loads this file by path under a synthetic module name, so the
    plugin's own ``dashboard/`` directory is not an import root. Loading the
    sibling explicitly keeps this plugin from appending to a long-lived
    server's ``sys.path``.
    """
    cached = sys.modules.get("topic_context_core")
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent / "topic_context_core.py"
    spec = importlib.util.spec_from_file_location("topic_context_core", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load topic-context core from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["topic_context_core"] = module
    spec.loader.exec_module(module)
    return module


core = _load_core()


def _open_db() -> Any:
    """Open the profile's session store read-only."""
    from hermes_state import SessionDB

    return SessionDB(read_only=True)


def _resolve_session_id(db: Any, session_id: Optional[str]) -> str:
    """Use the given session, or fall back to the most recently active one."""
    if session_id:
        return session_id
    rows = db.list_sessions_rich(limit=1)
    if not rows:
        raise HTTPException(status_code=404, detail="no sessions recorded yet")
    return rows[0]["id"]


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus the two facts that explain an empty pane."""
    payload: dict[str, Any] = {
        "ok": True,
        "plugin": "topic-context",
        "version": __version__,
        "hermes_home": str(get_hermes_home()),
    }
    try:
        db = _open_db()
        payload["session_count"] = len(db.list_sessions_rich(limit=1000))
        payload["memory_entries"] = len(core.read_memory_entries())
    except Exception as exc:  # plugin contract: report, never raise out of the handler
        payload["ok"] = False
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload


@router.get("/context")
async def context(
    session_id: Optional[str] = Query(None, description="Session to describe; defaults to the newest."),
    topic_limit: int = Query(core.DEFAULT_TOPIC_LIMIT, ge=1, le=40),
    memory_limit: int = Query(core.DEFAULT_MEMORY_LIMIT, ge=0, le=20),
    session_limit: int = Query(core.DEFAULT_SESSION_LIMIT, ge=0, le=20),
) -> dict[str, Any]:
    """The whole pane payload in one call: brief, topics, memory, related sessions."""
    db = _open_db()
    resolved = _resolve_session_id(db, session_id)
    try:
        payload = core.build_context(
            db,
            resolved,
            topic_limit=topic_limit,
            memory_limit=memory_limit,
            session_limit=session_limit,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown session: {resolved}") from None
    payload["version"] = __version__
    return payload


@router.get("/brief")
async def brief(
    session_id: Optional[str] = Query(None),
    refresh: bool = Query(False, description="Reserved for the agent-authored brief path."),
) -> dict[str, Any]:
    """Just the markdown brief, for a cheap re-render without refetching the pane."""
    db = _open_db()
    resolved = _resolve_session_id(db, session_id)
    try:
        payload = core.build_context(db, resolved, memory_limit=0, session_limit=0)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown session: {resolved}") from None
    return {
        "session_id": resolved,
        "brief": payload["brief"],
        "source": payload["brief_source"],
        "refresh_supported": False,  # no LLM path in this plugin, by design
        "requested_refresh": refresh,
    }


@router.get("/topics")
async def topics(
    session_id: Optional[str] = Query(None),
    limit: int = Query(core.DEFAULT_TOPIC_LIMIT, ge=1, le=40),
) -> dict[str, Any]:
    """Mined topics for one session."""
    db = _open_db()
    resolved = _resolve_session_id(db, session_id)
    session = db.get_session(resolved)
    if not session:
        raise HTTPException(status_code=404, detail=f"unknown session: {resolved}")
    messages = db.get_messages(resolved)
    return {
        "session_id": resolved,
        "title": session.get("title") or session.get("display_name"),
        "topics": core.extract_topics(session.get("title"), messages, limit=limit),
    }


@router.get("/memory")
async def memory(
    session_id: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Explicit terms; defaults to the session's topics."),
    limit: int = Query(core.DEFAULT_MEMORY_LIMIT, ge=1, le=20),
) -> dict[str, Any]:
    """Memory entries matching either the given terms or the session's own topics."""
    terms = [t.strip() for t in (q or "").split(",") if t.strip()]
    resolved: Optional[str] = session_id
    if not terms:
        db = _open_db()
        resolved = _resolve_session_id(db, session_id)
        session = db.get_session(resolved)
        if not session:
            raise HTTPException(status_code=404, detail=f"unknown session: {resolved}")
        topics = core.extract_topics(session.get("title"), db.get_messages(resolved))
        terms = [t["label"] for t in topics]
    return {
        "session_id": resolved,
        "terms": terms,
        "entries": core.related_memory(terms, limit=limit),
    }


@router.get("/sessions")
async def sessions(
    session_id: Optional[str] = Query(None, description="Session to exclude and mine terms from."),
    q: Optional[str] = Query(None, description="Explicit search terms; defaults to the session's topics."),
    limit: int = Query(core.DEFAULT_SESSION_LIMIT, ge=1, le=20),
) -> dict[str, Any]:
    """Past sessions that share the active session's topic."""
    terms = [t.strip() for t in (q or "").split(",") if t.strip()]
    db = _open_db()
    resolved: Optional[str] = None
    if not terms or session_id is not None:
        resolved = _resolve_session_id(db, session_id)
        session = db.get_session(resolved)
        if not session:
            raise HTTPException(status_code=404, detail=f"unknown session: {resolved}")
        if not terms:
            topics = core.extract_topics(session.get("title"), db.get_messages(resolved))
            terms = [t["label"] for t in topics]
    return {
        "session_id": resolved,
        "terms": terms,
        "sessions": core.related_sessions(
            db, terms, exclude_session_id=resolved, limit=limit
        ),
    }
