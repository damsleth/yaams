from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime

from yaams.ingest.teams_channels import (
  TeamsChannelsAdapter,
  _is_bot_row,
  _to_item,
)

# ---------------------------------------------------------------------------
# Canned owa-teams JSON shapes.
# ---------------------------------------------------------------------------


def _row(
  *,
  message_id: str = "1665994613428",
  root_id: str = "1665994613428",
  channel_id: str = "19:chan@thread.tacv2",
  team_id: str = "team-1",
  subject: str | None = "TV-aksjonen 2022",
  content: str = "Takk for innsatsen i går!",
  name: str = "Alice Hansen",
  sequence_id: int = 8,
  created: str = "2026-05-20T10:00:00Z",
) -> dict:
  """A message row shaped like `owa-teams messages` output."""
  is_reply = root_id != message_id
  return {
    "id": message_id,
    "threadId": f"{channel_id}:{root_id}",
    "rootMessageId": root_id,
    "isReply": is_reply,
    "sequenceId": sequence_id,
    "from": {"id": "8:orgid:oid-a", "name": name, "mri": "8:orgid:oid-a"},
    "timestamp": created,
    "subject": subject,
    "content": content,
    "messageType": "RichText/Html",
    "teamId": team_id,
    "channelId": channel_id,
  }


class _FakeProc:
  def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
    self.stdout = stdout
    self.returncode = returncode
    self.stderr = stderr


def _fake_run(
  teams: list[dict],
  channels_by_team: dict[str, list[dict]],
  messages_by_channel: dict[str, list[dict]],
):
  """Build a subprocess.run replacement dispatching on the owa-teams verb."""
  def run(cmd, capture_output=True, text=True):  # noqa: ARG001
    assert cmd[0] == "owa-teams"
    assert "--profile" in cmd
    verb = cmd[1]
    if verb == "teams":
      return _FakeProc(json.dumps(teams))
    if verb == "channels":
      team_id = cmd[cmd.index("--team") + 1]
      return _FakeProc(json.dumps(channels_by_team.get(team_id, [])))
    if verb == "messages":
      channel_id = cmd[cmd.index("--channel") + 1]
      return _FakeProc(json.dumps(messages_by_channel.get(channel_id, [])))
    return _FakeProc("[]")
  return run


# ---------------------------------------------------------------------------
# Pure _to_item mapping.
# ---------------------------------------------------------------------------


def test_to_item_basic():
  item = _to_item(
    _row(), "work", team_id="team-1",
    team_name="Marketing", channel_name="General",
  )
  assert item is not None
  assert item.source == "teams_channels_work"
  assert item.source_id == "19:chan@thread.tacv2:1665994613428"
  assert item.timestamp == datetime(2026, 5, 20, 10, 0, tzinfo=UTC)
  assert item.sender == "Alice Hansen"
  assert item.recipients == []          # broadcast, not addressed
  assert "Takk for innsatsen" in item.content
  assert item.subject == "TV-aksjonen 2022"
  assert item.thread_id == "19:chan@thread.tacv2:1665994613428"
  meta = item.raw_metadata
  assert meta["chat_type"] == "channel"
  assert meta["team_id"] == "team-1"
  assert meta["team_name"] == "Marketing"
  assert meta["channel_id"] == "19:chan@thread.tacv2"
  assert meta["channel_name"] == "General"
  assert meta["root_message_id"] == "1665994613428"
  assert meta["is_reply"] is False
  assert meta["sequence_id"] == 8
  assert meta["sender_mri"] == "8:orgid:oid-a"


def test_to_item_subject_fallback_to_team_and_channel():
  item = _to_item(
    _row(subject=None), "work", team_id="team-1",
    team_name="Marketing", channel_name="General",
  )
  assert item is not None
  assert item.subject == "Marketing / General"


def test_to_item_reply_inherits_subject_and_thread():
  root = _row(message_id="1665994613428", root_id="1665994613428")
  reply = _row(
    message_id="1666190353792", root_id="1665994613428",
    subject="TV-aksjonen 2022",  # owa-teams stamps the root subject on replies
    content="Enig!", sequence_id=9,
  )
  root_item = _to_item(root, "work", "team-1", "Marketing", "General")
  reply_item = _to_item(reply, "work", "team-1", "Marketing", "General")
  assert root_item is not None and reply_item is not None
  # Same thread clusters root with its replies.
  assert reply_item.thread_id == root_item.thread_id
  assert reply_item.subject == root_item.subject == "TV-aksjonen 2022"
  assert reply_item.raw_metadata["is_reply"] is True
  assert root_item.raw_metadata["is_reply"] is False
  # Distinct items.
  assert reply_item.source_id != root_item.source_id


def test_to_item_falls_back_to_sender_id_when_no_name():
  row = _row(name="")
  row["from"]["id"] = "8:orgid:ghost"
  item = _to_item(row, "work", "team-1", "Marketing", "General")
  assert item is not None
  assert item.sender == "8:orgid:ghost"


