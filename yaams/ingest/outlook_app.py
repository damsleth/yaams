"""Local Outlook.app (macOS) calendar + mail ingester.

Talks to the classic Outlook for Mac (16.x) via its AppleScript dictionary
instead of Microsoft Graph, so it works offline against the locally-synced
store. Two yaams sources: ``outlook_calendar`` and ``outlook_mail``.

CLASSIC OUTLOOK ONLY. "New Outlook" for Mac keeps account data in a cloud-sync
store with no AppleScript access — the dictionary still resolves but exposes
only the empty legacy "On My Computer" containers, so extraction returns
nothing. ``_new_outlook_warning()`` detects this and logs why. New-Outlook
users should ingest via the owa-piggy Graph ``mail`` / ``calendar`` sources, or
toggle off "New Outlook" to fall back to Classic.

Why AppleScript and not the on-disk ``Outlook.sqlite``: the SQLite schema is
undocumented, version-volatile, and locked (RecordLocks.sqlite) while the app
runs. AppleScript is the supported, stable contract.

Performance: every property is pulled in *bulk* — ``subject of every message
whose ...`` returns the whole column in a single Apple event, so a folder
costs O(properties) round-trips, not O(messages). Dates are emitted as local
``y:mo:dy:h:mi:s`` components and converted to UTC here (Python's
``astimezone`` resolves DST); formatting them in AppleScript is painful.

ponytail: only Exchange/M365 accounts and the Inbox+Sent folders are walked,
and recurring events are taken as Outlook materializes them. Widen the folder
list / handle recurrence masters explicitly if a gap shows up.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Iterator

from yaams.ingest.base import Item, hash_id
from yaams.ingest.email_mbox import (
  MAX_EMAIL_CHARS,
  clean_email_body,
  is_automated_sender,
)
from yaams.time import ensure_utc

logger = logging.getLogger("yaams.ingest.outlook_app")

# Field / record separators: ASCII unit (31) and record (30) separators. Email
# bodies are full of newlines and tabs, so reuse control chars no body carries.
FS = "\x1f"
RS = "\x1e"

# AppleScript can wedge for minutes during the initial sync; give it room.
_OSASCRIPT_TIMEOUT = 1800

_ISO_HANDLER = """
on isoLocal(d)
  if d is missing value then return ""
  set y to year of d
  set m to (month of d as integer)
  set dd to day of d
  set h to hours of d
  set mi to minutes of d
  set s to seconds of d
  return (y as text) & ":" & (m as text) & ":" & (dd as text) & ":" & (h as text) & ":" & (mi as text) & ":" & (s as text)
end isoLocal
"""


def _since_block(since: datetime) -> str:
  """AppleScript that builds `sinceDate` as a local-time date object.

  `since` is UTC; Outlook compares in local time, so convert here. `day` is
  pinned to 1 first so setting `month` can't overflow (e.g. Jan-31 → Feb).
  """
  loc = ensure_utc(since).astimezone()
  return (
    "set sinceDate to (current date)\n"
    "set day of sinceDate to 1\n"
    f"set year of sinceDate to {loc.year}\n"
    f"set month of sinceDate to {loc.month}\n"
    f"set day of sinceDate to {loc.day}\n"
    f"set hours of sinceDate to {loc.hour}\n"
    f"set minutes of sinceDate to {loc.minute}\n"
    f"set seconds of sinceDate to {loc.second}\n"
  )


def _run_osascript(script: str) -> str:
  full = (
    _ISO_HANDLER
    + 'set FS to (ASCII character 31)\nset RS to (ASCII character 30)\n'
    + f"with timeout of {_OSASCRIPT_TIMEOUT} seconds\n"
    + script
    + "\nend timeout\n"
  )
  try:
    result = subprocess.run(
      ["osascript", "-e", full],
      capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT + 60,
    )
  except subprocess.TimeoutExpired:
    logger.warning("osascript timed out after %ss (Outlook still syncing?)", _OSASCRIPT_TIMEOUT)
    return ""
  if result.returncode != 0:
    logger.warning("osascript failed (rc=%d): %s", result.returncode, (result.stderr or "").strip())
    return ""
  return result.stdout


@lru_cache(maxsize=1)
def _new_outlook_warning() -> str | None:
  """One-shot check (per process) for "New Outlook" for Mac.

  New Outlook keeps account data in its own cloud-sync store with NO
  AppleScript access — the dictionary still resolves but exposes only the
  empty legacy "On My Computer" containers (Inbox, Sent, Calendar, all 0). So
  outlook_calendar / outlook_mail silently return nothing. Detect the
  signature (0 Exchange accounts but folders present) and return a human
  explanation; None when classic Outlook with accounts is present (or we
  can't tell — don't cry wolf).
  """
  out = _run_osascript(
    'tell application "Microsoft Outlook" to return '
    "(count of exchange accounts as text) & FS & (count of mail folders as text)"
  )
  rec = out.strip().split(FS)
  if len(rec) != 2:
    return None
  try:
    accounts, folders = int(rec[0]), int(rec[1])
  except ValueError:
    return None
  if accounts == 0 and folders > 0:
    return (
      "New Outlook for Mac detected: AppleScript exposes only the empty legacy "
      "'On My Computer' store, not your synced accounts, so outlook_calendar / "
      "outlook_mail will return nothing. Use the owa-piggy Graph 'mail' / "
      "'calendar' sources instead, or toggle off 'New Outlook' (Settings ▸ New "
      "Outlook) to fall back to Classic Outlook, which is AppleScript-readable."
    )
  return None


def _parse_records(out: str, n_fields: int) -> Iterator[list[str]]:
  for rec in out.split(RS):
    if not rec:
      continue
    fields = rec.split(FS)
    if len(fields) >= n_fields:
      yield fields


def _local_to_utc(comp: str) -> datetime | None:
  parts = comp.split(":")
  if len(parts) != 6:
    return None
  try:
    y, mo, dy, h, mi, s = (int(p) for p in parts)
    naive = datetime(y, mo, dy, h, mi, s)
  except ValueError:
    return None
  # A naive datetime's .astimezone(UTC) treats it as local time first.
  return naive.astimezone(UTC)


# --- Calendar -------------------------------------------------------------

_CAL_SCRIPT = """
set outRecs to {}
tell application "Microsoft Outlook"
  repeat with c in calendars
    set evList to (every calendar event of c whose start time ≥ sinceDate)
    if (count of evList) > 0 then
      set ids to (exchange id of evList)
      set starts to (start time of evList)
      set ends to (end time of evList)
      set subs to (subject of evList)
      set orgs to (organizer of evList)
      set locs to (location of evList)
      set bodies to (plain text content of evList)
      repeat with i from 1 to count of ids
        set rec to (item i of ids as text) & FS & my isoLocal(item i of starts) & FS & my isoLocal(item i of ends) & FS & (item i of subs as text) & FS & (item i of orgs as text) & FS & (item i of locs as text) & FS & (item i of bodies as text)
        set end of outRecs to rec
      end repeat
    end if
  end repeat
