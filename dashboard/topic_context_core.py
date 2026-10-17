"""topic-context core: deterministic topic extraction and session brief compilation.

Everything here is a *pure* transformation of data already on disk — session rows
and messages from the Hermes state store, plus the agent's own memory markdown
files. No LLM calls, no network, no writes, so refreshing the pane is free.

The module deliberately takes a plain ``db`` object (anything exposing the
``hermes_state.SessionDB`` read API) instead of importing Hermes, which keeps it
unit-testable against a stub and keeps the plugin's Python half import-light.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "Topic",
    "extract_topics",
    "render_brief",
    "read_memory_entries",
    "related_memory",
    "related_sessions",
    "build_context",
    "hermes_home",
]

# --------------------------------------------------------------------- limits

DEFAULT_TOPIC_LIMIT = 12
DEFAULT_MEMORY_LIMIT = 6
DEFAULT_SESSION_LIMIT = 6
RECENT_MESSAGE_WINDOW = 40          # messages scanned for topic terms
MAX_TOPIC_CHARS = 48                # longer labels are dropped or shortened
SNIPPET_CHARS = 220
NOTE_CHARS = 300
REQUEST_CHARS = 300

# Kind of a topic — drives the colour dot in the pane.
KIND_COMMAND = "command"
KIND_FILE = "file"
KIND_TOOL = "tool"
KIND_PHRASE = "phrase"
KIND_IDENT = "ident"
KIND_TERM = "term"

_STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those is are was were be been being
    to of in on for with at by from as it its i you he she we they them his her their our
    your my me us do does did doing done have has had having will would can could should
    may might must not no yes so what which who whom whose when where why how all any both
    each few more most other some such only own same too very just also about into over
    after before under again further once here there out up down off above below between
    during through because while let lets get got make made use used using need needs
    want wants like also new now one two three first last next well okay ok sure fine
    """.split()
)

# ------------------------------------------------------------------ regexen

_CMD_RE = re.compile(r"(?:^|[\s(\[])/([a-z][a-z0-9_-]{1,30})\b")
_FILE_RE = re.compile(
    r"[\w~./-]*[\w-]\.(?:py|js|mjs|cjs|ts|tsx|jsx|md|ya?ml|json|toml|ini|cfg|conf"
    r"|sh|bash|zsh|html|css|sql|txt|db|env|lock|log|rs|go|rb|java|kt|c|cc|cpp|h)\b"
)
_SNAKE_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9]*_[a-zA-Z0-9_]{2,}\b")
_CAMEL_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+){1,}\b")
_TICK_RE = re.compile(r"`([^`\n]{4,60})`")
_WORD_RE = re.compile(r"[^\W\d_][\w-]{2,}", re.UNICODE)

# Score weights. Commands and file paths are the strongest signal a session is
# "about" something concrete; bare words are the weakest and need repetition.
_W = {
    KIND_COMMAND: 3.0,
    KIND_FILE: 3.0,
    KIND_TOOL: 2.6,
    KIND_PHRASE: 2.2,
    KIND_IDENT: 1.8,
    KIND_TERM: 1.0,
}
_TITLE_BONUS = 1.8


# -------------------------------------------------------------------- paths


def hermes_home() -> Path:
    """Resolve the active profile home (``$HERMES_HOME``, else ``~/.hermes``)."""
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


def memory_files() -> list[tuple[str, Path]]:
    """The two built-in memory documents, labelled for the UI."""
    home = hermes_home()
    return [("MEMORY.md", home / "memories" / "MEMORY.md"),
            ("USER.md", home / "memories" / "USER.md")]


# ------------------------------------------------------------------ helpers


def _as_text(content: Any) -> str:
    """Flatten a message content value (``str`` or a list of blocks) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                for key in ("text", "content", "input_text", "output_text"):
                    value = block.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
        return "\n".join(parts)
    if isinstance(content, dict):
        return _as_text(content.get("text") or content.get("content") or "")
    return str(content)


def _iso_day(epoch: Any) -> str:
    """Format a unix timestamp as a UTC ``YYYY-MM-DD`` day, or ``""``."""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _truncate(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _is_noise(label: str) -> bool:
    if not label or len(label) > MAX_TOPIC_CHARS:
        return True
    stripped = label.strip("`'\"()[]{},.;:")
    if len(stripped) < 3 or stripped.isdigit():
        return True
    return stripped.lower() in _STOPWORDS


def clean_snippet(snippet: str, limit: int = SNIPPET_CHARS) -> str:
    """Turn an FTS snippet's ``>>>term<<<`` markers into ``**term**`` and flatten it."""
    if not snippet:
        return ""
    text = snippet.replace(">>>", "**").replace("<<<", "**")
    return _truncate(text, limit)


