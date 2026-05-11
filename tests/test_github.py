from __future__ import annotations

import io
import urllib.error
from datetime import UTC, datetime
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


def _event(event_id: str, event_type: str, created_at: str, payload: dict | None = None, repo: str = "me/proj") -> dict:
  return {
    "id": event_id,
    "type": event_type,
    "actor": {"login": "me"},
    "repo": {"name": repo},
    "payload": payload or {},
    "created_at": created_at,
    "public": True,
  }


def test_get_raises_on_auth_failure():
  adapter = GitHubAdapter(username="me")
  adapter._token = "fake"
  with patch("urllib.request.urlopen", side_effect=_http_error(401, b"Bad credentials")):
    with pytest.raises(GitHubAPIError) as exc_info:
      adapter._get("https://api.github.com/user/events")
  assert exc_info.value.status == 401


def test_get_raises_on_rate_limit():
  adapter = GitHubAdapter(username="me")
  adapter._token = "fake"
  with patch("urllib.request.urlopen", side_effect=_http_error(429)):
    with pytest.raises(GitHubAPIError) as exc_info:
      adapter._get("https://api.github.com/user/events")
  assert exc_info.value.status == 429


def test_get_raises_on_server_error():
  adapter = GitHubAdapter(username="me")
  adapter._token = "fake"
  with patch("urllib.request.urlopen", side_effect=_http_error(503)):
    with pytest.raises(GitHubAPIError):
      adapter._get("https://api.github.com/user/events")


def test_get_returns_empty_on_404():
  adapter = GitHubAdapter(username="me")
  adapter._token = "fake"
  with patch("urllib.request.urlopen", side_effect=_http_error(404)):
    data, next_url = adapter._get("https://api.github.com/repos/missing/repo")
  assert data == []
  assert next_url is None


def test_iter_events_short_circuits_on_stale_event():
  adapter = GitHubAdapter(username="me")
  adapter._token = "fake"
  page = [
    _event("3", "PushEvent", "2026-05-11T10:00:00Z"),
    _event("2", "PushEvent", "2026-05-10T10:00:00Z"),
    _event("1", "PushEvent", "2026-01-01T00:00:00Z"),
  ]
  with patch.object(adapter, "_get", return_value=(page, "https://api.github.com/next")) as m:
    events = list(adapter._iter_events(datetime(2026, 5, 1, tzinfo=UTC)))
  assert [e["id"] for e in events] == ["3", "2"]
  assert m.call_count == 1


def test_iter_events_paginates_until_stale_then_stops():
  adapter = GitHubAdapter(username="me")
  adapter._token = "fake"
  page1 = [_event("4", "PushEvent", "2026-05-11T10:00:00Z")]
  page2 = [_event("3", "PushEvent", "2026-05-09T10:00:00Z"), _event("2", "PushEvent", "2026-01-01T00:00:00Z")]
  responses = iter([(page1, "https://api.github.com/p2"), (page2, "https://api.github.com/p3")])
  with patch.object(adapter, "_get", side_effect=lambda _url: next(responses)) as m:
    events = list(adapter._iter_events(datetime(2026, 5, 1, tzinfo=UTC)))
  assert [e["id"] for e in events] == ["4", "3"]
  assert m.call_count == 2


def test_extract_yields_items_with_event_id_as_source_id():
  adapter = GitHubAdapter(username="me")
  events = [_event("42", "PushEvent", "2026-05-11T10:00:00Z", {"commits": [{"message": "fix"}], "ref": "refs/heads/main"})]
  with patch("yaams.ingest.github._get_token", return_value="fake"), \
       patch.object(adapter, "_get", return_value=(events, None)):
    items = list(adapter.extract(datetime(2026, 5, 1, tzinfo=UTC)))
  assert len(items) == 1
  item = items[0]
  assert item.source == "github"
  assert item.source_id == "event:42"
  assert item.sender == "me"
  assert item.thread_id == "me/proj"
  assert "Pushed 1 commit(s)" in item.subject
  assert "fix" in item.content


def test_renders_pull_request_event():
  adapter = GitHubAdapter(username="me")
  events = [_event("5", "PullRequestEvent", "2026-05-11T10:00:00Z", {
    "action": "opened",
    "pull_request": {"title": "Add foo", "body": "details here", "html_url": "https://github.com/me/proj/pull/1"},
  })]
  with patch("yaams.ingest.github._get_token", return_value="fake"), \
       patch.object(adapter, "_get", return_value=(events, None)):
    items = list(adapter.extract(datetime(2026, 5, 1, tzinfo=UTC)))
  assert items[0].subject == "PR opened: Add foo"
  assert "details here" in items[0].content
  assert items[0].raw_metadata["url"] == "https://github.com/me/proj/pull/1"


def test_renders_issue_comment_event():
  adapter = GitHubAdapter(username="me")
  events = [_event("6", "IssueCommentEvent", "2026-05-11T10:00:00Z", {
    "action": "created",
    "issue": {"title": "Bug 1"},
    "comment": {"body": "lgtm", "html_url": "https://github.com/me/proj/issues/1#c"},
  })]
  with patch("yaams.ingest.github._get_token", return_value="fake"), \
       patch.object(adapter, "_get", return_value=(events, None)):
    items = list(adapter.extract(datetime(2026, 5, 1, tzinfo=UTC)))
  assert items[0].subject == "Commented on issue: Bug 1"
  assert "lgtm" in items[0].content


def test_unknown_event_type_has_generic_subject():
  adapter = GitHubAdapter(username="me")
  events = [_event("7", "SponsorshipEvent", "2026-05-11T10:00:00Z", {})]
  with patch("yaams.ingest.github._get_token", return_value="fake"), \
       patch.object(adapter, "_get", return_value=(events, None)):
    items = list(adapter.extract(datetime(2026, 5, 1, tzinfo=UTC)))
  assert items[0].subject == "SponsorshipEvent on me/proj"


def test_extract_caps_at_max_pages():
  adapter = GitHubAdapter(username="me")
  page = [_event(str(i), "PushEvent", "2026-05-11T10:00:00Z") for i in range(100)]
  with patch("yaams.ingest.github._get_token", return_value="fake"), \
       patch.object(adapter, "_get", return_value=(page, "https://api.github.com/next")) as m:
    items = list(adapter.extract(datetime(2026, 5, 1, tzinfo=UTC)))
  assert len(items) == 300
  assert m.call_count == 3