def test_to_item_empty_content_returns_none():
  assert _to_item(_row(content=""), "work", "t", "T", "C") is None
  assert _to_item(_row(content="   "), "work", "t", "T", "C") is None


def test_to_item_missing_or_bad_timestamp_returns_none():
  row = _row()
  row["timestamp"] = None
  assert _to_item(row, "work", "t", "T", "C") is None
  row["timestamp"] = "not-a-date"
  assert _to_item(row, "work", "t", "T", "C") is None


def test_to_item_id_is_stable_across_runs():
  a = _to_item(_row(), "work", "t", "T", "C")
  b = _to_item(_row(), "work", "t", "T", "C")
  assert a is not None and b is not None
  assert a.id == b.id


def test_is_bot_row_matches_known_bot_name():
  assert _is_bot_row({"from": {"name": "Approvals"}})
  assert not _is_bot_row({"from": {"name": "Alice Hansen"}})
  assert not _is_bot_row({"from": {}})


# ---------------------------------------------------------------------------
# Adapter — threading, cutoff, allowlist, skip counters.
# ---------------------------------------------------------------------------


def _adapter(monkeypatch, *, teams_json, channels_by_team, messages_by_channel, **kw):
  """Build an adapter wired to a fake owa-teams. `teams_json` is the fake
  `owa-teams teams` listing; the adapter's own `teams` allowlist comes via kw."""
  import yaams.ingest.teams_channels as mod
  monkeypatch.setattr(
    mod.subprocess, "run",
    _fake_run(teams_json, channels_by_team, messages_by_channel),
  )
  return TeamsChannelsAdapter(profile="work", **kw)


def test_adapter_threads_root_and_reply_together(monkeypatch):
  chan = "19:chan@thread.tacv2"
  root = _row(message_id="r1", root_id="r1", subject="Topic", sequence_id=1)
  reply = _row(
    message_id="r2", root_id="r1", subject="Topic",
    content="reply body", sequence_id=2,
  )
  adapter = _adapter(
    monkeypatch,
    teams_json=[{"id": "team-1", "displayName": "Marketing"}],
    channels_by_team={"team-1": [{"id": chan, "displayName": "General"}]},
    messages_by_channel={chan: [root, reply]},
  )
  items = list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC)))
  assert len(items) == 2
  assert items[0].thread_id == items[1].thread_id == f"{chan}:r1"
  assert {i.raw_metadata["is_reply"] for i in items} == {True, False}


def test_adapter_cutoff_filters_old_rows(monkeypatch):
  chan = "19:chan@thread.tacv2"
  old = _row(message_id="old", created="2024-01-01T10:00:00Z")
  recent = _row(message_id="recent", created="2026-05-20T10:00:00Z")
  adapter = _adapter(
    monkeypatch,
    teams_json=[{"id": "team-1", "displayName": "Marketing"}],
    channels_by_team={"team-1": [{"id": chan, "displayName": "General"}]},
    messages_by_channel={chan: [old, recent]},  # chronological, oldest-first
  )
  items = list(adapter.extract(datetime(2026, 4, 1, tzinfo=UTC)))
  assert len(items) == 1
  assert items[0].source_id.endswith(":recent")


def test_adapter_skip_bots_counter(monkeypatch):
  chan = "19:chan@thread.tacv2"
  human = _row(message_id="m1", name="Alice Hansen")
  bot = _row(message_id="m2", name="Approvals")
  adapter = _adapter(
    monkeypatch,
    teams_json=[{"id": "team-1", "displayName": "Marketing"}],
    channels_by_team={"team-1": [{"id": chan, "displayName": "General"}]},
    messages_by_channel={chan: [human, bot]},
    skip_bots=True,
  )
  items = list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC)))
  assert len(items) == 1
  assert items[0].source_id.endswith(":m1")
  assert adapter.skipped_bots == 1


def test_adapter_skip_bots_disabled_keeps_bot(monkeypatch):
  chan = "19:chan@thread.tacv2"
  bot = _row(message_id="m2", name="Approvals")
  adapter = _adapter(
    monkeypatch,
    teams_json=[{"id": "team-1", "displayName": "Marketing"}],
    channels_by_team={"team-1": [{"id": chan, "displayName": "General"}]},
    messages_by_channel={chan: [bot]},
    skip_bots=False,
  )
  items = list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC)))
  assert len(items) == 1
  assert adapter.skipped_bots == 0


def test_adapter_counts_missing_timestamp_as_empty(monkeypatch):
  chan = "19:chan@thread.tacv2"
  bad = _row(message_id="m1")
  bad["timestamp"] = None
  adapter = _adapter(
    monkeypatch,
    teams_json=[{"id": "team-1", "displayName": "Marketing"}],
    channels_by_team={"team-1": [{"id": chan, "displayName": "General"}]},
    messages_by_channel={chan: [bad]},
  )
  items = list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC)))
  assert items == []
  assert adapter.skipped_empty == 1


