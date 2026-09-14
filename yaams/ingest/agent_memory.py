from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from yaams.config import expand_path
from yaams.ingest._markdown import (
  MIN_CONTENT_CHARS,
  collapse_blank_lines,
  parse_frontmatter,
  strip_frontmatter,
)
from yaams.ingest.base import Item, hash_id
from yaams.time import ensure_utc

# Durable memory written by coding agents about themselves and their repos.
# Two stores, two shapes, one source id:
#
#   Claude Code  ~/.claude/projects/<path-key>/memory/*.md
#                one fact per file, YAML frontmatter (name/description/metadata).
#                The repo is encoded only in the directory name, as the session's
#                cwd with "/" replaced by "-", so it has to be decoded by walking
#                the filesystem -- "-Users-damsleth-code-cognitive-ledger" is
#                ambiguous between .../cognitive/ledger and .../cognitive-ledger.
#
#   Codex        ~/.codex/memories/rollout_summaries/*.md   (one per session)
#                ~/.codex/memories/MEMORY.md                (many task groups)
#                Codex records the repo itself, as `applies_to: cwd=<path>` or
#                `(cwd=<path>, ...)`, so attribution is a parse, not a guess.
#
# MEMORY.md inside a Claude project dir is an index over its siblings, not a
# fact; ingesting it would duplicate every entry it points at.

CLAUDE_INDEX_FILE = "MEMORY.md"
# Trailing sentence punctuation is not part of the path: codex writes both
# "(cwd=/Users/me/code/x, ...)" and "cwd=/Users/me/code/x." in prose.
# Codex writes the working directory two ways: `cwd=<path>` inline in prose
# (MEMORY.md task groups) and `cwd: <path>` in a rollout summary's header
# block. Trailing sentence punctuation is not part of the path.
_CWD_RE = re.compile(r"cwd[=:]\s*([^\s,)]+?)[;.,:]?(?=[\s,)]|$)")
_HEADER_RE = re.compile(r"^([a-z_]+):\s*(.+)$", re.M)
_TASK_GROUP_RE = re.compile(r"^# Task Group:\s*(.+)$", re.M)
# raw_memories.md is a thread-ordered dump of unreviewed stage-1 material; the
# distilled rollout summaries cover the same sessions. Indexing both would
# double-count every codex session.
CODEX_SKIP_FILES = {"raw_memories.md"}


