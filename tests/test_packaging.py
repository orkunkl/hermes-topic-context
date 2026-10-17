"""Packaging contracts.

A plugin is only installable if its manifest, its dashboard manifest and its
code agree. These tests fail on the drift that would otherwise surface as a
confusing runtime error for whoever installs the plugin.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))


def _yaml(path: Path) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_plugin_id_is_consistent_across_manifests():
    plugin = _yaml(ROOT / "plugin.yaml")
    dashboard = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))

    assert plugin["name"] == "topic-context"
    assert dashboard["name"] == plugin["name"] == ROOT.name, "folder name must equal the plugin id"
    assert plugin["version"] == dashboard["version"]


def test_version_matches_every_python_declaration():
    plugin_version = _yaml(ROOT / "plugin.yaml")["version"]
    sources = {
        "__init__.py": ROOT / "__init__.py",
        "dashboard/plugin_api.py": ROOT / "dashboard" / "plugin_api.py",
    }
    for label, path in sources.items():
        match = re.search(r'^__version__ = "([^"]+)"', path.read_text(encoding="utf-8"), re.M)
        assert match, f"{label} declares no __version__"
        assert match.group(1) == plugin_version, f"{label} disagrees with plugin.yaml"


def test_manifest_declares_the_backend_and_hides_the_tab():
    dashboard = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert dashboard["api"] == "plugin_api.py"
    assert (ROOT / "dashboard" / "plugin_api.py").is_file()
    # The plugin is desktop-only: it must not add a dashboard tab.
    assert dashboard.get("tab", {}).get("hidden") is True


def test_plugin_declares_no_tools_or_hooks():
    plugin = _yaml(ROOT / "plugin.yaml")
    assert not plugin.get("provides_tools")
    assert not plugin.get("provides_hooks")
    assert not plugin.get("requires_env"), "the plugin must need no credentials"


def test_readme_and_license_exist():
    assert (ROOT / "README.md").is_file()
    assert (ROOT / "LICENSE").is_file()


def test_desktop_half_ships_beside_the_backend():
    """One folder must carry both halves so the plugin installs as a unit."""
    assert (ROOT / "desktop" / "plugin.js").is_file()
    plugin_js = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "topic-context" in plugin_js, "the desktop half must export the plugin id"
    assert "jsx(" in plugin_js, "the desktop half is loaded uncompiled: jsx() calls, not JSX syntax"
