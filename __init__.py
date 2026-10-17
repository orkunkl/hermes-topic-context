"""topic-context — a read-only desktop pane for the session's live context.

This package is the *backend* half of the plugin. Hermes mounts
``dashboard/plugin_api.py`` at ``/api/plugins/topic-context/`` while the plugin
is listed in ``plugins.enabled``; the UI half is ``desktop/plugin.js``, which
the desktop app loads as a disk plugin.

The plugin contributes no tools, no hooks and no prompt surface — it only
answers questions about data the agent already has on disk.
"""

from __future__ import annotations

__version__ = "0.1.0"


def register(ctx) -> None:  # noqa: ANN001 - the host passes its own context object
    """Register nothing: this plugin is backend routes plus a desktop pane.

    ``register`` is still required so the loader has an entry point to call; an
    empty body is the honest implementation rather than a stub with dead hooks.
    """
    return None
