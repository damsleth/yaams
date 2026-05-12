from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Iterator

from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc

logger = logging.getLogger(__name__)


MAX_EVENT_PAGES = 3
PAGE_SIZE = 100
REQUEST_TIMEOUT = 30
AUTH_TIMEOUT = 15


class GitHubAPIError(RuntimeError):
  """Raised for GitHub HTTP failures that should abort or surface, not silently skip."""

  def __init__(self, status: int, url: str, message: str = ""):
    self.status = status
    self.url = url
    super().__init__(f"GitHub API {status} on {url}: {message}".rstrip(": "))


@dataclass
class GitHubAdapter:
  username: str
  _token: str = field(default="", init=False, repr=False)
  _last_rate: dict[str, str] = field(default_factory=dict, init=False, repr=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    logger.info("github: starting extract user=%s since=%s", self.username, since.isoformat())
    self._token = _get_token()
    cutoff = ensure_utc(since)
    yielded = 0
    seen = 0
    for event in self._iter_events(cutoff):
      seen += 1
      item = _event_item(event, self.username)
      if item is None:
        continue
      yielded += 1
      yield item
    logger.info(
      "github: extract complete user=%s seen=%d yielded=%d cutoff=%s",
      self.username, seen, yielded, cutoff.isoformat(),
    )

  def _iter_events(self, since: datetime) -> Iterator[dict]:
    url = f"https://api.github.com/users/{self.username}/events?per_page={PAGE_SIZE}"
    pages = 0
    total_events = 0
    skipped_old = 0
    while url and pages < MAX_EVENT_PAGES:
      logger.debug("github: GET page %d url=%s", pages + 1, url)
      t0 = time.perf_counter()
      data, next_url = self._get(url)
      elapsed_ms = (time.perf_counter() - t0) * 1000
      pages += 1
      page_count = len(data) if isinstance(data, list) else 0
      total_events += page_count
      logger.info(
        "github: page %d/%d in %.0fms events=%d rate_remaining=%s reset=%s next=%s",
        pages, MAX_EVENT_PAGES, elapsed_ms, page_count,
        self._last_rate.get("remaining", "?"), self._last_rate.get("reset", "?"),
        "yes" if next_url else "no",
      )
      if not isinstance(data, list) or not data:
        return
      stop = False
      for event in data:
        created = _parse_ts(event.get("created_at"))
        if created is None:
          continue
        if created < since:
          skipped_old += 1
          stop = True
          break
        yield event
      if stop:
        logger.debug("github: page %d hit since cutoff (%s); stopping pagination", pages, since.isoformat())
        return
      url = next_url
    logger.info(
      "github: pagination ended pages=%d total_events_scanned=%d skipped_older_than_since=%d",
      pages, total_events, skipped_old,
    )

  def _get(self, url: str) -> tuple[list[dict], str | None]:
    req = urllib.request.Request(
      url,
      headers={
        "Authorization": f"Bearer {self._token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
    )
    try:
      with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = resp.read()
        data = json.loads(body)
        link = resp.headers.get("Link", "")
        next_url = _parse_next_link(link)
        self._last_rate = {
          "remaining": resp.headers.get("X-RateLimit-Remaining", ""),
          "limit": resp.headers.get("X-RateLimit-Limit", ""),
          "reset": resp.headers.get("X-RateLimit-Reset", ""),
        }
        logger.debug("github: response %d bytes status=%s", len(body), resp.status)
        return data, next_url
    except urllib.error.HTTPError as exc:
      if exc.code == 404:
        logger.warning("GitHub 404 on %s - skipping", url)
        return [], None
      body = ""
      try:
        body = exc.read().decode("utf-8", errors="replace")[:200]
      except Exception:
        pass
      logger.error("github: HTTP %d on %s body=%r", exc.code, url, body)
      raise GitHubAPIError(exc.code, url, body) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
      logger.error("github: network error on %s: %s", url, exc)
      raise


def _get_token() -> str:
  logger.debug("github: invoking `gh auth token`")
  t0 = time.perf_counter()
  try:
    result = subprocess.run(
      ["gh", "auth", "token"],
      capture_output=True,
      text=True,
      timeout=AUTH_TIMEOUT,
    )
  except subprocess.TimeoutExpired as exc:
    logger.error("github: `gh auth token` timed out after %ds", AUTH_TIMEOUT)
    raise RuntimeError(
      f"`gh auth token` timed out after {AUTH_TIMEOUT}s - run 'gh auth login' interactively"
    ) from exc
  except FileNotFoundError as exc:
    logger.error("github: gh CLI not found on PATH")
    raise RuntimeError("`gh` CLI not found - install it or remove github from sources") from exc
  elapsed_ms = (time.perf_counter() - t0) * 1000
  if result.returncode != 0 or not result.stdout.strip():
    logger.error(
      "github: `gh auth token` failed in %.0fms rc=%d stderr=%r",
      elapsed_ms, result.returncode, result.stderr[:200],
    )
    raise RuntimeError("GitHub auth failed - run 'gh auth login' first")
  logger.debug("github: token retrieved in %.0fms", elapsed_ms)
  return result.stdout.strip()


def _parse_ts(value: str | None) -> datetime | None:
  if not value:
    return None
  return datetime.fromisoformat(value.rstrip("Z")).replace(tzinfo=UTC)


def _event_item(event: dict, username: str) -> Item | None:
  event_id = event.get("id")
  event_type = event.get("type") or "UnknownEvent"
  repo = (event.get("repo") or {}).get("name") or ""
  ts = _parse_ts(event.get("created_at"))
  if not event_id or ts is None:
    return None
  payload = event.get("payload") or {}
  subject, content, url = _render(event_type, payload, repo)
  source_id = f"event:{event_id}"
  return Item(
    id=hash_id("github", source_id),
    source="github",
    source_id=source_id,
    timestamp=ts,
    sender="me",
    recipients=[],
    content=content,
    subject=subject,
    thread_id=repo or None,
    raw_metadata={
      "repo": repo,
      "type": event_type,
      "event_id": event_id,
      "url": url,
      "public": event.get("public", True),
    },
  )


def _render(event_type: str, payload: dict, repo: str) -> tuple[str, str, str | None]:
  renderer = _RENDERERS.get(event_type, _render_unknown)
  return renderer(payload, repo, event_type)


def _render_push(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  commits = payload.get("commits") or []
  ref = (payload.get("ref") or "").replace("refs/heads/", "")
  subject = f"Pushed {len(commits)} commit(s) to {repo}" + (f" ({ref})" if ref else "")
  lines = [f"- {(c.get('message') or '').strip().splitlines()[0][:200]}" for c in commits if c.get("message")]
  content = subject + ("\n\n" + "\n".join(lines) if lines else "")
  return subject, content, None


def _render_pr(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  pr = payload.get("pull_request") or {}
  action = payload.get("action") or "updated"
  title = pr.get("title") or ""
  body = (pr.get("body") or "").strip()
  subject = f"PR {action}: {title}" if title else f"PR {action} in {repo}"
  content = subject + (f"\n\n{body}" if body else "")
  return subject, content, pr.get("html_url")


def _render_pr_review(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  pr = payload.get("pull_request") or {}
  review = payload.get("review") or {}
  title = pr.get("title") or ""
  state = review.get("state") or ""
  body = (review.get("body") or "").strip()
  subject = f"Reviewed PR ({state}): {title}" if title else f"Reviewed PR in {repo}"
  content = subject + (f"\n\n{body}" if body else "")
  return subject, content, review.get("html_url") or pr.get("html_url")


def _render_pr_review_comment(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  pr = payload.get("pull_request") or {}
  comment = payload.get("comment") or {}
  title = pr.get("title") or ""
  body = (comment.get("body") or "").strip()
  subject = f"Commented on PR: {title}" if title else f"PR review comment in {repo}"
  content = subject + (f"\n\n{body}" if body else "")
  return subject, content, comment.get("html_url")


def _render_issue(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  issue = payload.get("issue") or {}
  action = payload.get("action") or "updated"
  title = issue.get("title") or ""
  body = (issue.get("body") or "").strip()
  subject = f"Issue {action}: {title}" if title else f"Issue {action} in {repo}"
  content = subject + (f"\n\n{body}" if body else "")
  return subject, content, issue.get("html_url")


def _render_issue_comment(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  issue = payload.get("issue") or {}
  comment = payload.get("comment") or {}
  title = issue.get("title") or ""
  body = (comment.get("body") or "").strip()
  subject = f"Commented on issue: {title}" if title else f"Issue comment in {repo}"
  content = subject + (f"\n\n{body}" if body else "")
  return subject, content, comment.get("html_url")


def _render_create(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  ref_type = payload.get("ref_type") or "ref"
  ref = payload.get("ref") or repo
  subject = f"Created {ref_type} {ref} in {repo}" if ref_type != "repository" else f"Created repository {repo}"
  return subject, subject, None


def _render_delete(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  ref_type = payload.get("ref_type") or "ref"
  ref = payload.get("ref") or ""
  subject = f"Deleted {ref_type} {ref} in {repo}"
  return subject, subject, None


def _render_fork(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  forkee = payload.get("forkee") or {}
  full = forkee.get("full_name") or "?"
  subject = f"Forked {repo} -> {full}"
  return subject, subject, forkee.get("html_url")


def _render_release(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  release = payload.get("release") or {}
  action = payload.get("action") or "published"
  name = release.get("name") or release.get("tag_name") or ""
  body = (release.get("body") or "").strip()
  subject = f"Release {action} in {repo}: {name}" if name else f"Release {action} in {repo}"
  content = subject + (f"\n\n{body}" if body else "")
  return subject, content, release.get("html_url")


def _render_watch(_payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  subject = f"Starred {repo}"
  return subject, subject, None


def _render_public(_payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  subject = f"Made {repo} public"
  return subject, subject, None


def _render_commit_comment(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  comment = payload.get("comment") or {}
  sha = (comment.get("commit_id") or "")[:7]
  body = (comment.get("body") or "").strip()
  subject = f"Commented on commit {sha} in {repo}" if sha else f"Commit comment in {repo}"
  content = subject + (f"\n\n{body}" if body else "")
  return subject, content, comment.get("html_url")


def _render_member(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  action = payload.get("action") or "changed"
  member = (payload.get("member") or {}).get("login") or "?"
  subject = f"{action.title()} {member} on {repo}"
  return subject, subject, None


def _render_gollum(payload: dict, repo: str, _type: str) -> tuple[str, str, str | None]:
  pages = payload.get("pages") or []
  titles = [p.get("title", "") for p in pages if p.get("title")]
  subject = f"Wiki edits on {repo}: {len(pages)} page(s)"
  content = subject + ("\n\n" + "\n".join(f"- {t}" for t in titles) if titles else "")
  return subject, content, None


def _render_unknown(_payload: dict, repo: str, event_type: str) -> tuple[str, str, str | None]:
  subject = f"{event_type} on {repo}" if repo else event_type
  return subject, subject, None


_RENDERERS = {
  "PushEvent": _render_push,
  "PullRequestEvent": _render_pr,
  "PullRequestReviewEvent": _render_pr_review,
  "PullRequestReviewCommentEvent": _render_pr_review_comment,
  "IssuesEvent": _render_issue,
  "IssueCommentEvent": _render_issue_comment,
  "CreateEvent": _render_create,
  "DeleteEvent": _render_delete,
  "ForkEvent": _render_fork,
  "ReleaseEvent": _render_release,
  "WatchEvent": _render_watch,
  "PublicEvent": _render_public,
  "CommitCommentEvent": _render_commit_comment,
  "MemberEvent": _render_member,
  "GollumEvent": _render_gollum,
}


def _parse_next_link(link_header: str) -> str | None:
  for part in link_header.split(","):
    if 'rel="next"' in part:
      url = part.split(";")[0].strip().strip("<>")
      return url
  return None
