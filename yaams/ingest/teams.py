from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable, Iterator

from yaams.ingest.base import Item, hash_id
from yaams.ingest.email_mbox import strip_html
from yaams.time import ensure_utc


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_REFRESH_MARGIN_SEC = 300
MAX_TEAMS_CHARS = 20_000


_BOT_LIKE_NAMES = re.compile(
  r"^(approvals?|workflows?|forms|polly|planner|loop|"
  r"viva|insights?|company communicator|communications?|"
  r"webex|zoom|news|announce|notify|notifications?|"
  r"whobot|who|tasks?|reminder|microsoft teams)$",
  re.IGNORECASE,
)

_TEAMS_MENTION_RE = re.compile(r"<at[^>]*>(.*?)</at>", re.DOTALL | re.IGNORECASE)
_TEAMS_ATTACHMENT_RE = re.compile(
  r"<attachment[^>]*></attachment>|<attachment[^>]*/>",
  re.IGNORECASE,
)
_WHITESPACE_RUN = re.compile(r"[ \t]+")


def parse_graph_datetime(value: str) -> datetime:
  trimmed = value.replace("Z", "+00:00")
  return ensure_utc(datetime.fromisoformat(trimmed))


def is_bot_message(message: dict) -> bool:
  sender = message.get("from") or {}
  if sender.get("application"):
    return True
  user = sender.get("user") or {}
  identity_type = (user.get("userIdentityType") or "").lower()
  if identity_type in {"applicationinstance", "bot", "anonymous"}:
    return True
  display = (user.get("displayName") or "").strip()
  if display and _BOT_LIKE_NAMES.match(display):
    return True
  return False


def is_system_message(message: dict) -> bool:
  message_type = (message.get("messageType") or "").lower()
  if message_type and message_type != "message":
    return True
  if message.get("eventDetail"):
    return True
  return False


def clean_teams_body(html: str) -> str:
  if not html:
    return ""
  text = _TEAMS_MENTION_RE.sub(r"\1", html)
  text = _TEAMS_ATTACHMENT_RE.sub("", text)
  text = strip_html(text)
  lines = [_WHITESPACE_RUN.sub(" ", line).strip() for line in text.splitlines()]
  return "\n".join(line for line in lines if line)


def _resolve_sender(message: dict, members_by_id: dict[str, dict]) -> str:
  sender = message.get("from") or {}
  user = sender.get("user") or {}
  user_id = user.get("id")
  if user_id and user_id in members_by_id:
    member = members_by_id[user_id]
    email = member.get("email") or member.get("userPrincipalName")
    if email:
      return email
  display = user.get("displayName") or sender.get("application", {}).get("displayName")
  return (display or "unknown").strip()


def _resolve_recipients(message: dict, members: list[dict]) -> list[str]:
  sender = message.get("from") or {}
  user = sender.get("user") or {}
  sender_id = user.get("id")
  recipients = []
  for member in members:
    if member.get("userId") == sender_id:
      continue
    email = member.get("email") or member.get("userPrincipalName")
    if email:
      recipients.append(email)
  return recipients


def message_to_item(
  profile: str,
  chat: dict,
  message: dict,
  members: list[dict],
) -> Item | None:
  if message.get("deletedDateTime"):
    return None
  if is_system_message(message):
    return None
  body_field = message.get("body") or {}
  raw_content = body_field.get("content") or ""
  body = clean_teams_body(raw_content)
  if not body:
    return None
  if len(body) > MAX_TEAMS_CHARS:
    body = body[:MAX_TEAMS_CHARS]

  created = message.get("createdDateTime")
  if not created:
    return None
  timestamp = parse_graph_datetime(created)

  members_by_id = {m["userId"]: m for m in members if m.get("userId")}
  sender = _resolve_sender(message, members_by_id)
  recipients = _resolve_recipients(message, members)
  chat_id = chat.get("id") or message.get("chatId") or ""
  message_id = str(message.get("id"))
  source_id = f"{chat_id}:{message_id}"
  topic = chat.get("topic") or ""
  chat_type = chat.get("chatType") or "oneOnOne"
  subject = topic or (recipients[0] if chat_type == "oneOnOne" and recipients else "")

  return Item(
    id=hash_id(f"teams_{profile}", source_id),
    source=f"teams_{profile}",
    source_id=source_id,
    timestamp=timestamp,
    sender=sender,
    recipients=recipients,
    content=body,
    subject=subject,
    thread_id=chat_id,
    raw_metadata={
      "profile": profile,
      "chat_type": chat_type,
      "topic": topic,
      "importance": message.get("importance"),
      "reply_to_id": message.get("replyToId"),
      "attachment_count": len(message.get("attachments") or []),
      "mention_count": len(message.get("mentions") or []),
    },
  )


