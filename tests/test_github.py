from __future__ import annotations

import io
import urllib.error
from unittest.mock import patch

import pytest

from yaams.ingest.github import GitHubAdapter, GitHubAPIError


def _http_error(code: int, body: bytes = b""):
  return urllib.error.HTTPError(
    url="https://api.github.com/repos/x/y",
    code=code,
    msg="boom",
    hdrs=None,  # type: ignore[arg-type]
    fp=io.BytesIO(body),
  )


def test_get_raises_on_auth_failure():
  adapter = GitHubAdapter(username="me")
  adapter._token = "fake"
  with patch("urllib.request.urlopen", side_effect=_http_error(401, b"Bad credentials")):
    with pytest.raises(GitHubAPIError) as exc_info:
      adapter._get("https://api.github.com/user/repos")
  assert exc_info.value.status == 401


def test_get_raises_on_rate_limit():
  adapter = GitHubAdapter(username="me")
  adapter._token = "fake"
  with patch("urllib.request.urlopen", side_effect=_http_error(429)):
    with pytest.raises(GitHubAPIError) as exc_info:
      adapter._get("https://api.github.com/user/repos")
  assert exc_info.value.status == 429


def test_get_raises_on_server_error():
  adapter = GitHubAdapter(username="me")
  adapter._token = "fake"
  with patch("urllib.request.urlopen", side_effect=_http_error(503)):
    with pytest.raises(GitHubAPIError):
      adapter._get("https://api.github.com/user/repos")


def test_get_returns_empty_on_404():
  adapter = GitHubAdapter(username="me")
  adapter._token = "fake"
  with patch("urllib.request.urlopen", side_effect=_http_error(404)):
    data, next_url = adapter._get("https://api.github.com/repos/missing/repo")
  assert data == []
  assert next_url is None
