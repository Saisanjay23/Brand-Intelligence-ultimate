"""The CLIP logo-embedding tier is a switch, and it is OFF by default.

Off means logo matching runs on the perceptual-hash tier alone: nothing heavy
is imported (no torch, no ~600 MB weights), and every caller already degrades
to the hash tier when `available()` is False. Off must also not be REMEMBERED
as a failure, or turning the setting back on would do nothing until a restart.
"""

from __future__ import annotations

import sys

import pytest

from backend.config.settings import Settings
from backend.shared import imageembedding as ie


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(ie, "_model", None)
    monkeypatch.setattr(ie, "_processor", None)
    monkeypatch.setattr(ie, "_load_failed", False)


class TestDefault:
    def test_the_tier_is_off_unless_asked_for(self):
        assert Settings(_env_file=None).logo_embeddings_enabled is False

    def test_it_can_be_turned_on(self):
        assert Settings(_env_file=None, logo_embeddings_enabled=True).logo_embeddings_enabled is True


class TestOff:
    def test_nothing_is_available_and_nothing_embeds(self, monkeypatch):
        monkeypatch.setattr(ie.settings, "logo_embeddings_enabled", False)
        assert ie.available() is False
        assert ie.embed(b"not even an image") is None

    def test_nothing_heavy_is_imported(self, monkeypatch):
        monkeypatch.setattr(ie.settings, "logo_embeddings_enabled", False)
        for mod in ("torch", "transformers"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        ie.available()
        ie.embed(b"x")
        assert "torch" not in sys.modules and "transformers" not in sys.modules

    def test_off_is_not_remembered_as_a_failure(self, monkeypatch):
        monkeypatch.setattr(ie.settings, "logo_embeddings_enabled", False)
        ie.available()
        assert ie._load_failed is False

    def test_stored_vectors_still_compare(self):
        a = [1.0, 0.0]
        assert ie.similarity(a, [1.0, 0.0]) == 1.0
        assert ie.is_match(ie.similarity(a, [1.0, 0.0])) is True
        assert ie.is_match(ie.similarity(a, [0.0, 1.0])) is False


class TestOn:
    def test_without_the_libraries_it_degrades_instead_of_failing(self, monkeypatch):
        monkeypatch.setattr(ie.settings, "logo_embeddings_enabled", True)
        monkeypatch.setitem(sys.modules, "torch", None)       # makes `import torch` raise
        assert ie.available() is False
        assert ie._load_failed is True                         # genuinely missing: remembered
        assert ie.embed(b"x") is None

    def test_switching_back_on_takes_effect_without_a_restart(self, monkeypatch):
        monkeypatch.setattr(ie.settings, "logo_embeddings_enabled", False)
        assert ie.available() is False
        monkeypatch.setattr(ie.settings, "logo_embeddings_enabled", True)
        monkeypatch.setitem(sys.modules, "torch", None)
        ie.available()
        assert ie._load_failed is True                         # it tried: the switch was read live