class OwaPiggyTokenSource:
  def __init__(self, profile: str, command: list[str] | None = None):
    self.profile = profile
    self._command = command or ["owa-piggy", "--profile", profile]
    self._token: str | None = None
    self._expires_at: float = 0.0

  def get_token(self) -> str:
    now = time.time()
    if self._token and self._expires_at - TOKEN_REFRESH_MARGIN_SEC > now:
      return self._token
    result = subprocess.run(self._command, capture_output=True, text=True, check=True)
    token = result.stdout.strip()
    if not token:
      raise RuntimeError(f"owa-piggy returned empty token for profile {self.profile}")
    self._token = token
    self._expires_at = self._jwt_exp(token)
    return token

  @staticmethod
  def _jwt_exp(token: str) -> float:
    parts = token.split(".")
    if len(parts) != 3:
      return time.time() + 3600
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
      decoded = json.loads(base64.urlsafe_b64decode(payload))
      return float(decoded.get("exp", time.time() + 3600))
    except (ValueError, json.JSONDecodeError):
      return time.time() + 3600


class GraphClient:
  def __init__(
    self,
    token_source: OwaPiggyTokenSource,
    timeout: float = 30.0,
    max_retries: int = 5,
  ):
    import httpx

    self.tokens = token_source
    self.client = httpx.Client(timeout=timeout)
    self.max_retries = max_retries

  def close(self) -> None:
    self.client.close()

  def get(self, url: str, params: dict | None = None) -> dict:
    if url.startswith("/"):
      url = f"{GRAPH_BASE}{url}"
    last_error: Exception | None = None
    for attempt in range(self.max_retries):
      headers = {"Authorization": f"Bearer {self.tokens.get_token()}"}
      try:
        response = self.client.get(url, headers=headers, params=params)
      except Exception as exc:
        last_error = exc
        time.sleep(min(2 ** attempt, 30))
        continue
      if response.status_code == 200:
        return response.json()
      if response.status_code == 429:
        retry_after = int(response.headers.get("Retry-After", "10"))
        time.sleep(retry_after)
        continue
      if response.status_code in (500, 502, 503, 504):
        time.sleep(min(2 ** attempt, 30))
        continue
      response.raise_for_status()
    if last_error:
      raise last_error
    raise RuntimeError(f"Graph request failed after {self.max_retries} retries: {url}")

  def paginate(
    self,
    url: str,
    params: dict | None = None,
  ) -> Iterator[dict]:
    next_url: str | None = url
    next_params = params
    while next_url:
      data = self.get(next_url, params=next_params)
      next_params = None
      for item in data.get("value", []):
        yield item
      next_url = data.get("@odata.nextLink")


@dataclass
class TeamsAdapter:
  profile: str
  graph_client: GraphClient
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

    for chat in self._iter_chats():
      last_updated = chat.get("lastUpdatedDateTime")
      if last_updated:
        last_updated_ts = parse_graph_datetime(last_updated)
        if last_updated_ts < cutoff:
          continue
      yield from self._iter_chat_messages(chat, cutoff)

  def _iter_chats(self) -> Iterator[dict]:
    yield from self.graph_client.paginate(
      "/me/chats",
      params={
        "$top": str(self.page_size),
        "$select": "id,topic,chatType,lastUpdatedDateTime,createdDateTime",
      },
    )

  def _iter_chat_messages(self, chat: dict, cutoff: datetime) -> Iterator[Item]:
    chat_id = chat.get("id")
    if not chat_id:
      return
    members = self._fetch_members(chat_id)
    params = {"$top": str(self.page_size)}
    for message in self.graph_client.paginate(
      f"/me/chats/{chat_id}/messages",
      params=params,
    ):
      created = message.get("createdDateTime")
      if not created:
        continue
      ts = parse_graph_datetime(created)
      if ts <= cutoff:
        return
      if is_system_message(message):
        self.skipped_system += 1
        continue
      if self.skip_bots and is_bot_message(message):
        self.skipped_bots += 1
        continue
      item = message_to_item(self.profile, chat, message, members)
      if item is None:
        self.skipped_empty += 1
        continue
      yield item

  def _fetch_members(self, chat_id: str) -> list[dict]:
    members = []
    try:
      for member in self.graph_client.paginate(f"/me/chats/{chat_id}/members"):
        members.append(member)
    except Exception:
      return []
    return members
