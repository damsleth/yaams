"""Microsoft Teams **channel** ingester (root posts + threaded replies).

Shells out to ``owa-teams`` — the same thin-adapter pattern the calendar
(`CalendarAdapter` -> ``owa-cal``) and mail (`M365MailAdapter` -> ``owa-mail``)
sources use. ``owa-teams`` owns all the Teams/chatsvc complexity (dual-door
auth, regional chat service, ``rootMessageId`` threading); this adapter just
maps its JSON rows to `Item`s. One yaams source per profile:
``teams_channels_<profile>`` — distinct from chat ingestion's ``teams_<profile>``
so routing and watermarks tell channels apart from chats.

Cost note: a naive run is 1 ``teams`` call + 1 ``channels`` call per team +
1 ``messages`` call per channel (an N+1 subprocess fan-out, each re-minting a
token). Use the ``teams`` allowlist to ingest only the teams you care about;
the default config ships ``enabled: false`` for the same reason.

``owa-teams messages`` has no ``--since`` yet, so v1 fetches ``--limit`` pages
(newest-first; owa-teams reverses to chronological) and filters
``timestamp <= cutoff`` here. Steady-state daily ingest is fine; a cold start
of a very busy channel can miss history older than ``limit_pages × ~50``
messages — acceptable for v1, removed once owa-teams grows ``--since``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from yaams.ingest.base import Item, hash_id
from yaams.ingest.teams import _BOT_LIKE_NAMES, MAX_TEAMS_CHARS
from yaams.time import ensure_utc, parse_iso_datetime

logger = logging.getLogger("yaams.ingest.teams_channels")

# Channel ingestion fans out one owa-teams call per team + per channel, which
# bursts enough requests to trip chatsvc's rate limiter. owa-teams surfaces a
# 429 as a non-zero exit rather than honoring Retry-After itself, so the
# adapter retries rate-limited verbs with exponential backoff (1, 2, 4, … s).
DEFAULT_MAX_RETRIES = 5
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_CAP_SEC = 30.0


@dataclass
class TeamsChannelsAdapter:
  profile: str
  teams: tuple[str, ...] = ()      # team-id allowlist; empty = all joined teams
  limit_pages: int = 4             # owa-teams --limit (pages of ~50)
  skip_bots: bool = True
  max_retries: int = DEFAULT_MAX_RETRIES  # retries per owa-teams verb on 429
  skipped_bots: int = field(default=0, init=False)
  skipped_empty: int = field(default=0, init=False)
  rate_limit_retries: int = field(default=0, init=False)  # 429 backoffs this run

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_bots = 0
    self.skipped_empty = 0
    self.rate_limit_retries = 0
    cutoff = ensure_utc(since)
    for team_id, team_name in self._teams():
      for ch_id, ch_name in self._channels(team_id):
        for row in self._messages(ch_id, team_id):
          ts_str = row.get("timestamp")
          if not ts_str:
            self.skipped_empty += 1
            continue
          try:
            ts = parse_iso_datetime(ts_str)
          except ValueError:
            self.skipped_empty += 1
            continue
          # owa-teams returns chronological (oldest-first) and has no --since
          # yet, so drop pre-cutoff rows here rather than break early.
          if ts <= cutoff:
            continue
          if self.skip_bots and _is_bot_row(row):
            self.skipped_bots += 1
            continue
          item = _to_item(row, self.profile, team_id, team_name, ch_name)
          if item is None:
            self.skipped_empty += 1
            continue
          yield item

  def _teams(self) -> list[tuple[str, str]]:
    """Yield (team_id, team_name) for joined teams, honoring the allowlist."""
    allow = set(self.teams)
    out: list[tuple[str, str]] = []
    for team in self._run(["teams"]):
      team_id = team.get("id")
      if not team_id:
        continue
      if allow and team_id not in allow:
        continue
      out.append((team_id, (team.get("displayName") or "").strip()))
    return out

  def _channels(self, team_id: str) -> list[tuple[str, str]]:
    """Yield (channel_id, channel_name) for a team's channels.

    owa-teams does not stamp ``channelName`` onto message rows, so the
    adapter holds the id->name map from this call and applies it itself.
    """
    out: list[tuple[str, str]] = []
    for channel in self._run(["channels", "--team", team_id]):
      channel_id = channel.get("id")
      if not channel_id:
        continue
      out.append((channel_id, (channel.get("displayName") or "").strip()))
    return out

  def _messages(self, channel_id: str, team_id: str) -> list[dict]:
    return self._run([
      "messages",
      "--channel", channel_id,
      "--team", team_id,
      "--limit", str(self.limit_pages),
    ])

  def _run(self, args: list[str]) -> list[dict]:
    """Run ``owa-teams <args> --profile <p>`` and parse its JSON stdout.

    Mirrors the calendar/mail adapters: check ``returncode``, tolerate empty
    output and non-JSON, never raise on a single failed verb (return ``[]``).
    Rate-limit (429) failures are retried with exponential backoff; the fan-out
    bursts enough calls to trip chatsvc's limiter and owa-teams exits non-zero
    rather than waiting itself, so without this a single 429 silently drops a
    whole team's channels.
    """
    verb = args[0] if args else "?"
    cmd = ["owa-teams", *args, "--profile", self.profile]
    attempts = max(self.max_retries, 0) + 1
    for attempt in range(attempts):
      result = subprocess.run(cmd, capture_output=True, text=True)
      if result.returncode == 0:
        return _parse_rows(result.stdout, verb, self.profile)
      if _is_rate_limited(result) and attempt + 1 < attempts:
        delay = min(_BACKOFF_BASE_SEC * 2 ** attempt, _BACKOFF_CAP_SEC)
        self.rate_limit_retries += 1
        logger.warning(
          "owa-teams %s rate-limited (429); backing off %.1fs "
          "(retry %d/%d, profile=%s)",
          verb, delay, attempt + 1, self.max_retries, self.profile,
        )
        time.sleep(delay)
        continue
      logger.warning(
        "owa-teams %s failed (profile=%s rc=%d): %s",
        verb, self.profile, result.returncode,
        (result.stderr or "").strip() or "no stderr",
      )
      return []
    return []  # unreachable (loop always returns), keeps the function total


def _parse_rows(stdout: str, verb: str, profile: str) -> list[dict]:
  if not stdout.strip():
    return []
  try:
    data = json.loads(stdout)
  except json.JSONDecodeError:
    logger.warning("owa-teams %s returned non-JSON (profile=%s)", verb, profile)
    return []
  return data if isinstance(data, list) else []


def _is_rate_limited(result: subprocess.CompletedProcess) -> bool:
  """A 429 from chatsvc; owa-teams prints e.g. ``ERROR: rate limited (429)``."""
  blob = f"{result.stdout or ''} {result.stderr or ''}".lower()
  return "429" in blob or "rate limit" in blob


def _is_bot_row(row: dict) -> bool:
  """Channels rarely carry bots; owa-teams only drops system/empty, so we
  guard on the sender display name the same way the chat adapter does."""
  name = ((row.get("from") or {}).get("name") or "").strip()
  return bool(name and _BOT_LIKE_NAMES.match(name))


def _to_item(
  row: dict,
  profile: str,
  team_id: str,
  team_name: str,
  channel_name: str,
) -> Item | None:
  content = (row.get("content") or "").strip()  # already HTML-stripped by owa-teams
  if not content:
    return None
  if len(content) > MAX_TEAMS_CHARS:
    content = content[:MAX_TEAMS_CHARS]

  ts_str = row.get("timestamp")
  if not ts_str:
    return None
  try:
    timestamp = parse_iso_datetime(ts_str)
  except ValueError:
    return None

  sender_obj = row.get("from") or {}
  sender = (sender_obj.get("name") or sender_obj.get("id") or "unknown").strip()
  channel_id = row.get("channelId") or ""
  message_id = str(row.get("id") or "")
  source_id = f"{channel_id}:{message_id}"
  root_id = row.get("rootMessageId") or message_id
  # owa-teams already builds threadId as "{channelId}:{rootId}"; fall back to
  # composing it ourselves if a row predates that field.
  thread_id = row.get("threadId") or f"{channel_id}:{root_id}"
  # Roots carry `subject`; replies inherit the root's subject from owa-teams.
  # When neither has one, fall back to the team/channel name so the item is
  # still labelled.
  subject = (row.get("subject") or "").strip() or f"{team_name} / {channel_name}"

  source = f"teams_channels_{profile}"
  return Item(
    id=hash_id(source, source_id),
    source=source,
    source_id=source_id,
    timestamp=timestamp,
    sender=sender,
    recipients=[],          # a channel post is a broadcast, not addressed
    content=content,
    subject=subject,
    thread_id=thread_id,
    raw_metadata={
      "profile": profile,
      "chat_type": "channel",
      "team_id": team_id,
      "team_name": team_name,
      "channel_id": channel_id,
      "channel_name": channel_name,
      "root_message_id": row.get("rootMessageId"),
      "is_reply": bool(row.get("isReply")),
      "sequence_id": row.get("sequenceId"),
      "sender_mri": sender_obj.get("mri"),
      "message_type": row.get("messageType"),
    },
  )
