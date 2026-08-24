"""Tests for the chatsvc (ic3) Teams adapter.

This module had no test file at all, despite carrying the tenant-specific
thread filtering that has already needed one bug fix (348e358). The seam is
`ChatsvcClient.paginate`, so a fake client driving `extract()` covers the
conversation filter, the roster and the item mapping without any network.
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from yaams.ingest.teams_chatsvc import (
  ChatsvcAdapter,
  is_bot_chatsvc_message,
  message_to_item,
  other_party_mri,
  parse_chatsvc_time,
)

SELF_OID = "11111111-1111-1111-1111-111111111111"
PEER_OID = "22222222-2222-2222-2222-222222222222"
SELF_MRI = f"8:orgid:{SELF_OID}"
PEER_MRI = f"8:orgid:{PEER_OID}"
BOT_MRI = "28:some-bot-guid"


def _token(oid: str = SELF_OID, name: str = "Me Myself") -> str:
  claims = base64.urlsafe_b64encode(
    json.dumps({"oid": oid, "name": name}).encode()
  ).decode().rstrip("=")
  return f"header.{claims}.signature"


class _FakeTokens:
  def __init__(self, token: str) -> None:
    self._token = token

  def get_token(self) -> str:
    return self._token


class _FakeClient:
  """Serves canned pages keyed by a substring of the requested URL."""

  def __init__(self, conversations: list[dict], messages: dict[str, list[dict]]) -> None:
    self.tokens = _FakeTokens(_token())
    self._conversations = conversations
    self._messages = messages
    self.requested: list[str] = []

  def paginate(self, url: str):
    self.requested.append(url)
    if "/conversations?" in url:
      yield {"conversations": self._conversations}
      return
    for chat_id, msgs in self._messages.items():
      import urllib.parse

      if urllib.parse.quote(chat_id, safe="") in url:
        yield {"messages": msgs}
        return
    raise AssertionError(f"unexpected URL: {url}")


def _msg(mri: str, display: str, text: str, *, mid: str, when: str = "2026-08-01T10:00:00Z") -> dict:
  return {
    "id": mid,
    "from": f"https://notifications.skype.net/v1/users/ME/contacts/{mri}",
    "imdisplayname": display,
    "content": text,
    "messagetype": "RichText/Html",
    "originalarrivaltime": when,
  }


def _chat(chat_id: str, thread_type: str = "chat", topic: str = "") -> dict:
  props: dict = {"threadType": thread_type}
  if topic:
    props["topic"] = topic
  return {"id": chat_id, "threadProperties": props}


def _adapter(conversations, messages) -> ChatsvcAdapter:
  return ChatsvcAdapter(profile="test", client=_FakeClient(conversations, messages))


# --- conversation filtering (regression for 348e358) ------------------------


@pytest.mark.parametrize("thread_type", ["space", "topic", "SPACE", "Topic"])
def test_channel_threads_are_skipped(thread_type):
  """`space` and `topic` are channel threads that leak into ME/conversations.

  chatsvc's user-scoped /messages 404s on both, and teams_channels already
  ingests them. 348e358 added `topic` but tested only the teams_channels half.
  """
  chat_id = "19:channelthread@thread.tacv2"
  adapter = _adapter(
    [_chat(chat_id, thread_type)],
    {chat_id: [_msg(PEER_MRI, "Peer", "should never be read", mid="1")]},
  )
  assert list(adapter.extract(datetime(2026, 7, 1, tzinfo=UTC))) == []
  # The messages endpoint must never even be requested.
  assert not any("/messages" in u for u in adapter.client.requested)


def test_streamof_aggregators_are_skipped():
  chat_id = "19:streamofmentions@thread.skype"
  adapter = _adapter(
    [_chat(chat_id, "streamofmentions")],
    {chat_id: [_msg(PEER_MRI, "Peer", "roll-up noise", mid="1")]},
  )
  assert list(adapter.extract(datetime(2026, 7, 1, tzinfo=UTC))) == []


def test_ordinary_chat_is_ingested():
  chat_id = f"19:{SELF_OID}_{PEER_OID}@unq.gbl.spaces"
  adapter = _adapter(
    [_chat(chat_id, "chat")],
    {chat_id: [_msg(PEER_MRI, "Peer Person", "hello there", mid="1")]},
  )
  items = list(adapter.extract(datetime(2026, 7, 1, tzinfo=UTC)))
  assert [i.content for i in items] == ["hello there"]
  assert items[0].sender == "Peer Person"


# --- bots must not leak into recipients -------------------------------------


def test_bot_sender_is_kept_out_of_the_roster():
  """Regression: the roster was built in pass 1, before the bot filter in
  pass 2, so a bot posting in a group chat was dropped as a sender but still
  appeared in `recipients` of every human message in that chat."""
  chat_id = "19:groupchat@thread.v2"
  adapter = _adapter(
    [_chat(chat_id, "chat", topic="Team room")],
    {
      chat_id: [
        _msg(BOT_MRI, "Build Bot", "pipeline green", mid="1"),
        _msg(PEER_MRI, "Peer Person", "thanks bot", mid="2"),
      ]
    },
  )
  items = list(adapter.extract(datetime(2026, 7, 1, tzinfo=UTC)))

  assert [i.sender for i in items] == ["Peer Person"], "bot must not be ingested"
  assert adapter.skipped_bots == 1
  recipients = items[0].recipients
  assert "Build Bot" not in recipients, "bot leaked into recipients"
  assert "Me Myself" in recipients


def test_human_participants_still_populate_recipients():
  chat_id = "19:groupchat@thread.v2"
  third = "8:orgid:33333333-3333-3333-3333-333333333333"
  adapter = _adapter(
    [_chat(chat_id, "chat", topic="Team room")],
    {
      chat_id: [
        _msg(third, "Third Person", "morning", mid="1"),
        _msg(PEER_MRI, "Peer Person", "morning all", mid="2"),
      ]
    },
  )
  items = list(adapter.extract(datetime(2026, 7, 1, tzinfo=UTC)))
  by_sender = {i.sender: i for i in items}
  assert set(by_sender["Peer Person"].recipients) == {"Third Person", "Me Myself"}


# --- pure helpers -----------------------------------------------------------


def test_other_party_mri_picks_the_peer_in_a_one_on_one():
  chat_id = f"19:{SELF_OID}_{PEER_OID}@unq.gbl.spaces"
  assert other_party_mri(chat_id, SELF_MRI) == PEER_MRI


def test_other_party_mri_is_empty_for_group_chats():
  assert other_party_mri("19:groupchat@thread.v2", SELF_MRI) == ""


@pytest.mark.parametrize(
  "mri,display,expected",
  [
    (BOT_MRI, "Build Bot", True),
    ("48:orgapp-guid", "Approvals", True),
    (PEER_MRI, "Peer Person", False),
    ("8:teamsvisitor:guest", "Guest User", False),
  ],
)
def test_is_bot_chatsvc_message(mri, display, expected):
  assert is_bot_chatsvc_message(_msg(mri, display, "x", mid="1")) is expected


def test_parse_chatsvc_time_normalizes_to_utc():
  assert parse_chatsvc_time("2026-08-01T10:00:00Z") == datetime(
    2026, 8, 1, 10, 0, tzinfo=UTC
  )
  # chatsvc emits sub-millisecond precision that fromisoformat rejects on 3.11.
  assert parse_chatsvc_time("2026-08-01T10:00:00.1234567Z").tzinfo is UTC


def test_message_to_item_drops_deleted_messages():
  chat = _chat("19:x@thread.v2", "chat")
  msg = _msg(PEER_MRI, "Peer", "gone", mid="1") | {"deletetime": "123"}
  assert message_to_item("test", chat, msg, roster={}, self_mri=SELF_MRI) is None
