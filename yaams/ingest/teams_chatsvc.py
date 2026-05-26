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

import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from yaams.ingest.base import Item, hash_id
from yaams.ingest.teams import (
  MAX_TEAMS_CHARS,
  OwaPiggyTokenSource,
  _BOT_LIKE_NAMES,
  clean_teams_body,
)
from yaams.time import ensure_utc

CHATSVC_HOST = "https://teams.microsoft.com"
DEFAULT_VIEW = "msnp24Equivalent|supportsMessageProperties"

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


def _chat_topic(chat: dict) -> str:
  return (chat.get("threadProperties") or {}).get("topic") or ""


def _chat_type(chat: dict) -> str:
  tt = (chat.get("threadProperties") or {}).get("threadType") or ""
  # Normalize chatsvc threadType to the same vocabulary the Graph adapter
  # emits so downstream consumers don't need to branch: "Meeting"->"meeting",
  # "Group"->"group", "OneOnOne"->"oneOnOne", "Space"/"Channel"->"channel".
  m = {
    "meeting": "meeting",
    "group": "group",
    "oneonone": "oneOnOne",
    "chat": "oneOnOne",
    "space": "channel",
    "channel": "channel",
  }
  return m.get(tt.lower(), tt)


def message_to_item(profile: str, chat: dict, message: dict) -> Item | None:
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

  sender_display = (message.get("imdisplayname") or "").strip() or sender_mri(message)
  chat_id = chat.get("id") or message.get("conversationid") or ""
  message_id = str(message.get("id"))
  source_id = f"{chat_id}:{message_id}"
  topic = _chat_topic(chat)
  ctype = _chat_type(chat)

  return Item(
    id=hash_id(f"teams_{profile}", source_id),
    source=f"teams_{profile}",
    source_id=source_id,
    timestamp=timestamp,
    sender=sender_display or "unknown",
    recipients=[],
    content=body,
    subject=topic,
    thread_id=chat_id,
    raw_metadata={
      "profile": profile,
      "engine": "chatsvc",
      "chat_type": ctype,
      "topic": topic,
      "messagetype": message.get("messagetype"),
      "sender_mri": sender_mri(message),
    },
  )


class ChatsvcClient:
  def __init__(self, token_source: OwaPiggyTokenSource, timeout: float = 30.0):
    self.tokens = token_source
    self.timeout = timeout

  def get(self, url: str) -> dict:
    req = urllib.request.Request(
      url,
      headers={"Authorization": f"Bearer {self.tokens.get_token()}"},
    )
    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
      import json
      return json.loads(resp.read())

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

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_bots = 0
    self.skipped_system = 0
    self.skipped_empty = 0
    cutoff = ensure_utc(since)

    for chat in self._iter_conversations():
      last_msg = chat.get("lastMessage") or {}
      last_ts_raw = last_msg.get("originalarrivaltime") or last_msg.get("composetime")
      if last_ts_raw:
        try:
          if parse_chatsvc_time(last_ts_raw) < cutoff:
            continue
        except ValueError:
          pass  # bad timestamp - fall through and iterate, the per-message check will filter
      yield from self._iter_chat_messages(chat, cutoff)

  def _iter_conversations(self) -> Iterator[dict]:
    url = (
      f"{CHATSVC_HOST}/api/chatsvc/{self.region}/v1/users/ME/conversations"
      f"?pageSize={self.page_size}&view={urllib.parse.quote(DEFAULT_VIEW)}"
    )
    for page in self.client.paginate(url):
      for c in page.get("conversations", []):
        yield c

  def _iter_chat_messages(self, chat: dict, cutoff: datetime) -> Iterator[Item]:
    chat_id = chat.get("id")
    if not chat_id:
      return
    cid = urllib.parse.quote(chat_id, safe="")
    url = (
      f"{CHATSVC_HOST}/api/chatsvc/{self.region}/v1/users/ME/conversations/{cid}/messages"
      f"?pageSize={self.page_size}&view={urllib.parse.quote(DEFAULT_VIEW)}"
    )
    for page in self.client.paginate(url):
      for message in page.get("messages", []):
        ts_str = message.get("originalarrivaltime") or message.get("composetime")
        if not ts_str:
          continue
        try:
          ts = parse_chatsvc_time(ts_str)
        except ValueError:
          continue
        if ts <= cutoff:
          # chatsvc messages are returned newest-first; once we hit the
          # cutoff we can stop walking this conversation entirely.
          return
        if is_system_chatsvc_message(message):
          self.skipped_system += 1
          continue
        if self.skip_bots and is_bot_chatsvc_message(message):
          self.skipped_bots += 1
          continue
        item = message_to_item(self.profile, chat, message)
        if item is None:
          self.skipped_empty += 1
          continue
        yield item
