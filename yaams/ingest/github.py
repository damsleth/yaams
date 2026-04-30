from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Iterator

from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc


@dataclass
class GitHubAdapter:
  username: str
  include_private: bool = True
  include_forks: bool = False
  fetch_issues: bool = True
  fetch_prs: bool = True
  _token: str = field(default="", init=False, repr=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self._token = _get_token()
    cutoff = ensure_utc(since)
    repos = self._fetch_repos()
    for repo in repos:
      full_name = repo["full_name"]
      if self.fetch_issues:
        yield from self._fetch_issues(full_name, cutoff)
      if self.fetch_prs:
        yield from self._fetch_prs(full_name, cutoff)

  def _fetch_repos(self) -> list[dict]:
    visibility = "all" if self.include_private else "public"
    repos = self._paginate(f"/user/repos?visibility={visibility}&per_page=100&sort=pushed")
    return [r for r in repos if self.include_forks or not r.get("fork")]

  def _fetch_issues(self, full_name: str, since: datetime) -> Iterator[Item]:
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    path = f"/repos/{full_name}/issues?state=all&since={since_str}&per_page=100"
    for issue in self._paginate(path):
      if "pull_request" in issue:
        continue
      yield _issue_item(issue, full_name, self.username)

  def _fetch_prs(self, full_name: str, since: datetime) -> Iterator[Item]:
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    path = f"/repos/{full_name}/pulls?state=all&sort=updated&direction=desc&per_page=100"
    for pr in self._paginate(path):
      updated = datetime.fromisoformat(pr["updated_at"].rstrip("Z")).replace(tzinfo=UTC)
      if updated < since:
        break
      yield _pr_item(pr, full_name, self.username)

  def _paginate(self, path: str) -> list[dict]:
    results: list[dict] = []
    url = f"https://api.github.com{path}"
    while url:
      data, next_url = self._get(url)
      if isinstance(data, list):
        results.extend(data)
      url = next_url
    return results

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
      with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
        link = resp.headers.get("Link", "")
        next_url = _parse_next_link(link)
        return data, next_url
    except urllib.error.HTTPError:
      return [], None


def _get_token() -> str:
  result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
  if result.returncode != 0 or not result.stdout.strip():
    raise RuntimeError("GitHub auth failed - run 'gh auth login' first")
  return result.stdout.strip()


def _issue_item(issue: dict, repo: str, username: str) -> Item:
  number = issue["number"]
  author = (issue.get("user") or {}).get("login", "unknown")
  body = (issue.get("body") or "").strip()
  content = issue["title"]
  if body:
    content += f"\n\n{body}"
  return Item(
    id=hash_id("github", f"{repo}#issues/{number}"),
    source="github",
    source_id=f"{repo}#issues/{number}",
    timestamp=datetime.fromisoformat(issue["updated_at"].rstrip("Z")).replace(tzinfo=UTC),
    sender="me" if author == username else author,
    recipients=[],
    content=content,
    subject=issue["title"],
    thread_id=repo,
    raw_metadata={"repo": repo, "number": number, "type": "issue", "state": issue["state"], "url": issue["html_url"]},
  )


def _pr_item(pr: dict, repo: str, username: str) -> Item:
  number = pr["number"]
  author = (pr.get("user") or {}).get("login", "unknown")
  body = (pr.get("body") or "").strip()
  content = pr["title"]
  if body:
    content += f"\n\n{body}"
  return Item(
    id=hash_id("github", f"{repo}#pulls/{number}"),
    source="github",
    source_id=f"{repo}#pulls/{number}",
    timestamp=datetime.fromisoformat(pr["updated_at"].rstrip("Z")).replace(tzinfo=UTC),
    sender="me" if author == username else author,
    recipients=[],
    content=content,
    subject=pr["title"],
    thread_id=repo,
    raw_metadata={"repo": repo, "number": number, "type": "pr", "state": pr["state"], "url": pr["html_url"]},
  )


def _parse_next_link(link_header: str) -> str | None:
  for part in link_header.split(","):
    if 'rel="next"' in part:
      url = part.split(";")[0].strip().strip("<>")
      return url
  return None
