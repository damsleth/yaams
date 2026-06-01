from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime

from yaams.ingest.teams import (
  OwaPiggyTokenSource,
  TeamsAdapter,
  clean_teams_body,
  is_bot_message,
  is_system_message,
  message_to_item,
  parse_graph_datetime,
)


def _make_user_message(
  message_id: str = "1234567890",
  chat_id: str = "19:abc@unq.gbl.spaces",
  body_html: str = "<p>Hei, jobber du i kveld?</p>",
  display_name: str = "Alice",
  user_id: str = "user-1",
  created: str = "2026-04-15T10:00:00.000Z",
) -> dict:
  return {
    "id": message_id,
    "chatId": chat_id,
    "createdDateTime": created,
    "messageType": "message",
    "deletedDateTime": None,
    "from": {
      "application": None,
      "user": {
        "id": user_id,
        "displayName": display_name,
        "userIdentityType": "aadUser",
      },
    },
    "body": {"contentType": "html", "content": body_html},
    "subject": None,
    "attachments": [],
    "mentions": [],
  }


def _make_chat(chat_id: str = "19:abc@unq.gbl.spaces", chat_type: str = "oneOnOne", topic: str | None = None) -> dict:
  return {
    "id": chat_id,
    "topic": topic,
    "chatType": chat_type,
    "lastUpdatedDateTime": "2026-04-29T12:00:00.000Z",
  }


def _make_members() -> list[dict]:
  return [
    {"userId": "user-1", "email": "alice@example.test", "displayName": "Alice"},
    {"userId": "user-2", "email": "user@example.test", "displayName": "Kim"},
  ]


def test_clean_teams_body_strips_mention_wrapper_keeps_name():
  html = '<p>Hi <at id="0">Bob Smith</at>, take a look.</p>'
  out = clean_teams_body(html)
  assert "Bob Smith" in out
  assert "<at" not in out


def test_clean_teams_body_drops_attachment_placeholders():
  html = '<attachment id="abc"></attachment><p>real text</p>'
  out = clean_teams_body(html)
  assert "real text" in out
  assert "attachment" not in out.lower()


def test_clean_teams_body_collapses_whitespace_and_blanks():
  html = "<p>line one</p>\n\n\n<p>line   two</p>\n"
  out = clean_teams_body(html)
  assert out.split("\n") == ["line one", "line two"]


def test_is_bot_message_detects_application_sender():
  msg = _make_user_message()
  msg["from"]["application"] = {"displayName": "Approvals"}
  assert is_bot_message(msg)


def test_is_bot_message_detects_known_bot_display_name():
  msg = _make_user_message(display_name="Approvals")
  assert is_bot_message(msg)


def test_is_bot_message_passes_real_user():
  msg = _make_user_message(display_name="Alice Smith")
  assert not is_bot_message(msg)


def test_is_system_message_detects_event_messages():
  msg = _make_user_message()
  msg["messageType"] = "systemEventMessage"
  assert is_system_message(msg)


def test_is_system_message_detects_event_detail():
  msg = _make_user_message()
  msg["eventDetail"] = {"@odata.type": "#microsoft.graph.memberAddedEventMessageDetail"}
  assert is_system_message(msg)


def test_message_to_item_resolves_email_from_members():
  chat = _make_chat()
  msg = _make_user_message(user_id="user-1")
  item = message_to_item("work", chat, msg, _make_members())
  assert item is not None
  assert item.sender == "alice@example.test"
  assert item.recipients == ["user@example.test"]
  assert "jobber du" in item.content
  assert item.thread_id == chat["id"]
  assert item.source == "teams_work"
  assert item.raw_metadata["profile"] == "work"
  assert item.raw_metadata["chat_type"] == "oneOnOne"


def test_message_to_item_falls_back_to_display_name_when_no_member_email():
  chat = _make_chat()
  msg = _make_user_message(user_id="ghost-id", display_name="External Person")
  item = message_to_item("work", chat, msg, _make_members())
  assert item is not None
  assert item.sender == "External Person"


def test_message_to_item_uses_topic_for_group_chat_subject():
  chat = _make_chat(chat_type="group", topic="Ops Group")
  msg = _make_user_message()
  item = message_to_item("volunteer", chat, msg, _make_members())
  assert item is not None
  assert item.subject == "Ops Group"


def test_message_to_item_skips_deleted_messages():
  chat = _make_chat()
  msg = _make_user_message()
  msg["deletedDateTime"] = "2026-04-15T11:00:00.000Z"
  item = message_to_item("work", chat, msg, _make_members())
  assert item is None


def test_message_to_item_skips_empty_body():
  chat = _make_chat()
  msg = _make_user_message(body_html="")
  item = message_to_item("work", chat, msg, _make_members())
  assert item is None


def test_message_to_item_id_is_stable_across_runs():
  chat = _make_chat()
  msg = _make_user_message()
  members = _make_members()
  a = message_to_item("work", chat, msg, members)
  b = message_to_item("work", chat, msg, members)
  assert a is not None and b is not None
  assert a.id == b.id


def test_owa_piggy_token_source_caches_token(tmp_path, monkeypatch):
  fake = tmp_path / "fake-owa-piggy"
  payload = {"exp": int(time.time()) + 3600}
  payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
  fake_token = f"hdr.{payload_b64}.sig"
  fake.write_text(f"#!/bin/sh\necho {fake_token}\n")
  fake.chmod(0o755)

  src = OwaPiggyTokenSource("any", command=[str(fake)])
  first = src.get_token()
  second = src.get_token()
  assert first == fake_token
  assert second == fake_token


def test_parse_graph_datetime_handles_z_suffix():
  ts = parse_graph_datetime("2026-04-15T10:00:00.000Z")
  assert ts == datetime(2026, 4, 15, 10, 0, tzinfo=UTC)