# ------------------------------------------------------------- topic mining


class Topic(dict):
    """A mined topic: ``{label, kind, count, score}``.

    A ``dict`` subclass so it serialises straight to JSON for the API layer
    while still reading as an object in Python.
    """

    __slots__ = ()


def _bump(store: dict[str, dict], label: str, kind: str, weight: float) -> None:
    """Record one observation of *label* with a strength *weight*.

    Keeps the strongest kind seen for a label (a path that also shows up as a
    bare word stays a path).
    """
    label = (label or "").strip().strip("`'\"")
    if _is_noise(label):
        return
    key = label.lower()
    slot = store.get(key)
    if slot is None:
        store[key] = {"label": label, "kind": kind, "count": 1,
                      "score": weight, "_kind_weight": weight}
        return
    slot["count"] += 1
    slot["score"] += weight
    if weight > slot["_kind_weight"]:
        slot["_kind_weight"] = weight
        slot["kind"] = kind


def extract_topics(
    title: Optional[str],
    messages: Sequence[dict[str, Any]],
    *,
    limit: int = DEFAULT_TOPIC_LIMIT,
    window: int = RECENT_MESSAGE_WINDOW,
) -> list[dict[str, Any]]:
    """Mine what a session is *about* from its title and recent messages.

    Deterministic and cheap: slash commands, file paths, tool names, quoted
    phrases, code identifiers and (weakly) repeated words. No model involved.
    """
    store: dict[str, dict] = {}

    for match in _CMD_RE.findall(title or ""):
        _bump(store, "/" + match, KIND_COMMAND, _W[KIND_COMMAND] + _TITLE_BONUS)

    for word in _WORD_RE.findall(title or ""):
        _bump(store, word, KIND_TERM, _W[KIND_TERM] + _TITLE_BONUS)

    recent = list(messages)[-window:] if window > 0 else list(messages)
    for message in recent:
        role = message.get("role")
        tool_name = message.get("tool_name")
        if tool_name and role == "tool":
            _bump(store, str(tool_name), KIND_TOOL, _W[KIND_TOOL])

        text = _as_text(message.get("content"))
        if not text or role == "tool":
            # Tool payloads are machine output (file dumps, JSON) — they swamp the
            # signal, so only the tool *name* counts.
            continue

        for match in _CMD_RE.findall(text):
            _bump(store, "/" + match, KIND_COMMAND, _W[KIND_COMMAND])
        for match in _FILE_RE.findall(text):
            label = match if len(match) <= MAX_TOPIC_CHARS else Path(match).name
            _bump(store, label, KIND_FILE, _W[KIND_FILE])
        for match in _TICK_RE.findall(text):
            _bump(store, match, KIND_PHRASE, _W[KIND_PHRASE])
        for match in _SNAKE_RE.findall(text):
            _bump(store, match, KIND_IDENT, _W[KIND_IDENT])
        for match in _CAMEL_RE.findall(text):
            _bump(store, match, KIND_IDENT, _W[KIND_IDENT])
        for word in _WORD_RE.findall(text):
            _bump(store, word, KIND_TERM, _W[KIND_TERM])

    ranked = sorted(
        store.values(),
        key=lambda slot: (-slot["score"], -slot["count"], slot["label"].lower()),
    )

    # Drop a bare word that a stronger topic already contains (e.g. "plugin"
    # when "plugin.js" is present) so the chip row stays tight.
    strong = {slot["label"].lower() for slot in ranked[: limit * 2]
              if slot["kind"] in (KIND_FILE, KIND_PHRASE, KIND_IDENT, KIND_TOOL)}
    result: list[dict[str, Any]] = []
    for slot in ranked:
        label = slot["label"]
        if slot["kind"] == KIND_TERM and any(label.lower() in s for s in strong):
            continue
        slot.pop("_kind_weight", None)
        slot["score"] = round(slot["score"], 2)
        result.append(dict(slot))
        if len(result) >= limit:
            break
    return result


