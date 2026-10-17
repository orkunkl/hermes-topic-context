# topic-context

A read-only desktop pane for [Hermes Agent](https://hermes-agent.nousresearch.com) that answers one question while you work:

> **what is this session about, and what did I already know about it?**

A right-hand pane shows, for the session you are currently in:

- **Brief** — a three-section markdown brief: `Context`, `Current state`, `Direction`. Rendered with the app's own markdown pipeline, with a `</>` toggle to see the raw source.
- **Topics** — the terms the conversation actually keeps touching: slash commands, file paths, tool names, quoted phrases, code identifiers.
- **Memory** — the `MEMORY.md` / `USER.md` entries that match those topics, with the matching terms marked.
- **Related sessions** — earlier sessions that share the topic. Click one to open it.

## The point: zero model calls

Every section is **assembled from data already on disk** — the Hermes session store and the agent's memory files. Nothing is summarised by a model, nothing is written, nothing leaves your machine. Refreshing the pane costs one read-only SQLite query, so it can stay live while you talk.

| | |
|---|---|
| LLM calls | none |
| Writes | none |
| Network | none |
| Refresh cost | one read-only query, ~10–40 ms on a 16 k-message store |

That property is what makes the pane safe to leave enabled: it cannot change your session, break prompt caching, or spend your credits.

## How the brief is built without a model

| Section | Source |
|---|---|
| `## Context` | session title, source, start day, top five mined topics |
| `## Current state` | last activity description, the three most recent tools, the newest assistant message, session length |
| `## Direction` | `pending` / `in_progress` items from the newest `todo_list` result in the transcript; falls back to your last request |

The todo list is in-memory on the agent, but every `todo_list` call writes the full list into the transcript — so the last one *is* the session's live plan, and reading it costs nothing.

The core is a pure function over rows. If you later want a model-written brief, the shape is already there: `build_context(..., brief_text=...)` takes an authored brief and reports `brief_source: "authored"` instead of `"compiled"`, and the pane labels which one it is showing.

## Install

```bash
hermes plugins install orkunkl/hermes-topic-context --enable
```

Or from a checkout:

```bash
git clone https://github.com/orkunkl/hermes-topic-context
ln -s "$PWD/hermes-topic-context" ~/.hermes/plugins/topic-context
hermes plugins enable topic-context
```

Then:

1. Restart the surface that serves the backend (the desktop app's `serve` process, or `hermes dashboard`). Plugin Python is imported at startup.
2. In the desktop app, open **Capabilities → Plugins** and enable **Topic Context** (the desktop half is gated separately — the app never imports Python for a plugin you have not enabled).
3. The pane appears on the right. Drag it wherever you like.

Closing the pane disables the plugin; **Reset layout** brings it back.

### Requirements

- Hermes Agent ≥ 0.21 (desktop plugin SDK + dashboard plugin backend mount).
- Desktop app. There is no dashboard tab by design — `tab.hidden` is set.

## HTTP API

Mounted read-only at `/api/plugins/topic-context/` (profile-scoped by the host):

| Route | Returns |
|---|---|
| `GET /health` | liveness, hermes home, session count, memory entry count |
| `GET /context?session_id=&topic_limit=&memory_limit=&session_limit=` | the whole pane payload; `session_id` defaults to the newest session |
| `GET /brief?session_id=` | just the markdown brief and its source |
| `GET /topics?session_id=&limit=` | mined topics |
| `GET /memory?session_id=&q=` | matching memory entries; `q` overrides the topic-derived terms |
| `GET /sessions?session_id=&q=&limit=` | related past sessions |

There is no write route — a test asserts every route is a `GET`.

## Layout

```
topic-context/                  # one installable folder, both halves
├── plugin.yaml                 # agent-half manifest
├── __init__.py                 # register(ctx) — no tools, no hooks
├── dashboard/
│   ├── manifest.json           # { "name": ..., "api": "plugin_api.py", "tab": { "hidden": true } }
│   ├── plugin_api.py           # FastAPI routes → /api/plugins/topic-context/
│   └── topic_context_core.py   # the pure aggregation (topics, brief, matching)
├── desktop/
│   └── plugin.js               # the pane (uncompiled ESM, jsx() calls)
└── tests/
```

## Development

Tests run against a stub session store, so they need neither a live Hermes install nor a real `state.db`:

```bash
uv run --no-project \
  --with pytest==9.1.1 --with fastapi --with httpx --with pyyaml \
  python -m pytest tests/ -q
```

`Hermes` itself is not importable from the plugin's own test environment, so `tests/test_api.py` stubs the two host modules the API layer touches (`hermes_constants`, `hermes_state`). That keeps the route tests hermetic — they pin the HTTP contract and the read-only guarantee, not Hermes internals.

Validate the package against the real loader:

```bash
hermes plugins doctor .
```

While developing the desktop half, note that the app loads the **copy** in `~/.hermes/desktop-plugins/<id>/`, not your checkout. A symlinked package is watched and re-synced on save; otherwise run **Rescan** in **Capabilities → Plugins**.

## Design notes

- **Read-only by construction.** No write route exists, and `tests/test_api.py::test_db_is_always_opened_read_only` fails the build if one is added.
- **Deterministic topics.** A bare word never outranks a file path, and a strong topic suppresses the redundant bare words inside it, so the chip row stays tight.
- **Honest empty states.** A session with no todos says so instead of inventing a direction.
- **No Python pollution.** `plugin_api.py` loads its sibling core by path rather than appending to a long-lived server's `sys.path`.

## License

MIT — see [LICENSE](LICENSE).