def test_adapter_empty_allowlist_enumerates_all_teams(monkeypatch):
  c1, c2 = "19:c1@thread.tacv2", "19:c2@thread.tacv2"
  adapter = _adapter(
    monkeypatch,
    teams_json=[
      {"id": "team-1", "displayName": "Marketing"},
      {"id": "team-2", "displayName": "Eng"},
    ],
    channels_by_team={
      "team-1": [{"id": c1, "displayName": "General"}],
      "team-2": [{"id": c2, "displayName": "General"}],
    },
    messages_by_channel={
      c1: [_row(message_id="a", channel_id=c1, team_id="team-1")],
      c2: [_row(message_id="b", channel_id=c2, team_id="team-2")],
    },
    # No `teams=` kwarg → adapter defaults to the empty allowlist (= all teams).
  )
  items = list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC)))
  assert {i.raw_metadata["team_id"] for i in items} == {"team-1", "team-2"}


def test_adapter_populated_allowlist_restricts(monkeypatch):
  c1, c2 = "19:c1@thread.tacv2", "19:c2@thread.tacv2"
  adapter = _adapter(
    monkeypatch,
    teams_json=[
      {"id": "team-1", "displayName": "Marketing"},
      {"id": "team-2", "displayName": "Eng"},
    ],
    channels_by_team={
      "team-1": [{"id": c1, "displayName": "General"}],
      "team-2": [{"id": c2, "displayName": "General"}],
    },
    messages_by_channel={
      c1: [_row(message_id="a", channel_id=c1, team_id="team-1")],
      c2: [_row(message_id="b", channel_id=c2, team_id="team-2")],
    },
  )
  # Restrict to team-1 only.
  adapter.teams = ("team-1",)
  items = list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC)))
  assert len(items) == 1
  assert items[0].raw_metadata["team_id"] == "team-1"


def test_adapter_tolerates_failed_owa_teams(monkeypatch):
  import yaams.ingest.teams_channels as mod

  def boom(cmd, capture_output=True, text=True):  # noqa: ARG001
    return _FakeProc(stdout="", returncode=1, stderr="auth blew up")

  monkeypatch.setattr(mod.subprocess, "run", boom)
  adapter = TeamsChannelsAdapter(profile="work")
  assert list(adapter.extract(datetime(2026, 1, 1, tzinfo=UTC))) == []


# ---------------------------------------------------------------------------
# CLI routing — the teams_channels_ branch must win over teams_.
# ---------------------------------------------------------------------------


def test_get_adapter_routes_to_channels_adapter():
  ingest_mod = importlib.import_module("yaams.cli.ingest")
  adapter = ingest_mod.get_adapter(
    "teams_channels_crayon",
    {"teams": ["team-1"], "limit_pages": 6, "skip_bots": False},
  )
  assert isinstance(adapter, TeamsChannelsAdapter)
  assert adapter.profile == "crayon"
  assert adapter.teams == ("team-1",)
  assert adapter.limit_pages == 6
  assert adapter.skip_bots is False


def test_config_section_maps_channels():
  ingest_mod = importlib.import_module("yaams.cli.ingest")
  assert ingest_mod._config_section("teams_channels_work") == "teams_channels"
  assert ingest_mod._config_section("teams-channels") == "teams_channels"
  # The chat branch is unaffected.
  assert ingest_mod._config_section("teams_work") == "teams"


def test_sources_to_run_enumerates_channels(monkeypatch):
  ingest_mod = importlib.import_module("yaams.cli.ingest")
  monkeypatch.setattr(
    ingest_mod.sources_mod, "discover_teams_profiles",
    lambda: [{"alias": "crayon", "enabled": True}],
  )
  cfg = {
    "ingest": {
      "teams": {"enabled": False, "profiles": []},
      "teams_channels": {"enabled": True, "profiles": ["crayon"]},
    }
  }
  assert ingest_mod._sources_to_run("teams-channels", cfg) == ["teams_channels_crayon"]
  assert "teams_channels_crayon" in ingest_mod._sources_to_run("all", cfg)
  assert ingest_mod._source_enabled(cfg, "teams_channels_crayon") is True


def test_source_enabled_respects_disabled_block(monkeypatch):
  ingest_mod = importlib.import_module("yaams.cli.ingest")
  monkeypatch.setattr(
    ingest_mod.sources_mod, "discover_teams_profiles",
    lambda: [{"alias": "crayon", "enabled": True}],
  )
  cfg = {"ingest": {"teams_channels": {"enabled": False, "profiles": ["crayon"]}}}
  assert ingest_mod._source_enabled(cfg, "teams_channels_crayon") is False