# ------------------------------------------------------------------- todos


def _parse_todos(messages: Sequence[dict[str, Any]]) -> Optional[dict[str, list[dict[str, str]]]]:
    """Read the newest ``todo_list`` tool result out of the transcript.

    The todo store itself is in-memory on the agent, but every call's full list
    is written to the transcript, so the last one is the session's live plan.
    """
    import json

    for message in reversed(list(messages)):
        if message.get("role") != "tool" or message.get("tool_name") != "todo_list":
            continue
        raw = _as_text(message.get("content"))
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        todos = payload.get("todos") if isinstance(payload, dict) else None
        if not isinstance(todos, list):
            continue
        buckets: dict[str, list[dict[str, str]]] = {
            "pending": [], "in_progress": [], "completed": [], "cancelled": [],
        }
        for item in todos:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "pending")
            buckets.setdefault(status, []).append({
                "id": str(item.get("id") or ""),
                "content": _truncate(str(item.get("content") or ""), 140),
                "status": status,
            })
        return buckets
    return None


# ------------------------------------------------------------------- brief


def render_brief(
    session: dict[str, Any],
    messages: Sequence[dict[str, Any]],
    topics: Sequence[dict[str, Any]],
) -> str:
    """Compile a three-section markdown brief straight from session data.

    This is the zero-call path: nothing here is summarised by a model, it is
    *assembled* from rows that already exist. Sections are fixed so the pane can
    style them, and so an agent-written brief can adopt the same shape.
    """
    title = (session.get("title") or session.get("display_name")
             or session.get("preview") or "(untitled session)")
    source = session.get("source") or "unknown"
    started = _iso_day(session.get("started_at"))

    lines: list[str] = ["## Context"]
    origin = f"`{source}` session"
    if started:
        origin += f" started {started}"
    lines.append(f"**{_truncate(str(title), 120)}** — {origin}.")

    if topics:
        chips = ", ".join(f"`{t['label']}`" for t in topics[:5])
        lines.append(f"Key topics: {chips}.")
    else:
        lines.append("No distinctive topics detected yet.")

    # --- current state
    lines.append("")
    lines.append("## Current state")
    facts: list[str] = []

    activity = session.get("last_activity_description") or ""
    if activity:
        facts.append(f"Last activity: {_truncate(str(activity), 160)}")

    tools: list[str] = []
    for message in reversed(list(messages)):
        name = message.get("tool_name")
        if message.get("role") == "tool" and name and name not in tools:
            tools.append(str(name))
        if len(tools) >= 3:
            break
    if tools:
        facts.append("Recent tools: " + ", ".join(f"`{t}`" for t in tools))

    last_note = ""
    for message in reversed(list(messages)):
        if message.get("role") == "assistant":
            text = _as_text(message.get("content")).strip()
            if text:
                last_note = text
                break
    if last_note:
        facts.append(f"Latest note: {_truncate(last_note, NOTE_CHARS)}")

    counts = session.get("message_count")
    if counts:
        facts.append(f"Session length: {counts} messages")

    lines.extend(f"- {fact}" for fact in facts)
    if not facts:
        lines.append("- Not enough activity recorded yet.")

    # --- direction
    lines.append("")
    lines.append("## Direction")
    todos = _parse_todos(messages)
    active = (todos or {}).get("in_progress", [])
    pending = (todos or {}).get("pending", [])
    if active or pending:
        for item in active:
            lines.append(f"- `[>]` {item['content']}")
        for item in pending:
            lines.append(f"- `[ ]` {item['content']}")
    else:
        last_request = ""
        for message in reversed(list(messages)):
            if message.get("role") == "user":
                text = _as_text(message.get("content")).strip()
                if text and not text.startswith("["):
                    last_request = text
                    break
        if last_request:
            lines.append(f"_No open task list._ Last request: {_truncate(last_request, REQUEST_CHARS)}")
        else:
            lines.append("_No open task list and no recent request._")

    return "\n".join(lines).strip() + "\n"


# ------------------------------------------------------------------ memory


