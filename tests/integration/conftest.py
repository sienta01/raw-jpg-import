"""Shared fixtures for the integration tests: fake Zf cards and a destination root.

Card detection is blocked for every test here, so nothing can ever reach a real card that
happens to be in the reader; all cards are fake folders under ``tmp_path``.
"""

from __future__ import annotations

import pytest
from fakes.card import ZfCards, no_real_cards, zf_cards


@pytest.fixture(autouse=True)
def _no_real_cards(monkeypatch):
    no_real_cards(monkeypatch)


@pytest.fixture
def zf(tmp_path) -> ZfCards:
    """The sunset walk on two fake cards (raw_jpg mode)."""
    return zf_cards(tmp_path / "cards")


@pytest.fixture
def dest(tmp_path):
    """An empty library root whose path has spaces and non-ASCII characters."""
    d = tmp_path / "Fotos Bibliothek ü"
    d.mkdir()
    return d