class _FakeGraphClient:
  def __init__(self, chats: list[dict], messages_by_chat: dict[str, list[dict]], members_by_chat: dict[str, list[dict]]):
    self.chats = chats
    self.messages_by_chat = messages_by_chat
    self.members_by_chat = members_by_chat

  def paginate(self, url: str, params: dict | None = None):
    if url == "/me/chats":
      yield from self.chats
      return
    if url.startswith("/me/chats/") and url.endswith("/messages"):
      chat_id = url[len("/me/chats/"):-len("/messages")]
      yield from self.messages_by_chat.get(chat_id, [])
      return
    if url.startswith("/me/chats/") and url.endswith("/members"):
      chat_id = url[len("/me/chats/"):-len("/members")]
      yield from self.members_by_chat.get(chat_id, [])
      return
    return


def test_teams_adapter_yields_user_messages_and_skips_bots():
  chat = _make_chat()
  user_msg = _make_user_message(message_id="m-user")
  bot_msg = _make_user_message(message_id="m-bot")
  bot_msg["from"]["application"] = {"displayName": "Approvals"}
  bot_msg["from"]["user"] = None
  system_msg = _make_user_message(message_id="m-sys")
  system_msg["messageType"] = "systemEventMessage"

  fake = _FakeGraphClient(
    chats=[chat],
    messages_by_chat={chat["id"]: [user_msg, bot_msg, system_msg]},
    members_by_chat={chat["id"]: _make_members()},
  )

  adapter = TeamsAdapter(profile="work", graph_client=fake)
  items = list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC)))

  assert len(items) == 1
  assert items[0].source_id.endswith(":m-user")
  assert adapter.skipped_bots == 1
  assert adapter.skipped_system == 1


def test_teams_adapter_stops_walking_chat_when_messages_predate_cutoff():
  chat = _make_chat()
  recent = _make_user_message(message_id="recent", created="2026-04-25T10:00:00Z")
  old = _make_user_message(message_id="old", created="2024-01-01T10:00:00Z")

  fake = _FakeGraphClient(
    chats=[chat],
    messages_by_chat={chat["id"]: [recent, old]},
    members_by_chat={chat["id"]: _make_members()},
  )

  adapter = TeamsAdapter(profile="work", graph_client=fake)
  items = list(adapter.extract(datetime(2026, 4, 1, tzinfo=UTC)))

  assert len(items) == 1
  assert items[0].source_id.endswith(":recent")


class _OrderedFakeGraphClient(_FakeGraphClient):
  """Adds the get()-based ordered chat listing the real client exposes.

  Returns chats newest-first across two pages and records which chats had
  their messages fetched, so a test can prove the early-break stopped paging.
  """

  def __init__(self, pages, messages_by_chat, members_by_chat):
    super().__init__([], messages_by_chat, members_by_chat)
    self._pages = pages
    self.fetched_message_chats: list[str] = []

  def get(self, url: str, params: dict | None = None):
    if url == "/me/chats":
      assert params and "lastMessagePreview/createdDateTime desc" in params.get("$orderby", "")
      return self._pages[0]
    return self._pages[1]  # the @odata.nextLink page

  def paginate(self, url: str, params: dict | None = None):
    if url.startswith("/me/chats/") and url.endswith("/messages"):
      chat_id = url[len("/me/chats/"):-len("/messages")]
      self.fetched_message_chats.append(chat_id)
    yield from super().paginate(url, params=params)


def test_teams_adapter_breaks_early_on_ordered_chats():
  def chat_with_preview(chat_id, last_msg_created):
    return {**_make_chat(chat_id=chat_id),
            "lastMessagePreview": {"createdDateTime": last_msg_created}}

  # Page 1: one fresh chat, then a stale one (newest-message-first order).
  page1 = {
    "value": [
      chat_with_preview("fresh", "2026-04-25T10:00:00Z"),
      chat_with_preview("stale", "2024-01-01T10:00:00Z"),
    ],
    "@odata.nextLink": "/me/chats?$skiptoken=2",
  }
  # Page 2 would be reached only if the break failed.
  page2 = {"value": [chat_with_preview("older", "2023-01-01T10:00:00Z")]}

  fresh_msg = _make_user_message(message_id="f1", chat_id="fresh", created="2026-04-25T10:00:00Z")
  fake = _OrderedFakeGraphClient(
    pages=[page1, page2],
    messages_by_chat={"fresh": [fresh_msg]},
    members_by_chat={"fresh": _make_members()},
  )

  adapter = TeamsAdapter(profile="work", graph_client=fake)
  items = list(adapter.extract(datetime(2026, 4, 1, tzinfo=UTC)))

  assert adapter._chats_ordered is True
  assert len(items) == 1
  assert items[0].source_id.endswith(":f1")
  # Broke at "stale": never fetched its messages, never paged to "older".
  assert fake.fetched_message_chats == ["fresh"]


def test_teams_adapter_skips_chats_with_no_recent_activity():
  recent_chat = _make_chat(chat_id="recent")
  stale_chat = {**_make_chat(chat_id="stale"), "lastUpdatedDateTime": "2024-01-01T00:00:00Z"}

  recent_msg = _make_user_message(message_id="r1", chat_id="recent", created="2026-04-25T10:00:00Z")
  fake = _FakeGraphClient(
    chats=[recent_chat, stale_chat],
    messages_by_chat={"recent": [recent_msg], "stale": [recent_msg]},
    members_by_chat={"recent": _make_members(), "stale": _make_members()},
  )

  adapter = TeamsAdapter(profile="work", graph_client=fake)
  items = list(adapter.extract(datetime(2026, 4, 1, tzinfo=UTC)))

  assert len(items) == 1