def read_memory_entries() -> list[dict[str, str]]:
    """Parse MEMORY.md / USER.md into individual entries.

    Entries are separated by a lone ``§`` line (the same delimiter the memory
    tool writes), so this mirrors exactly what the agent carries into a session.
    """
    entries: list[dict[str, str]] = []
    for label, path in memory_files():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for chunk in raw.split("\n§\n") if "\n§\n" in raw else raw.split("§"):
            text = chunk.strip()
            if text:
                entries.append({"source": label, "text": text, "path": str(path)})
    return entries


def related_memory(
    terms: Iterable[str],
    *,
    limit: int = DEFAULT_MEMORY_LIMIT,
    entries: Optional[Sequence[dict[str, str]]] = None,
) -> list[dict[str, Any]]:
    """Match memory entries against topic terms (case-insensitive substring).

    Pure string work over a handful of kilobytes — the reason the memory section
    costs nothing.
    """
    needles = [t.lower() for t in terms if t and len(t) >= 3]
    if not needles:
        return []
    pool = entries if entries is not None else read_memory_entries()

    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in pool:
        haystack = entry["text"].lower()
        hits = sorted({needle for needle in needles if needle in haystack})
        if hits:
            scored.append((len(hits), {
                "source": entry["source"],
                "text": _truncate(entry["text"], 400),
                "matches": hits[:6],
            }))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored[:limit]]


# ---------------------------------------------------------------- sessions


def related_sessions(
    db: Any,
    terms: Iterable[str],
    *,
    exclude_session_id: Optional[str] = None,
    limit: int = DEFAULT_SESSION_LIMIT,
) -> list[dict[str, Any]]:
    """Full-text search the transcript for sessions that share the active topic.

    One FTS query per refresh, no LLM. Results are grouped by session and the
    best (first, i.e. highest-ranked) hit represents each one.
    """
    query_terms = [t for t in terms if t and len(t) >= 3][:8]
    if not query_terms:
        return []
    query = " OR ".join(f'"{t}"' for t in query_terms)

    try:
        hits = db.search_messages(query, limit=max(limit * 4, 24), sort="newest")
    except Exception:
        # A malformed FTS query must never take the pane down — the caller
        # renders an empty section instead.
        return []

    seen: dict[str, dict[str, Any]] = {}
    for hit in hits:
        sid = hit.get("session_id")
        if not sid or sid == exclude_session_id or sid in seen:
            continue
        seen[sid] = {
            "session_id": sid,
            "title": hit.get("title") or hit.get("session_title") or "(untitled session)",
            "source": hit.get("source"),
            "day": _iso_day(hit.get("session_started") or hit.get("timestamp")),
            "timestamp": hit.get("timestamp"),
            "snippet": clean_snippet(str(hit.get("snippet") or "")),
            "role": hit.get("role"),
        }
        if len(seen) >= limit:
            break
    return list(seen.values())


# ---------------------------------------------------------------- aggregate


def build_context(
    db: Any,
    session_id: str,
    *,
    topic_limit: int = DEFAULT_TOPIC_LIMIT,
    memory_limit: int = DEFAULT_MEMORY_LIMIT,
    session_limit: int = DEFAULT_SESSION_LIMIT,
    brief_text: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the whole pane payload for one session.

    ``brief_text`` lets a caller substitute a richer (agent-authored) brief;
    when it is ``None`` the zero-call compiled brief is used.
    """
    session = db.get_session(session_id)
    if not session:
        raise KeyError(f"unknown session: {session_id}")

    messages = db.get_messages(session_id)
    topics = extract_topics(session.get("title"), messages, limit=topic_limit)
    terms = [t["label"] for t in topics]

    brief = brief_text if brief_text is not None else render_brief(session, messages, topics)

    return {
        "session": {
            "id": session_id,
            "title": session.get("title") or session.get("display_name"),
            "source": session.get("source"),
            "model": session.get("model"),
            "profile": session.get("profile_name"),
            "cwd": session.get("cwd"),
            "message_count": session.get("message_count"),
            "started_at": session.get("started_at"),
            "day": _iso_day(session.get("started_at")),
        },
        "brief": brief,
        "brief_source": "compiled" if brief_text is None else "authored",
        "topics": topics,
        "memory": related_memory(terms, limit=memory_limit),
        "sessions": related_sessions(
            db, terms, exclude_session_id=session_id, limit=session_limit
        ),
        "cognee": {"enabled": False},
    }