@dataclass
class AgentMemoryAdapter:
  claude_projects: Path | None = None
  codex_memories: Path | None = None
  repo_roots: tuple[Path, ...] = ()
  skipped_empty: int = field(default=0, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_empty = 0
    cutoff = ensure_utc(since)
    if self.claude_projects:
      yield from self._claude(expand_path(self.claude_projects), cutoff)
    if self.codex_memories:
      yield from self._codex(expand_path(self.codex_memories), cutoff)

  # --- Claude Code ------------------------------------------------------------

  def _claude(self, root: Path, cutoff: datetime) -> Iterator[Item]:
    if not root.exists():
      return
    for md in sorted(root.glob("*/memory/*.md")):
      if md.name == CLAUDE_INDEX_FILE:
        continue
      mtime = datetime.fromtimestamp(md.stat().st_mtime, tz=UTC)
      if mtime < cutoff:
        continue
      raw = md.read_text(encoding="utf-8", errors="replace")
      body = collapse_blank_lines(strip_frontmatter(raw))
      if len(body) < MIN_CONTENT_CHARS:
        self.skipped_empty += 1
        continue
      fm = parse_frontmatter(raw)
      meta = fm.get("metadata")
      meta = meta if isinstance(meta, dict) else {}
      key = md.parent.parent.name
      cwd = decode_project_key(key)
      yield Item(
        id=hash_id("agent_memory", f"claude/{key}/{md.name}"),
        source="agent_memory",
        source_id=f"claude/{key}/{md.name}",
        timestamp=mtime,
        timestamp_inferred=True,
        sender="me",
        recipients=[],
        content=body,
        subject=str(fm.get("description") or fm.get("name") or md.stem),
        thread_id=str(meta.get("originSessionId") or "") or None,
        lang=None,
        raw_metadata={
          "agent": "claude",
          "repo": repo_for(cwd, self.repo_roots),
          "cwd": str(cwd) if cwd else None,
          "memory_type": meta.get("type"),
          "path": str(md),
          "mtime": mtime.isoformat(),
        },
      )

  # --- Codex ------------------------------------------------------------------

  def _codex(self, root: Path, cutoff: datetime) -> Iterator[Item]:
    if not root.exists():
      return
    for md in sorted(root.glob("rollout_summaries/*.md")):
      mtime = datetime.fromtimestamp(md.stat().st_mtime, tz=UTC)
      if mtime < cutoff:
        continue
      raw = md.read_text(encoding="utf-8", errors="replace")
      body = collapse_blank_lines(strip_frontmatter(raw))
      if len(body) < MIN_CONTENT_CHARS:
        self.skipped_empty += 1
        continue
      head = rollout_header(raw)
      cwd = Path(head["cwd"]) if head.get("cwd") else first_cwd(raw)
      stamp, inferred = mtime, True
      if head.get("updated_at"):
        try:
          stamp, inferred = ensure_utc(datetime.fromisoformat(head["updated_at"])), False
        except ValueError:
          pass
      yield Item(
        id=hash_id("agent_memory", f"codex/rollout/{md.name}"),
        source="agent_memory",
        source_id=f"codex/rollout/{md.name}",
        timestamp=stamp,
        timestamp_inferred=inferred,
        sender="me",
        recipients=[],
        content=body,
        subject=md.stem,
        thread_id=head.get("thread_id"),
        lang=None,
        raw_metadata={
          "agent": "codex",
          "repo": repo_for(cwd, self.repo_roots),
          "cwd": str(cwd) if cwd else None,
          "memory_type": "rollout_summary",
          "git_branch": head.get("git_branch"),
          "rollout_path": head.get("rollout_path"),
          "path": str(md),
          "mtime": mtime.isoformat(),
        },
      )

    for name in ("MEMORY.md", "memory_summary.md"):
      md = root / name
      if not md.exists() or md.name in CODEX_SKIP_FILES:
        continue
      mtime = datetime.fromtimestamp(md.stat().st_mtime, tz=UTC)
      if mtime < cutoff:
        continue
      raw = md.read_text(encoding="utf-8", errors="replace")
      for idx, (title, chunk) in enumerate(split_task_groups(raw)):
        body = collapse_blank_lines(chunk)
        if len(body) < MIN_CONTENT_CHARS:
          self.skipped_empty += 1
          continue
        cwd = first_cwd(chunk)
        source_id = f"codex/{name}#{idx:03d}"
        yield Item(
          id=hash_id("agent_memory", source_id),
          source="agent_memory",
          source_id=source_id,
          timestamp=mtime,
          timestamp_inferred=True,
          sender="me",
          recipients=[],
          content=body,
          subject=title,
          thread_id=None,
          lang=None,
          raw_metadata={
            "agent": "codex",
            "repo": repo_for(cwd, self.repo_roots),
            "cwd": str(cwd) if cwd else None,
            "memory_type": "task_group",
            "path": str(md),
            "mtime": mtime.isoformat(),
          },
        )


# --- helpers ------------------------------------------------------------------


def decode_project_key(key: str) -> Path | None:
  """Turn "-Users-damsleth-code-cognitive-ledger" back into a real path.

  Claude Code replaces every "/" with "-" and keeps literal dashes as dashes,
  so the encoding is lossy: the key above could be .../cognitive/ledger or
  .../cognitive-ledger. Resolve it by walking the filesystem and preferring the
  longest segment that exists, which is what disambiguates the two.
  """
  parts = key.lstrip("-").split("-")
  cur, i = Path("/"), 0
  while i < len(parts):
    for j in range(len(parts), i, -1):
      candidate = cur / "-".join(parts[i:j])
      if candidate.is_dir():
        cur, i = candidate, j
        break
    else:
      return None
  return cur if cur != Path("/") else None


def first_cwd(text: str) -> Path | None:
  """Codex records the working directory inline, as `cwd=<path>`."""
  m = _CWD_RE.search(text)
  return Path(m.group(1)) if m else None


def rollout_header(text: str) -> dict[str, str]:
  """Read a codex rollout summary's leading `key: value` block.

  These files carry no `---` fences, so the generic frontmatter parser sees
  nothing. The block ends at the first blank line; everything after it is prose
  that may contain colons of its own.
  """
  head, _, _ = text.partition("\n\n")
  return {m.group(1): m.group(2).strip() for m in _HEADER_RE.finditer(head)}


def split_task_groups(text: str) -> list[tuple[str, str]]:
  """Split codex's MEMORY.md into its `# Task Group:` sections.

  One 145 KB file holding dozens of unrelated task groups retrieves badly: a
  query matches the whole blob, and the cited evidence is mostly about
  something else. Each group carries its own `applies_to: cwd=`, so splitting
  also makes repo attribution per-item rather than per-file.
  """
  matches = list(_TASK_GROUP_RE.finditer(text))
  if not matches:
    stripped = text.strip()
    return [("codex memory", stripped)] if stripped else []
  out: list[tuple[str, str]] = []
  for n, m in enumerate(matches):
    end = matches[n + 1].start() if n + 1 < len(matches) else len(text)
    out.append((m.group(1).strip(), text[m.start():end]))
  return out


def repo_for(cwd: Path | None, repo_roots: tuple[Path, ...]) -> str | None:
  """Name the git repo a cwd belongs to, walking up to the enclosing worktree.

  Returns the repo's directory name, not its path, so the same repo checked out
  in two places is one key. Nested repos resolve to the innermost one, which is
  what the caller means: NOCOS-Main inside norconsult is its own repo.
  """
  if cwd is None:
    return None
  cur = cwd if cwd.is_dir() else cwd.parent
  home = Path.home()
  while cur != cur.parent and cur != home:
    if (cur / ".git").exists():
      return cur.name
    cur = cur.parent
  # Not a repo: fall back to the path itself, encoded, so user-scoped memory
  # still gets a stable key instead of being dropped.
  try:
    rel = cwd.relative_to(home)
  except ValueError:
    return None
  return "-" + str(rel).replace("/", "-") if str(rel) != "." else None
