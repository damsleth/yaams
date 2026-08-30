"""Unit tests for scripts/promotion_freeze.py pure helpers."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from promotion_freeze import _is_short_single_token, _strip_secrets  # noqa: E402


def test_strip_secrets_drops_secret_keys_recursively():
  cfg = {
    "llm": {"api_key": "sk-123", "model": "m"},
    "sources": [{"password": "x", "name": "imap"}],
    "auth_token": "t",
    "dictionary_path": "entities.json",
  }
  clean = _strip_secrets(cfg)
  assert clean == {
    "llm": {"model": "m"},
    "sources": [{"name": "imap"}],
    "dictionary_path": "entities.json",
  }


def test_short_single_token_filter():
  assert _is_short_single_token("SP")
  assert _is_short_single_token("NOCOS")
  assert _is_short_single_token("+4794324297")  # identifiers stay in: high recall
  assert not _is_short_single_token("X")  # too short
  assert not _is_short_single_token("Nina Cathrine")  # multi-token
  assert not _is_short_single_token("a" * 13)  # too long
