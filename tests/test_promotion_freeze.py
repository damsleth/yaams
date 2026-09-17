"""Unit tests for scripts/promotion_freeze.py pure helpers."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from promotion_freeze import _is_short_single_token, _sha256_file, _strip_secrets  # noqa: E402


@pytest.mark.parametrize("content", [b"", b"fixture", b"\x00\xff" * (1 << 20)])
def test_file_hash_matches_sha256(tmp_path, content):
  path = tmp_path / "fixture.db"
  path.write_bytes(content)
  assert _sha256_file(path) == hashlib.sha256(content).hexdigest()


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
