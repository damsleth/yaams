from __future__ import annotations

import sys
from unittest.mock import patch

from yaams.enrich.embed import _confirm_download


def test_confirm_download_returns_false_in_non_tty():
  with patch.object(sys.stdin, "isatty", return_value=False):
    assert _confirm_download("BAAI/bge-m3") is False


def test_confirm_download_accepts_y():
  with patch.object(sys.stdin, "isatty", return_value=True), \
       patch("builtins.input", return_value="y"):
    assert _confirm_download("BAAI/bge-m3") is True


def test_confirm_download_accepts_yes():
  with patch.object(sys.stdin, "isatty", return_value=True), \
       patch("builtins.input", return_value="YES"):
    assert _confirm_download("BAAI/bge-m3") is True


def test_confirm_download_rejects_n():
  with patch.object(sys.stdin, "isatty", return_value=True), \
       patch("builtins.input", return_value="n"):
    assert _confirm_download("BAAI/bge-m3") is False


def test_confirm_download_rejects_empty():
  with patch.object(sys.stdin, "isatty", return_value=True), \
       patch("builtins.input", return_value=""):
    assert _confirm_download("BAAI/bge-m3") is False


def test_confirm_download_handles_eof():
  with patch.object(sys.stdin, "isatty", return_value=True), \
       patch("builtins.input", side_effect=EOFError):
    assert _confirm_download("BAAI/bge-m3") is False


def test_confirm_download_prompt_mentions_model_name():
  with patch.object(sys.stdin, "isatty", return_value=True), \
       patch("builtins.input", return_value="n") as mock_input:
    _confirm_download("some-model-name")
  prompt = mock_input.call_args.args[0]
  assert "some-model-name" in prompt
  assert "huggingface" in prompt.lower()
