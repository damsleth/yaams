"""Teams ingest via the chatsvc (ic3) API instead of Microsoft Graph.

Used as the engine for owa-piggy profiles whose tenant gates Graph's
`/me/chats` behind a Conditional Access policy that requires
Intune-managed devices (e.g. SoftwareOne). Teams web itself talks to
`teams.microsoft.com/api/chatsvc/<region>/v1/...` with an
`ic3.teams.office.com`-audience access token, which most tenants leave
open. owa-piggy can mint such a token from its existing FOCI refresh
token via `--audience ic3` (no re-auth needed).

API shape (chatsvc, undocumented but stable):
  GET /api/chatsvc/<region>/v1/users/ME/conversations
      ?pageSize=N&view=msnp24Equivalent|supportsMessageProperties
  -> { conversations: [...], _metadata: { backwardLink, forwardLink, syncState } }

  GET /api/chatsvc/<region>/v1/users/ME/conversations/<id>/messages
      ?pageSize=N&view=...
  -> { messages: [...], _metadata: { backwardLink, syncState } }

`backwardLink` is the cursor for older items (backfill direction);
`forwardLink` is for newer items (incremental); `syncState` packs both.

This adapter is intentionally separate from the Graph-based one so the
two can coexist - most tenants keep working through Graph and only
problem tenants opt into chatsvc via per-profile engine config.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from yaams.ingest.base import Item, hash_id
from yaams.ingest.teams import (
  _BOT_LIKE_NAMES,
  MAX_TEAMS_CHARS,
  OwaPiggyTokenSource,
  clean_teams_body,
)
from yaams.time import ensure_utc

logger = logging.getLogger(__name__)

CHATSVC_HOST = "https://teams.microsoft.com"
DEFAULT_VIEW = "msnp24Equivalent|supportsMessageProperties"

# 1:1 chat IDs encode both participants' AAD OIDs as
#   19:<oid_a>_<oid_b>@unq.gbl.spaces
# so we can identify the "other party" without an extra /threads call.
_ONE_ON_ONE_CHAT_RE = re.compile(
  r"^19:([0-9a-f-]{36})_([0-9a-f-]{36})@unq\.gbl\.spaces$",
  re.IGNORECASE,
)

# chatsvc returns 7-digit fractional seconds, which datetime.fromisoformat
# can't handle pre-3.11. Trim to microseconds.
_FRACTIONAL_TS = re.compile(r"\.(\d{6})\d*")

# `from` is a chatsvc URL like
#   https://teams.microsoft.com/api/chatsvc/.../contacts/8:orgid:<oid>
# `8:orgid:<oid>` is a tenant user; `28:<botid>` and `48:<id>` are bots.
_FROM_MRI_RE = re.compile(r"/contacts/([0-9]+:[^/?#]+)")


def parse_chatsvc_time(value: str) -> datetime:
  trimmed = _FRACTIONAL_TS.sub(r".\1", value).replace("Z", "+00:00")
  return ensure_utc(datetime.fromisoformat(trimmed))


def is_system_chatsvc_message(message: dict) -> bool:
  mtype = (message.get("messagetype") or "").lower()
  return bool(mtype) and not mtype.startswith("text") and mtype not in {
    "richtext/html",
    "richtext",
  }


def sender_mri(message: dict) -> str:
  raw = message.get("from") or ""
  m = _FROM_MRI_RE.search(raw)
  return m.group(1) if m else ""


def is_bot_chatsvc_message(message: dict) -> bool:
  mri = sender_mri(message)
  # 28: is the Skype bot MRI namespace; 48: is app/orgapp; 8: is human.
  # 8:teamsvisitor: is a federated/guest user - still human.
  if mri.startswith("28:") or mri.startswith("48:"):
    return True
  display = (message.get("imdisplayname") or "").strip()
  if display and _BOT_LIKE_NAMES.match(display):
    return True
  return False


def _self_identity_from_token(token: str) -> tuple[str, str]:
  """Extract the user's chatsvc MRI and display name from a JWT.

  Returns `(mri, display_name)`. We decode the payload without
  verifying the signature - this is fine because we trust the token
  (we just minted it via owa-piggy) and only read non-sensitive claims.
  Falls back to `("", "")` on any parse failure; callers must tolerate
  empty values.
  """
  try:
    payload = token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    oid = claims.get("oid") or ""
    mri = f"8:orgid:{oid}" if oid else ""
    name = (claims.get("name") or claims.get("unique_name") or "").strip()
    return mri, name
  except (ValueError, IndexError, json.JSONDecodeError):
    return "", ""


def other_party_mri(chat_id: str, self_mri: str) -> str:
  """For 1:1 chats, return the other participant's MRI from the chat ID.

  Returns empty string for group chats / meetings / channels where the
  ID structure doesn't encode participants.
  """
  m = _ONE_ON_ONE_CHAT_RE.match(chat_id or "")
  if not m:
    return ""
  oids = (m.group(1).lower(), m.group(2).lower())
  self_oid = self_mri.removeprefix("8:orgid:").lower()
  for oid in oids:
    if oid != self_oid:
      return f"8:orgid:{oid}"
  return ""


def _chat_topic(chat: dict) -> str:
  return (chat.get("threadProperties") or {}).get("topic") or ""


def _chat_type(chat: dict) -> str:
  """Normalize chatsvc threadType into the Graph adapter's vocabulary.

  chatsvc's "chat" threadType is overloaded for both 1:1 and group
  chats; we disambiguate via the chat ID structure -
  `@unq.gbl.spaces` is always 1:1, anything else under "chat" is group.
  """
  tt = (chat.get("threadProperties") or {}).get("threadType") or ""
  tt_low = tt.lower()
  m = {
    "meeting": "meeting",
    "group": "group",
    "oneonone": "oneOnOne",
    "space": "channel",
    "channel": "channel",
  }
  if tt_low in m:
    return m[tt_low]
  if tt_low == "chat":
    chat_id = chat.get("id") or ""
    return "oneOnOne" if chat_id.endswith("@unq.gbl.spaces") else "group"
  return tt


def message_to_item(
  profile: str,
  chat: dict,
  message: dict,
  *,
  roster: dict[str, str],
  self_mri: str,
) -> Item | None:
  if message.get("deletetime") or message.get("deletionDate"):
    return None
  if is_system_chatsvc_message(message):
    return None
  raw = message.get("content") or ""
  body = clean_teams_body(raw)
  if not body:
    return None
  if len(body) > MAX_TEAMS_CHARS:
    body = body[:MAX_TEAMS_CHARS]

  ts_str = message.get("originalarrivaltime") or message.get("composetime")
  if not ts_str:
    return None
  timestamp = parse_chatsvc_time(ts_str)

  msg_sender_mri = sender_mri(message)
  sender_display = (message.get("imdisplayname") or "").strip() or msg_sender_mri or "unknown"
  chat_id = chat.get("id") or message.get("conversationid") or ""
  message_id = str(message.get("id"))
  source_id = f"{chat_id}:{message_id}"
  topic = _chat_topic(chat)
  ctype = _chat_type(chat)

  # Recipients = everyone in the roster except the sender. Self IS
  # included when someone else sent the message (matches Graph adapter:
  # see yaams/ingest/teams.py _resolve_recipients, which excludes the
  # sender only). chatsvc doesn't expose UPN/email at this layer so we
  # use display names; downstream entity linking copes either way.
  recipients = [
    name for mri, name in roster.items()
    if mri and mri != msg_sender_mri and name
  ]
  # For 1:1 chats, subject falls back to "the other person's name" -
  # never self, since that's not informative. Use self_mri to pick.
  subject = topic
  if not subject and ctype == "oneOnOne":
    others = [
      name for mri, name in roster.items()
      if mri and mri != self_mri and name
    ]
    subject = others[0] if others else ""

  return Item(
    id=hash_id(f"teams_{profile}", source_id),
    source=f"teams_{profile}",
    source_id=source_id,
    timestamp=timestamp,
    sender=sender_display,
    recipients=recipients,
    content=body,
    subject=subject,
    thread_id=chat_id,
    raw_metadata={
      "profile": profile,
      "engine": "chatsvc",
      "chat_type": ctype,
      "topic": topic,
      "messagetype": message.get("messagetype"),
      "sender_mri": msg_sender_mri,
    },
  )


class ChatsvcClient:
  def __init__(
    self,
    token_source: OwaPiggyTokenSource,
    timeout: float = 30.0,
    max_retries: int = 5,
  ):
    self.tokens = token_source
    self.timeout = timeout
    self.max_retries = max_retries

  def get(self, url: str) -> dict:
    """GET a chatsvc URL with retry/backoff on transient failures.

    Retries 429 (honoring Retry-After), 5xx, and network errors with
    exponential backoff. 4xx other than 429 (e.g. 404 on a rotated
    thread, 401 on a bad token) raise immediately - retrying can't help
    and the caller's per-chat guard skips them.
    """
    last_error: Exception | None = None
    for attempt in range(self.max_retries):
      req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {self.tokens.get_token()}"},
      )
      try:
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
          return json.loads(resp.read())
      except urllib.error.HTTPError as exc:
        if exc.code == 429:
          retry_after = exc.headers.get("Retry-After") if exc.headers else None
          time.sleep(int(retry_after) if retry_after and retry_after.isdigit() else 10)
          last_error = exc
          continue
        if exc.code in (500, 502, 503, 504):
          time.sleep(min(2 ** attempt, 30))
          last_error = exc
          continue
        raise
      except (urllib.error.URLError, TimeoutError) as exc:
        time.sleep(min(2 ** attempt, 30))
        last_error = exc
        continue
    if last_error:
      raise last_error
    raise RuntimeError(f"chatsvc request failed after {self.max_retries} retries: {url}")

  def paginate(self, url: str) -> Iterator[dict]:
    """Walk `backwardLink` until exhausted, yielding the list payload.

    chatsvc's `_metadata.backwardLink` is a fully-qualified URL with all
    query params baked in - no need to re-encode anything between hops.
    """
    while url:
      data = self.get(url)
      yield data
      url = (data.get("_metadata") or {}).get("backwardLink") or ""


@dataclass
class ChatsvcAdapter:
  profile: str
  client: ChatsvcClient
  region: str = "emea"
  skip_bots: bool = True
  page_size: int = 50
  skipped_bots: int = field(default=0, init=False)
  skipped_system: int = field(default=0, init=False)
  skipped_empty: int = field(default=0, init=False)
  _self_mri: str = field(default="", init=False)
  _self_name: str = field(default="", init=False)

  def _ensure_self_identity(self) -> tuple[str, str]:
    if not self._self_mri:
      self._self_mri, self._self_name = _self_identity_from_token(
        self.client.tokens.get_token()
      )
    return self._self_mri, self._self_name

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_bots = 0
    self.skipped_system = 0
    self.skipped_empty = 0
    cutoff = ensure_utc(since)
    self_mri, self_name = self._ensure_self_identity()

    for chat in self._iter_conversations():
      # Skip Teams' internal "streamof*" conversations (notifications,
      # mentions roll-up, call logs). These are aggregator pointers into
      # real chats, not chats themselves - ingesting them duplicates the
      # underlying messages and bloats the entity graph with self-mentions.
      tt = (chat.get("threadProperties") or {}).get("threadType", "").lower()
      # streamof* are aggregator pointers (see below); `space` (team-channel
      # spaces) and `topic` (channel post-reply threads) are channel threads
      # that leak into ME/conversations on some tenants (e.g. SoftwareOne).
      # chatsvc's user-scoped /messages 404s on both, and they're already
      # ingested via the teams_channels source, so skip them.
      if tt.startswith("streamof") or tt in ("space", "topic"):
        continue
      last_msg = chat.get("lastMessage") or {}
      last_ts_raw = last_msg.get("originalarrivaltime") or last_msg.get("composetime")
      if last_ts_raw:
        try:
          if parse_chatsvc_time(last_ts_raw) < cutoff:
            continue
        except ValueError:
          pass  # bad timestamp - fall through and iterate, the per-message check will filter
      # One unreachable chat must not sink the whole source. A chat can 404
      # (deleted/rotated thread, a `space` type we failed to filter) or 5xx
      # mid-run; log it and move on. Items already yielded for this chat are
      # idempotent by hash id, so a partial fetch is safe.
      try:
        yield from self._iter_chat_messages(chat, cutoff, self_mri, self_name)
      except urllib.error.HTTPError as exc:
        logger.warning(
          "chatsvc[%s]: HTTP %d on chat %s - skipping", self.profile, exc.code,
          chat.get("id", "?"),
        )
      except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning(
          "chatsvc[%s]: network error on chat %s: %s - skipping", self.profile,
          chat.get("id", "?"), exc,
        )

  def _iter_conversations(self) -> Iterator[dict]:
    url = (
      f"{CHATSVC_HOST}/api/chatsvc/{self.region}/v1/users/ME/conversations"
      f"?pageSize={self.page_size}&view={urllib.parse.quote(DEFAULT_VIEW)}"
    )
    for page in self.client.paginate(url):
      for c in page.get("conversations", []):
        yield c

  def _iter_chat_messages(
    self,
    chat: dict,
    cutoff: datetime,
    self_mri: str,
    self_name: str,
  ) -> Iterator[Item]:
    """Two-pass over a single chat's messages.

    Pass 1: walk all in-window messages into `buffered`, building a
            mri -> display-name roster from each sender. For 1:1 chats
            seed the roster with the other party's MRI extracted from
            the chat ID, so recipients still populate even if the
            other party hasn't spoken in the window.

    Pass 2: yield Items in newest-first order with full recipients.

    The buffer is bounded by `since` (yaams' watermark), which in
    practice keeps it well under a few hundred messages per chat.
    """
    chat_id = chat.get("id")
    if not chat_id:
      return
    cid = urllib.parse.quote(chat_id, safe="")
    url = (
      f"{CHATSVC_HOST}/api/chatsvc/{self.region}/v1/users/ME/conversations/{cid}/messages"
      f"?pageSize={self.page_size}&view={urllib.parse.quote(DEFAULT_VIEW)}"
    )

    buffered: list[dict] = []
    roster: dict[str, str] = {}
    # Seed self into the roster so `recipients = roster - sender` resolves
    # to [self_name] when someone else sends to us, even if we never spoke
    # in the visible window.
    if self_mri:
      roster[self_mri] = self_name
    # Seed 1:1 roster from chat ID so the other party is known even if
    # they haven't sent a message in the visible window (e.g. user is
    # always the speaker in a short window).
    other = other_party_mri(chat_id, self_mri)
    if other:
      roster.setdefault(other, "")  # name fills in if/when they speak

    stop_walking = False
    for page in self.client.paginate(url):
      if stop_walking:
        break
      for message in page.get("messages", []):
        ts_str = message.get("originalarrivaltime") or message.get("composetime")
        if not ts_str:
          continue
        try:
          ts = parse_chatsvc_time(ts_str)
        except ValueError:
          continue
        if ts <= cutoff:
          # chatsvc returns newest-first; once we hit the cutoff we're done
          # with the entire chat.
          stop_walking = True
          break
        mri = sender_mri(message)
        display = (message.get("imdisplayname") or "").strip()
        if mri and display:
          # Last write wins; display names are stable so the choice
          # doesn't matter, but this keeps the roster fresh.
          roster[mri] = display
        buffered.append(message)

    for message in buffered:
      if is_system_chatsvc_message(message):
        self.skipped_system += 1
        continue
      if self.skip_bots and is_bot_chatsvc_message(message):
        self.skipped_bots += 1
        continue
      item = message_to_item(
        self.profile, chat, message,
        roster=roster, self_mri=self_mri,
      )
      if item is None:
        self.skipped_empty += 1
        continue
      yield item
