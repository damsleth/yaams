"""Microsoft Teams **channel** ingester (root posts + threaded replies).

Shells out to ``owa-teams`` — the same thin-adapter pattern the calendar
(`CalendarAdapter` -> ``owa-cal``) and mail (`M365MailAdapter` -> ``owa-mail``)
sources use. ``owa-teams`` owns all the Teams/chatsvc complexity (dual-door
auth, regional chat service, ``rootMessageId`` threading); this adapter just
maps its JSON rows to `Item`s. One yaams source per profile:
``teams_channels_<profile>`` — distinct from chat ingestion's ``teams_<profile>``
so routing and watermarks tell channels apart from chats.

Cost note: a run is 1 ``teams`` call + 1 ``channels`` call per team +
1 ``messages`` call per channel (an N+1 subprocess fan-out, each re-minting a
token). The two fan-out levels run in a small thread pool — the work is all
subprocess wait — and ``owa-teams messages --since`` stops paging at the
watermark, so a steady-state run is ~1 page per channel. Use the ``teams``
allowlist to bound the fan-out further.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
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
#
# swon (chatsvc-gated tenant) status (verified 2026-06-10):
#   owa-teams teams --profile swon exits rc=11 (auth expired / 401).
#   The profile needs a fresh owa-piggy token before channel ingest can run.
#   Run: owa-piggy setup --profile swon
#   Once re-authed, swon should work like any other profile — chatsvc routing
#   is handled inside owa-teams, not here.
DEFAULT_MAX_RETRIES = 5
_BACKOFF_BASE_SEC = 1.0
_BACKOFF_CAP_SEC = 30.0

# Per-call wall-clock cap. Measured: `owa-teams channels` returns in 0.6-2.2s,
# but roughly one call per run hangs and comes back at ~61s (a chatsvc request
# with no client-side timeout riding some upstream 60s limit). One such hang
# was the whole difference between a 19s and a 1m32s ingest, so cut it off and
# retry instead of waiting it out.
_CALL_TIMEOUT_SEC = 20.0

# Audiences owa-teams needs: `teams` and `channels` run on graph, `messages` on
# ic3 (owa_teams/cli.py tags each verb with `auth=`). Both are warmed up front.
_PREWARM_AUDIENCES = ("graph", "ic3")

# Cap for a prewarm mint. A cold one launches a headless Edge and takes ~40s;
# this is the one call in the run that must be allowed to finish.
_PREWARM_TIMEOUT_SEC = 180.0

# Content-pattern filter: skip automated/system posts whose body starts with
# well-known Microsoft admin digest / Message Center patterns.
_AUTOMATED_CONTENT_RE = re.compile(
  r"^(Message ID:\s*MC\d+|Published date:|Action required by:|"
  r"(Type|Category):\s*(Message center|Advisory))",
  re.IGNORECASE | re.MULTILINE,
)


# Grace between SIGTERM and SIGKILL for a timed-out call's process group.
# Enough for Chromium to clear its SingletonLock on the way out.
_KILL_GRACE_SEC = 2.0


def _kill_group(proc: subprocess.Popen) -> None:
  """SIGTERM the child's whole process group, then SIGKILL the survivors.

  ``start_new_session=True`` makes the child a group leader, so its pid doubles
  as the pgid and one signal reaches every descendant.
  """
  pgid = proc.pid
  with contextlib.suppress(OSError):
    os.killpg(pgid, signal.SIGTERM)
  with contextlib.suppress(subprocess.TimeoutExpired):
    proc.wait(timeout=_KILL_GRACE_SEC)
  with contextlib.suppress(OSError):
    os.killpg(pgid, signal.SIGKILL)


def _run_capped(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
  """``subprocess.run`` with a wall-clock cap that also kills the child's
  *descendants*.

  ``subprocess.run(timeout=...)`` kills only the direct child, and that is not
  enough here. ``owa-teams`` shells out to ``owa-piggy token``, which launches a
  headless Edge holding Chromium's SingletonLock on the profile dir. Kill the
  middle process alone and Edge is reparented to init and squats that dir, so
  the retry we are about to make gets singleton-forwarded, never binds its debug
  port and times out too - leaking one more Edge per attempt and burning the
  whole retry ladder on a fault we caused ourselves. Observed 2026-09-07 on
  profile nc, where it outlived the ingest that started it.

  The ``finally`` covers the other direction: if yaams is interrupted mid-call,
  the child group goes down with us instead of being orphaned. Nothing helps
  against a SIGKILL to yaams itself.

  Raises `subprocess.TimeoutExpired` exactly like `subprocess.run`, so callers
  keep their existing except-clause.
  """
  with subprocess.Popen(
    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    start_new_session=True,
  ) as proc:
    try:
      out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
      _kill_group(proc)
      out, err = proc.communicate()
      raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err) from None
    finally:
      if proc.poll() is None:
        _kill_group(proc)
  return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _prewarm_tokens(profile: str) -> None:
  """Mint each audience owa-teams needs once, serially, before the fan-out.

  Without this the source livelocks on a cold cache. `_CALL_TIMEOUT_SEC` is 20s,
  but a cold `owa-piggy token` mint launches a headless Edge and takes ~38s, so
  every capped call was killed *before* it could write the token to cache. The
  retry then paid the same doomed mint, and so did every later channel: 25
  timeouts and zero rows, observed 2026-09-07 on profile nc. Warming here moves
  that one-time cost outside the cap; calls afterwards return in ~0.5s.

  `extract` already ran `_teams()` serially "to warm the token cache", but that
  only ever covered the graph audience - `messages` runs on ic3 and stayed cold,
  which is exactly the call that spent the whole run timing out.

  Best-effort by design: a failure here is not fatal. The fan-out still tries on
  its own and reports through the normal per-call error path.
  """
  for audience in _PREWARM_AUDIENCES:
    cmd = ["owa-piggy", "token", "--audience", audience, "--profile", profile]
    try:
      result = _run_capped(cmd, _PREWARM_TIMEOUT_SEC)
    except (subprocess.TimeoutExpired, OSError) as exc:
      logger.warning("token prewarm %s/%s failed: %s", profile, audience, exc)
      continue
    if result.returncode != 0:
      # stdout carries the token itself, so only stderr is ever logged.
      logger.warning(
        "token prewarm %s/%s failed (rc=%d): %s",
        profile, audience, result.returncode,
        (result.stderr or "").strip() or "no stderr",
      )


@dataclass
class TeamsChannelsAdapter:
  profile: str
  teams: tuple[str, ...] = ()      # team-id allowlist; empty = all joined teams
  # Channel-id denylist. Graph's /teams/{id}/channels lists private channels
  # you can see but not read, so ingest 403s on them every run. Ids are
  # globally unique, so this is a flat list - no team/profile nesting.
  skip_channels: frozenset[str] = frozenset()
  limit_pages: int = 4             # owa-teams --limit safety cap (pages of ~50)
  # Overrides limit_pages for a one-time deep backfill (set
  # teams_channels.backfill_limit_pages in config) without raising the
  # steady-state cap. Rarely needed now that --since bounds the paging.
  backfill_limit_pages: int | None = None
  # Threads for the channels/messages fan-out. Small on purpose: the outer
  # ingest already runs up to 8 sources at once, and chatsvc rate-limits.
  # ponytail: fixed 8; make it configurable only if 429 retries show up.
  max_workers: int = 8
  skip_bots: bool = True
  max_retries: int = DEFAULT_MAX_RETRIES  # retries per owa-teams verb on 429
  skipped_bots: int = field(default=0, init=False)
  skipped_empty: int = field(default=0, init=False)
  skipped_automated: int = field(default=0, init=False)  # content-pattern filtered
  rate_limit_retries: int = field(default=0, init=False)  # 429 backoffs this run
  timed_out_calls: int = field(default=0, init=False)     # calls cut off as hung

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_bots = 0
    self.skipped_empty = 0
    self.skipped_automated = 0
    self.rate_limit_retries = 0
    self.timed_out_calls = 0
    cutoff = ensure_utc(since)
    _prewarm_tokens(self.profile)  # both audiences, before anything is capped
    teams = self._teams()
    # One task per team, each doing its own channels-then-messages calls.
    # Deliberately not two pool.map passes: that barriers every messages
    # call behind the slowest channels call, and per-call latency is 0.6-6s
    # of server variance, so one outlier stalls the whole source.
    # ponytail: inner calls stay serial per team — nesting submits into the
    # same pool risks deadlock, and team-level width is enough.
    with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
      fanned = [
        fetched
        for per_team in pool.map(lambda t: self._fetch_team(t, cutoff), teams)
        for fetched in per_team
      ]
    for (team_id, team_name, ch_name), rows in fanned:
      for row in rows:
        ts_str = row.get("timestamp")
        if not ts_str:
          self.skipped_empty += 1
          continue
        try:
          ts = parse_iso_datetime(ts_str)
        except ValueError:
          self.skipped_empty += 1
          continue
        # --since is inclusive ("at/after"), so the boundary row comes back.
        if ts <= cutoff:
          continue
        if self.skip_bots and _is_bot_row(row):
          self.skipped_bots += 1
          continue
        # Content-pattern filter: drop automated admin digest / connector posts
        # whose body matches well-known machine-generated patterns (MC IDs,
        # published-date headers, Message Center advisories).
        content_raw = (row.get("content") or "").strip()
        if _AUTOMATED_CONTENT_RE.search(content_raw):
          self.skipped_automated += 1
          continue
        item = _to_item(row, self.profile, team_id, team_name, ch_name)
        if item is None:
          self.skipped_empty += 1
          continue
        yield item

  def _fetch_team(
    self, team: tuple[str, str], cutoff: datetime,
  ) -> list[tuple[tuple[str, str, str], list[dict]]]:
    """Fetch every channel's messages for one team: 1 channels call + N."""
    team_id, team_name = team
    return [
      ((team_id, team_name, ch_name), self._messages(ch_id, team_id, cutoff))
      for ch_id, ch_name in self._channels(team_id)
    ]

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
      if not channel_id or channel_id in self.skip_channels:
        continue
      out.append((channel_id, (channel.get("displayName") or "").strip()))
    return out

  def _messages(self, channel_id: str, team_id: str, cutoff: datetime) -> list[dict]:
    pages = self.backfill_limit_pages if self.backfill_limit_pages is not None else self.limit_pages
    return self._run([
      "messages",
      "--channel", channel_id,
      "--team", team_id,
      "--since", cutoff.isoformat(),
      "--limit", str(pages),
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
      try:
        result = _run_capped(cmd, _CALL_TIMEOUT_SEC)
      except subprocess.TimeoutExpired:
        self.timed_out_calls += 1
        logger.warning(
          "owa-teams %s hung past %.0fs; retrying (attempt %d/%d, profile=%s, args=%s)",
          verb, _CALL_TIMEOUT_SEC, attempt + 1, attempts, self.profile,
          " ".join(args[1:]),
        )
        continue
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
      # Include the verb's own args: a per-channel 403 (rc=12, chatsvc
      # denying one conversation) is otherwise indistinguishable from a
      # profile-wide auth failure in the log, and you can't tell which
      # channel to go look at.
      logger.warning(
        "owa-teams %s failed (profile=%s rc=%d, args=%s): %s",
        verb, self.profile, result.returncode, " ".join(args[1:]),
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