end tell
set AppleScript's text item delimiters to RS
set outStr to outRecs as text
set AppleScript's text item delimiters to ""
return outStr
"""


@dataclass
class OutlookCalendarAdapter:
  def extract(self, since: datetime) -> Iterator[Item]:
    warning = _new_outlook_warning()
    if warning:
      logger.warning("%s", warning)
    out = _run_osascript(_since_block(since) + _CAL_SCRIPT)
    for f in _parse_records(out, 7):
      ev_id, start_c, end_c, subject, organizer, location, *rest = f
      body = (rest[0] if rest else "").strip()
      subject = subject.strip()
      ts = _local_to_utc(start_c)
      if ts is None or not subject:
        continue
      content = subject
      if location.strip():
        content += f"\nLocation: {location.strip()}"
      source = "outlook_calendar"
      source_id = f"{ev_id}:{start_c}"
      yield Item(
        id=hash_id(source, source_id),
        source=source,
        source_id=source_id,
        timestamp=ts,
        sender=organizer.strip() or "me",
        recipients=[],
        content=content,
        subject=subject,
        thread_id=None,
        raw_metadata={
          "exchange_id": ev_id,
          "end": end_c,
          "organizer": organizer.strip(),
          "location": location.strip(),
          "body": body[:MAX_EMAIL_CHARS],
        },
      )


# --- Mail -----------------------------------------------------------------

def _folder_block(folder_expr: str, time_field: str) -> str:
  """Harvest one folder. `time_field` differs: Sent items carry `time sent`,
  not `time received`."""
  return f"""
    set theFolder to ({folder_expr})
    set fname to (name of theFolder)
    set msgList to (every message of theFolder whose {time_field} ≥ sinceDate)
    if (count of msgList) > 0 then
      set ids to (exchange id of msgList)
      set subs to (subject of msgList)
      set times to ({time_field} of msgList)
      set bodies to (plain text content of msgList)
      try
        set fromAddrs to (address of sender of msgList)
      on error
        set fromAddrs to {{}}
      end try
      repeat with i from 1 to count of ids
        if (count of fromAddrs) ≥ i then
          set fa to (item i of fromAddrs as text)
        else
          set fa to ""
        end if
        set rec to (item i of ids as text) & FS & my isoLocal(item i of times) & FS & (item i of subs as text) & FS & fa & FS & fname & FS & (item i of bodies as text)
        set end of outRecs to rec
      end repeat
    end if
"""


_MAIL_SCRIPT = f"""
set outRecs to {{}}
tell application "Microsoft Outlook"
  repeat with acct in exchange accounts
{_folder_block("inbox of acct", "time received")}
{_folder_block("sent items of acct", "time sent")}
  end repeat
end tell
set AppleScript's text item delimiters to RS
set outStr to outRecs as text
set AppleScript's text item delimiters to ""
return outStr
"""


@dataclass
class OutlookMailAdapter:
  skip_newsletters: bool = True
  skipped_newsletters: int = field(default=0, init=False)
  scanned_through: datetime | None = field(default=None, init=False)

  def extract(self, since: datetime) -> Iterator[Item]:
    self.skipped_newsletters = 0
    self.scanned_through = None
    warning = _new_outlook_warning()
    if warning:
      logger.warning("%s", warning)
    out = _run_osascript(_since_block(since) + _MAIL_SCRIPT)
    for f in _parse_records(out, 6):
      msg_id, ts_c, subject, sender, folder, *rest = f
      body_raw = rest[0] if rest else ""
      ts = _local_to_utc(ts_c)
      if ts is not None and (self.scanned_through is None or ts > self.scanned_through):
        self.scanned_through = ts
      sender = sender.strip()
      is_sent = folder.lower().startswith("sent")
      if self.skip_newsletters and not is_sent and is_automated_sender(sender):
        self.skipped_newsletters += 1
        continue
      if ts is None or not msg_id:
        continue
      body = clean_email_body(body_raw.strip())
      if not body:
        continue
      source = "outlook_mail"
      yield Item(
        id=hash_id(source, msg_id),
        source=source,
        source_id=msg_id,
        timestamp=ts,
        sender=sender or "unknown",
        recipients=[],
        content=body[:MAX_EMAIL_CHARS],
        subject=subject.strip(),
        thread_id=None,
        raw_metadata={
          "exchange_id": msg_id,
          "folder": folder,
        },
      )
