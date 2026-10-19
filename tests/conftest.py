"""Shared fixtures. Every test runs with an isolated RAW_JPG_IMPORT_HOME."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Keep config, settings, ledger and logs away from the real user folders."""
    home = tmp_path_factory.mktemp("rji-home")
    monkeypatch.setenv("RAW_JPG_IMPORT_HOME", str(home))
    return home


@pytest.fixture
def fmt():
    """The built-in format table (no user config)."""
    from raw_jpg_import.config import load_config

    return load_config(use_user=False).formats
