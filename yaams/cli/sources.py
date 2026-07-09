"""Interactive enable/disable TUI for ingest sources.

Reads the active config.yaml, lists every `ingest.<source>` block that has an
`enabled:` key, lets the user toggle them with arrow keys + space, apply with
enter. For path-list sources (folders, email) and profile-aware sources
(calendar, teams, mail, drive) the TUI also shows individual sub-entries that
can be toggled and (for path sources) added/removed inline.

Calendar/teams discover their available profiles by shelling out to
`owa-cal profiles` and `owa-piggy profiles --json`; result is cached for the
TUI session. If a CLI is missing the discovery returns an empty list so the
TUI still works.

On apply, the YAML file is rewritten in place so comments, indentation, and
unrelated keys survive untouched.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import termios
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import click

from yaams.cli._root import cli
from yaams.cli._shared import config_option
from yaams.config import load_config, resolve_config_path

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RESET = "\033[0m"
CHECK = "◉"
EMPTY = "◯"
ARROW = "▸"
BULLET = "·"


PROFILE_AWARE = {"teams", "teams_channels", "calendar", "mail", "drive"}
PATH_LIST_SOURCES = {"email", "folders"}
SINGLE_PATH_SOURCES = {"notes"}

# What each owa-piggy profile `type` can feed. Grounded in ingest paths that
# exist today: owa-cal/owa-mail are Graph-only, so a google profile is drive
# only (drive picks its provider by token shape at ingest time, so a single
# `drive` row lists both m365 and google profiles); an ADO profile feeds
# nothing until the ado source lands. owa-piggy owns the `type`; yaams owns
# this mapping. Widen a row only when its ingest path exists.
SOURCES_BY_PROFILE_TYPE: dict[str, set[str]] = {
  "m365": {"mail", "calendar", "teams", "teams_channels", "drive"},
  "google": {"drive"},
  "ado": set(),
}
# Older owa-piggy has no `type` field; treat an unknown/absent type as m365 so
# behaviour is unchanged until the broker ships classification.
_DEFAULT_PROFILE_TYPE = "m365"


def _profile_type(prof: dict) -> str:
  return (prof.get("type") or _DEFAULT_PROFILE_TYPE).strip() or _DEFAULT_PROFILE_TYPE


def _type_supports(ptype: str, source_name: str) -> bool:
  return source_name in SOURCES_BY_PROFILE_TYPE.get(ptype, set())


def _supports(prof: dict, source_name: str) -> bool:
  return _type_supports(_profile_type(prof), source_name)

# M365 source blocks the TUI can lazy-create on first toggle. These are the
# sources whose availability is implied by an owa-piggy profile existing:
# rather than make the user edit YAML before they can see the checkbox,
# the TUI synthesizes a "not configured" row and writes a default block
# when the user toggles the parent or a profile child.
_M365_BLOCK_TEMPLATES: dict[str, list[str]] = {
  "mail": [
    "\n",
    "  mail:\n",
    "    enabled: false\n",
    "    profiles: []\n",
    "    folders:\n",
    "      - Inbox\n",
    "      - SentItems\n",
    "    skip_newsletters: true\n",
    "    chunk_days: 30\n",
    "    user_addresses: []\n",
  ],
  "calendar": [
    "\n",
    "  calendar:\n",
    "    enabled: false\n",
    "    profiles: []\n",
    "    skip_free: true\n",
  ],
  "teams": [
    "\n",
    "  teams:\n",
    "    enabled: false\n",
    "    profiles: []\n",
    "    skip_bots: true\n",
    "    page_size: 50\n",
  ],
  "teams_channels": [
    "\n",
    "  teams_channels:\n",
    "    enabled: false\n",
    "    profiles: []\n",
    "    teams: []\n",
    "    limit_pages: 4\n",
    "    skip_bots: true\n",
  ],
  "drive": [
    "\n",
    "  drive:\n",
    "    enabled: false\n",
    "    profiles: []\n",
    "    local_dir: ~/brain/docs\n",
  ],
}


@dataclass
class SourceRow:
  kind: Literal["source"]
  name: str
  enabled: bool
  summary: str
  synthetic: bool = False


@dataclass
class SubPathRow:
  kind: Literal["subpath"]
  parent: str
  subkind: Literal["path", "profile"]
  index: int
  label: str
  enabled: bool
  tag: str = ""  # e.g. "default" or "webcal" for profile rows


Row = SourceRow | SubPathRow


# ---------------------------------------------------------------------------
# External profile discovery (cached per process).
# ---------------------------------------------------------------------------


_profile_cache: dict[str, list[dict]] = {}


def _run_json(cmd: list[str], timeout: float = 3.0) -> object | None:
  try:
    proc = subprocess.run(
      cmd, capture_output=True, text=True, timeout=timeout, check=False,
    )
  except (FileNotFoundError, subprocess.TimeoutExpired):
    return None
  if proc.returncode != 0:
    return None
  out = proc.stdout.strip()
  if not out:
    return None
  try:
    return json.loads(out)
  except json.JSONDecodeError:
    return None


def discover_calendar_profiles() -> list[dict]:
  """Return [{alias, kind, default}] from `owa-cal profiles`."""
  if "calendar" in _profile_cache:
    return _profile_cache["calendar"]
  data = _run_json(["owa-cal", "profiles"])
  result: list[dict] = []
  if isinstance(data, list):
    for entry in data:
      if not isinstance(entry, dict):
        continue
      alias = entry.get("alias")
      if not alias:
        continue
      result.append({
        "alias": alias,
        "kind": entry.get("kind", ""),
        "default": bool(entry.get("default", False)),
      })
  _profile_cache["calendar"] = result
  return result


def discover_mail_profiles() -> list[dict]:
  """Return [{alias, default, enabled}] from `owa-piggy profiles --json`.

  Mail and Teams share the same auth audience (Graph), so they pull from
  the same owa-piggy profile list.
  """
  if "mail" in _profile_cache:
    return _profile_cache["mail"]
  _profile_cache["mail"] = list(discover_teams_profiles())
  return _profile_cache["mail"]


def discover_teams_profiles() -> list[dict]:
  """Return [{alias, default, enabled}] from `owa-piggy profiles --json`."""
  if "teams" in _profile_cache:
    return _profile_cache["teams"]
  data = _run_json(["owa-piggy", "profiles", "--json"])
  result: list[dict] = []
  if isinstance(data, dict):
    profiles = data.get("profiles") or []
    if isinstance(profiles, list):
      for entry in profiles:
        if not isinstance(entry, dict):
          continue
        alias = entry.get("alias")
        if not alias:
          continue
        enabled = True
        if "enabled" in entry:
          enabled = bool(entry.get("enabled"))
        elif "disabled" in entry:
          enabled = not bool(entry.get("disabled"))
        elif "registered" in entry:
          enabled = bool(entry.get("registered"))
        result.append({
          "alias": alias,
          "type": (entry.get("type") or _DEFAULT_PROFILE_TYPE),
          "default": bool(entry.get("default", False)),
          "enabled": enabled,
          "registered": bool(entry.get("registered", enabled)),
          "has_config": bool(entry.get("has_config", True)),
        })
  _profile_cache["teams"] = result
  return result


def _profile_types() -> dict[str, str]:
  """alias -> type from owa-piggy (the type authority).

  Used to filter sources whose profile list comes from another CLI that
  doesn't carry the type (calendar shells owa-cal). Aliases owa-piggy doesn't
  know default to m365 so a user's explicit config is never hidden.
  """
  return {p["alias"]: _profile_type(p) for p in discover_teams_profiles()}


def _config_alias_ok(alias: str, source_name: str, types: dict[str, str]) -> bool:
  """Keep an explicitly-configured alias unless owa-piggy knows its type AND
  that type can't feed this source. Unknown aliases are kept (never hide a
  user's own config just because the profile isn't discoverable)."""
  t = types.get(alias)
  return t is None or _type_supports(t, source_name)


def _clear_profile_cache() -> None:
  _profile_cache.clear()


# ---------------------------------------------------------------------------
# Row enumeration.
# ---------------------------------------------------------------------------


def _entry_path_and_enabled(entry: object) -> tuple[str | None, bool]:
  """Normalize a folders.paths entry. Returns (path, enabled)."""
  if isinstance(entry, str):
    return entry, True
  if isinstance(entry, dict):
    path = entry.get("path")
    if not isinstance(path, str):
      return None, True
    enabled = entry.get("enabled", True)
    return path, bool(enabled)
  return None, True


def _build_rows(cfg: dict) -> list[Row]:
  ingest = cfg.get("ingest") or {}
  rows: list[Row] = []
  for key, block in sorted(ingest.items()):
    if not isinstance(block, dict):
      continue
    if "enabled" not in block:
      continue
    rows.append(SourceRow(
      kind="source",
      name=key,
      enabled=bool(block.get("enabled")),
      summary=_summary_for(key, block),
    ))
    if key == "email":
      for i, entry in enumerate(block.get("sources") or []):
        if not isinstance(entry, dict):
          continue
        t = entry.get("type", "?")
        p = entry.get("path", "")
        enabled = bool(entry.get("enabled", True))
        rows.append(SubPathRow(
          kind="subpath", parent=key, subkind="path", index=i,
          label=f"{t}: {p}", enabled=enabled,
        ))
    elif key == "folders":
      for i, entry in enumerate(block.get("paths") or []):
        path, enabled = _entry_path_and_enabled(entry)
        if path is None:
          continue
        rows.append(SubPathRow(
          kind="subpath", parent=key, subkind="path", index=i,
          label=path, enabled=enabled,
        ))
    elif key == "calendar":
      configured = list(block.get("profiles") or [])
      # owa-cal's list carries no profile type; owa-piggy is the authority.
      types = _profile_types()
      available = [
        p for p in discover_calendar_profiles()
        if _type_supports(types.get(p["alias"], _DEFAULT_PROFILE_TYPE), key)
      ]
      seen: set[str] = set()
      for prof in available:
        alias = prof["alias"]
        seen.add(alias)
        tag_parts = []
        if prof.get("kind"):
          tag_parts.append(str(prof["kind"]))
        if prof.get("default"):
          tag_parts.append("default")
        rows.append(SubPathRow(
          kind="subpath", parent=key, subkind="profile", index=0,
          label=alias, enabled=alias in configured,
          tag=" / ".join(tag_parts),
        ))
      for alias in configured:
        if alias not in seen and _config_alias_ok(alias, key, types):
          rows.append(SubPathRow(
            kind="subpath", parent=key, subkind="profile", index=0,
            label=alias, enabled=True, tag="not discovered",
          ))
    elif key == "mail":
      configured = list(block.get("profiles") or [])
      types = _profile_types()
      available = [p for p in discover_mail_profiles() if _supports(p, key)]
      seen = set()
      for prof in available:
        alias = prof["alias"]
        seen.add(alias)
        tag_parts = []
        if prof.get("default"):
          tag_parts.append("default")
        if not prof.get("enabled", True):
          tag_parts.append("disabled")
        rows.append(SubPathRow(
          kind="subpath", parent=key, subkind="profile", index=0,
          label=alias, enabled=alias in configured,
          tag=" / ".join(tag_parts),
        ))
      for alias in configured:
        if alias not in seen and _config_alias_ok(alias, key, types):
          rows.append(SubPathRow(
            kind="subpath", parent=key, subkind="profile", index=0,
            label=alias, enabled=True, tag="not discovered",
          ))
    elif key in ("teams", "teams_channels", "drive"):
      # All share owa-piggy's profile list. teams/teams_channels are Graph/ic3
      # (m365 only); drive also accepts google (filtered per-key by _supports).
      configured = list(block.get("profiles") or [])
      types = _profile_types()
      available = [p for p in discover_teams_profiles() if _supports(p, key)]
      seen = set()
      for prof in available:
        alias = prof["alias"]
        seen.add(alias)
        tag_parts = []
        if prof.get("default"):
          tag_parts.append("default")
        if not prof.get("enabled", True):
          tag_parts.append("disabled")
        rows.append(SubPathRow(
          kind="subpath", parent=key, subkind="profile", index=0,
          label=alias, enabled=alias in configured,
          tag=" / ".join(tag_parts),
        ))
      for alias in configured:
        if alias not in seen and _config_alias_ok(alias, key, types):
          rows.append(SubPathRow(
            kind="subpath", parent=key, subkind="profile", index=0,
            label=alias, enabled=True, tag="not discovered",
          ))

  if not any(isinstance(r, SourceRow) and r.name == "folders" for r in rows):
    rows.append(SourceRow(
      kind="source",
      name="folders",
      enabled=False,
      summary="0 source(s)  (not configured — press `a` to add)",
      synthetic=True,
    ))

  if not any(isinstance(r, SourceRow) and r.name == "notes" for r in rows):
    rows.append(SourceRow(
      kind="source",
      name="notes",
      enabled=False,
      summary="(not configured — press `a` to set vault path)",
      synthetic=True,
    ))

  # Synthesize missing M365 source rows. If owa-piggy reports any enabled
  # profile, expose mail/calendar/teams as toggleable rows even when the
  # config doesn't have the block yet — first toggle writes it.
  _append_synthetic_m365_rows(rows)
  return rows


def _append_synthetic_m365_rows(rows: list[Row]) -> None:
  configured = {r.name for r in rows if isinstance(r, SourceRow)}
  piggy = [p for p in discover_teams_profiles() if p.get("enabled", True)]
  if not piggy:
    return
  for source_name in ("mail", "calendar", "teams", "teams_channels", "drive"):
    if source_name in configured:
      continue
    eligible = [p for p in piggy if _supports(p, source_name)]
    if not eligible:
      # No profile can feed this source (e.g. only google/ado profiles exist);
      # don't synthesize a row that would ingest nothing.
      continue
    rows.append(SourceRow(
      kind="source",
      name=source_name,
      enabled=False,
      summary="not configured — toggle a profile to create the block",
      synthetic=True,
    ))
    for prof in eligible:
      alias = prof["alias"]
      tag = "default" if prof.get("default") else ""
      rows.append(SubPathRow(
        kind="subpath", parent=source_name, subkind="profile", index=0,
        label=alias, enabled=False, tag=tag,
      ))


def _summary_for(key: str, block: dict) -> str:
  if key == "imessage":
    return str(block.get("chat_db_path", ""))
  if key == "signal":
    return str(block.get("signal_dir", ""))
  if key == "email":
    sources = block.get("sources") or []
    active = sum(1 for s in sources if isinstance(s, dict) and s.get("enabled", True))
    return f"{active}/{len(sources)} active"
  if key == "folders":
    paths = block.get("paths") or []
    active = 0
    for entry in paths:
      _, en = _entry_path_and_enabled(entry)
      if en:
        active += 1
    return f"{active}/{len(paths)} active"
  if key == "notes":
    vault = block.get("vault_path")
    return str(vault) if vault else "(no vault_path — press `a` to set)"
  if key == "tier2_ledger":
    return str(block.get("notes_path", ""))
  if key == "github":
    user = block.get("username", "?")
    return f"user={user}"
  if key == "calendar":
    configured = block.get("profiles") or []
    return f"{len(configured)} profile(s) active"
  if key == "teams":
    configured = block.get("profiles") or []
    return f"{len(configured)} profile(s) active"
  if key == "teams_channels":
    configured = block.get("profiles") or []
    allow = block.get("teams") or []
    scope = f"{len(allow)} team(s)" if allow else "all teams"
    return f"{len(configured)} profile(s) active, {scope}"
  if key == "mail":
    configured = block.get("profiles") or []
    return f"{len(configured)} profile(s) active"
  if key == "drive":
    configured = block.get("profiles") or []
    return f"{len(configured)} profile(s) active"
  if key == "outlook_calendar":
    return "Outlook.app (local, AppleScript)"
  if key == "outlook_mail":
    skip = block.get("skip_newsletters", True)
    return f"Outlook.app (local), skip_newsletters={str(bool(skip)).lower()}"
  return ""


# ---------------------------------------------------------------------------
# TUI plumbing.
# ---------------------------------------------------------------------------


def _read_key() -> str:
  fd = sys.stdin.fileno()
  old = termios.tcgetattr(fd)
  try:
    tty.setraw(fd)
    ch = sys.stdin.read(1)
    if ch == "\x1b":
      seq = sys.stdin.read(2)
      return "\x1b" + seq
    return ch
  finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render(
  rows: list[Row],
  selected: dict[str, bool],
  cursor: int,
  config_path: Path,
  message: str | None,
) -> None:
  sys.stdout.write("\033[H\033[2J")
  sys.stdout.write(f"{BOLD}YAAMS Sources{RESET}  {DIM}({config_path}){RESET}\n")
  sys.stdout.write(
    f"{DIM}up/down navigate  ·  space toggle  ·  a add path  "
    f"·  d remove path  ·  enter apply  ·  q quit{RESET}\n\n"
  )
  for i, row in enumerate(rows):
    cursor_mark = f"{CYAN}{ARROW}{RESET} " if i == cursor else "  "
    if isinstance(row, SourceRow):
      desired = selected.get(row.name, row.enabled)
      box = f"{GREEN}{CHECK}{RESET}" if desired else f"{DIM}{EMPTY}{RESET}"
      indicator = ""
      if desired and not row.enabled:
        indicator = f"  {GREEN}← will enable{RESET}"
      elif not desired and row.enabled:
        indicator = f"  {RED}← will disable{RESET}"
      summary_text = f"  {DIM}{row.summary}{RESET}" if row.summary else ""
      sys.stdout.write(f"{cursor_mark}{box}  {BOLD}{row.name}{RESET}{indicator}{summary_text}\n")
    else:
      box = f"{GREEN}{CHECK}{RESET}" if row.enabled else f"{DIM}{EMPTY}{RESET}"
      label_color = "" if row.enabled else DIM
      tag_text = f"  {DIM}({row.tag}){RESET}" if row.tag else ""
      sys.stdout.write(
        f"{cursor_mark}     {box}  {label_color}{row.label}{RESET}{tag_text}\n"
      )

  pending_on = 0
  pending_off = 0
  for row in rows:
    if isinstance(row, SourceRow):
      desired = selected.get(row.name, row.enabled)
      if desired and not row.enabled:
        pending_on += 1
      elif not desired and row.enabled:
        pending_off += 1
  sys.stdout.write("\n")
  if pending_on or pending_off:
    parts = []
    if pending_on:
      parts.append(f"{GREEN}{pending_on} to enable{RESET}")
    if pending_off:
      parts.append(f"{RED}{pending_off} to disable{RESET}")
    sys.stdout.write(f"  {', '.join(parts)}  {DIM}— press enter to apply{RESET}\n")
  else:
    sys.stdout.write(f"  {DIM}No pending parent toggles{RESET}\n")
  if message:
    sys.stdout.write(f"\n  {YELLOW}{message}{RESET}\n")
  sys.stdout.flush()


def _prompt(question: str, default: str = "") -> str | None:
  sys.stdout.write("\033[?25h")
  sys.stdout.flush()
  try:
    return click.prompt(question, default=default, show_default=bool(default))
  except (click.Abort, KeyboardInterrupt):
    return None
  finally:
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()


def _interactive(rows: list[Row], config_path: Path) -> dict[str, bool] | None:
  selected: dict[str, bool] = {}
  cursor = 0
  message: str | None = None
  sys.stdout.write("\033[?25l")
  sys.stdout.flush()
  try:
    while True:
      _render(rows, selected, cursor, config_path, message)
      message = None
      key = _read_key()
      if key in ("q", "\x03"):
        return None
      if key in ("k", "\x1b[A"):
        cursor = max(0, cursor - 1)
        continue
      if key in ("j", "\x1b[B"):
        cursor = min(len(rows) - 1, cursor + 1)
        continue
      row = rows[cursor]
      if key == " ":
        if isinstance(row, SourceRow):
          allowed_synthetic = (
            row.name in _M365_BLOCK_TEMPLATES or row.name == "notes"
          )
          if row.synthetic and not allowed_synthetic:
            message = "Add a path first (`a`)."
            continue
          if row.synthetic and row.name == "notes":
            message = "Set a vault_path first (`a`)."
            continue
          current = selected.get(row.name, row.enabled)
          selected[row.name] = not current
        else:
          new_rows, message = _toggle_subpath(row, config_path)
          if new_rows is not None:
            rows[:] = new_rows
            cursor = min(cursor, len(rows) - 1)
        continue
      if key == "a":
        new_rows, message = _add_path(row, config_path)
        if new_rows is not None:
          rows[:] = new_rows
          cursor = min(cursor, len(rows) - 1)
        continue
      if key == "d":
        new_rows, message = _remove_path(row, config_path)
        if new_rows is not None:
          rows[:] = new_rows
          cursor = min(cursor, len(rows) - 1)
        continue
      if key in ("\r", "\n"):
        return {
          r.name: selected.get(r.name, r.enabled)
          for r in rows
          if isinstance(r, SourceRow) and not r.synthetic
        }
  finally:
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


def _toggle_subpath(row: SubPathRow, config_path: Path) -> tuple[list[Row] | None, str | None]:
  """Flip the enabled state of a child row (path or profile)."""
  desired = not row.enabled
  if row.subkind == "path":
    if row.parent == "email":
      _yaml_set_email_entry_enabled(config_path, row.index, desired)
    elif row.parent == "folders":
      _yaml_set_folder_entry_enabled(config_path, row.index, desired)
    else:
      return None, f"Toggle unsupported for {row.parent} paths."
  elif row.subkind == "profile":
    _yaml_set_profile_enabled(config_path, row.parent, row.label, desired)
  else:
    return None, "Unknown sub-row kind."
  cfg = load_config(config_path)
  state = "enabled" if desired else "disabled"
  return _build_rows(cfg), f"{row.parent}: {row.label} {state}."


def _add_path(row: Row, config_path: Path) -> tuple[list[Row] | None, str | None]:
  parent = row.name if isinstance(row, SourceRow) else row.parent
  if parent == "email":
    src_type = _prompt("Type [emlx/mbox]", default="emlx")
    if not src_type:
      return None, "Add cancelled."
    src_type = src_type.strip().lower()
    if src_type not in ("emlx", "mbox"):
      return None, f"Unsupported email type: {src_type}"
    path_val = _prompt("Path")
    if not path_val:
      return None, "Add cancelled."
    _yaml_append_email_source(config_path, src_type, path_val.strip())
    cfg = load_config(config_path)
    return _build_rows(cfg), f"Added email source: {src_type} {path_val}"
  if parent == "folders":
    path_val = _prompt("Path")
    if not path_val:
      return None, "Add cancelled."
    _yaml_append_folder_path(config_path, path_val.strip())
    cfg = load_config(config_path)
    return _build_rows(cfg), f"Added folder: {path_val}"
  if parent == "notes":
    cfg_current = load_config(config_path)
    current_vault = (
      (cfg_current.get("ingest", {}).get("notes") or {}).get("vault_path") or ""
    )
    path_val = _prompt("Obsidian vault path", default=str(current_vault))
    if not path_val:
      return None, "Add cancelled."
    _yaml_set_notes_vault_path(config_path, path_val.strip())
    cfg = load_config(config_path)
    return _build_rows(cfg), f"Set notes vault_path: {path_val}"
  return None, f"Adding paths is not supported for `{parent}`."


def _remove_path(row: Row, config_path: Path) -> tuple[list[Row] | None, str | None]:
  if isinstance(row, SubPathRow) and row.subkind == "path":
    if row.parent == "email":
      _yaml_remove_email_source(config_path, row.index)
      cfg = load_config(config_path)
      return _build_rows(cfg), f"Removed email source #{row.index}."
    if row.parent == "folders":
      _yaml_remove_folder_path(config_path, row.index)
      cfg = load_config(config_path)
      return _build_rows(cfg), f"Removed folder #{row.index}."
  return None, "Move cursor onto a path entry to remove it."


# ---------------------------------------------------------------------------
# YAML in-place editors.
# ---------------------------------------------------------------------------


def _rewrite_enabled_flags(config_path: Path, target_state: dict[str, bool]) -> dict[str, bool]:
  text = config_path.read_text()
  lines = text.splitlines(keepends=True)
  out_lines = list(lines)
  changed: dict[str, bool] = {}

  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  if ingest_start is None:
    raise click.ClickException(f"Could not find `ingest:` block in {config_path}")

  for source, desired in target_state.items():
    block_start, block_end = _find_block_span(
      lines, top_level_key=source, parent_indent=2,
      search_from=ingest_start, search_to=ingest_end,
    )
    if (block_start is None or block_end is None) and source in _M365_BLOCK_TEMPLATES:
      out_lines = _ensure_m365_block(out_lines, source)
      lines = list(out_lines)
      ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
      block_start, block_end = _find_block_span(
        lines, top_level_key=source, parent_indent=2,
        search_from=ingest_start, search_to=ingest_end,
      )
    if (block_start is None or block_end is None) and source == "notes":
      out_lines = _ensure_notes_block(out_lines)
      lines = list(out_lines)
      ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
      block_start, block_end = _find_block_span(
        lines, top_level_key=source, parent_indent=2,
        search_from=ingest_start, search_to=ingest_end,
      )
    if block_start is None or block_end is None:
      continue
    flag_line_idx = _find_enabled_line(lines, block_start, block_end)
    if flag_line_idx is None:
      continue
    current_line = out_lines[flag_line_idx]
    new_line = re.sub(
      r"(enabled\s*:\s*)(true|false|True|False|yes|no)\b",
      lambda m: m.group(1) + ("true" if desired else "false"),
      current_line,
      count=1,
    )
    if new_line != current_line:
      out_lines[flag_line_idx] = new_line
      changed[source] = desired

  if changed:
    config_path.write_text("".join(out_lines))
  return changed


def _yaml_append_folder_path(config_path: Path, value: str) -> None:
  lines = config_path.read_text().splitlines(keepends=True)
  lines = _ensure_folders_block(lines)
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  assert ingest_start is not None
  folders_start, folders_end = _find_block_span(
    lines, top_level_key="folders", parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  assert folders_start is not None and folders_end is not None
  paths_idx = _find_key_line(lines, folders_start, folders_end, "paths")
  insertion = f"      - {value}\n"
  if paths_idx is None:
    enabled_idx = _find_enabled_line(lines, folders_start, folders_end)
    insert_at = (enabled_idx + 1) if enabled_idx is not None else (folders_start + 1)
    lines.insert(insert_at, f"    paths:\n{insertion}")
    config_path.write_text("".join(lines))
    return
  if re.match(r"\s+paths\s*:\s*\[\s*\]\s*$", lines[paths_idx].rstrip("\n")):
    indent = re.match(r"^(\s+)", lines[paths_idx]).group(1)  # type: ignore[union-attr]
    lines[paths_idx] = f"{indent}paths:\n"
    lines.insert(paths_idx + 1, insertion)
    config_path.write_text("".join(lines))
    return
  entries = _list_entry_spans(lines, paths_idx, folders_end)
  insert_at = entries[-1][1] if entries else paths_idx + 1
  lines.insert(insert_at, insertion)
  config_path.write_text("".join(lines))


def _yaml_remove_folder_path(config_path: Path, index: int) -> None:
  lines = config_path.read_text().splitlines(keepends=True)
  spans = _folders_entry_spans(lines)
  if spans is None or not (0 <= index < len(spans)):
    return
  start, end = spans[index]
  del lines[start:end]
  config_path.write_text("".join(lines))


def _yaml_set_folder_entry_enabled(config_path: Path, index: int, enabled: bool) -> None:
  """Toggle a folder entry's enabled state.

  Handles both forms:
    - ~/path                  (bare string, implicit enabled=true)
    - path: ~/path            (dict form)
      enabled: false
  When disabling a bare string, rewrites it to dict form. When re-enabling,
  collapses the dict back to a bare string if it has no other keys.
  """
  lines = config_path.read_text().splitlines(keepends=True)
  spans = _folders_entry_spans(lines)
  if spans is None or not (0 <= index < len(spans)):
    return
  start, end = spans[index]
  entry_lines = lines[start:end]
  first = entry_lines[0]
  bare_match = re.match(r"^(\s+)-\s+(?!path\s*:)(.+?)\s*$", first.rstrip("\n"))
  dict_match = re.match(r"^(\s+)-\s+path\s*:\s*(.+?)\s*$", first.rstrip("\n"))

  if bare_match and not dict_match:
    indent = bare_match.group(1)
    value = bare_match.group(2)
    if enabled:
      return
    new_block = [
      f"{indent}- path: {value}\n",
      f"{indent}  enabled: false\n",
    ]
    lines[start:end] = new_block + entry_lines[1:]
    config_path.write_text("".join(lines))
    return

  if not dict_match:
    return

  indent = dict_match.group(1)
  enabled_line_idx: int | None = None
  for j in range(start + 1, end):
    if re.match(r"\s+enabled\s*:\s*(true|false|True|False|yes|no)\b", lines[j]):
      enabled_line_idx = j
      break

  if enabled_line_idx is None:
    if enabled:
      return
    insert_at = start + 1
    lines.insert(insert_at, f"{indent}  enabled: false\n")
    config_path.write_text("".join(lines))
    return

  lines[enabled_line_idx] = re.sub(
    r"(enabled\s*:\s*)(true|false|True|False|yes|no)\b",
    lambda m: m.group(1) + ("true" if enabled else "false"),
    lines[enabled_line_idx],
    count=1,
  )
  config_path.write_text("".join(lines))


def _folders_entry_spans(lines: list[str]) -> list[tuple[int, int]] | None:
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  if ingest_start is None:
    return None
  folders_start, folders_end = _find_block_span(
    lines, top_level_key="folders", parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  if folders_start is None or folders_end is None:
    return None
  paths_idx = _find_key_line(lines, folders_start, folders_end, "paths")
  if paths_idx is None:
    return []
  return _list_entry_spans(lines, paths_idx, folders_end)


def _ensure_m365_block(lines: list[str], source: str) -> list[str]:
  """Insert a default ingest.<source>: block if it's missing.

  Used when the user toggles a synthetic m365 row or one of its profile
  children — we lazy-create the block so they don't have to hand-edit
  YAML before the TUI can manage it.
  """
  if source not in _M365_BLOCK_TEMPLATES:
    return lines
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  if ingest_start is None:
    raise click.ClickException("No `ingest:` block in config.")
  block_start, _ = _find_block_span(
    lines, top_level_key=source, parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  if block_start is not None:
    return lines
  template = list(_M365_BLOCK_TEMPLATES[source])
  insert_at = ingest_end if ingest_end is not None else len(lines)
  if insert_at > 0 and lines[insert_at - 1].strip() == "":
    template = template[1:]
  lines[insert_at:insert_at] = template
  return lines


def _ensure_folders_block(lines: list[str]) -> list[str]:
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  if ingest_start is None:
    raise click.ClickException("No `ingest:` block in config.")
  folders_start, _ = _find_block_span(
    lines, top_level_key="folders", parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  if folders_start is not None:
    return lines
  block_lines = ["\n", "  folders:\n", "    enabled: false\n", "    paths: []\n"]
  insert_at = ingest_end if ingest_end is not None else len(lines)
  if insert_at > 0 and lines[insert_at - 1].strip() == "":
    block_lines = block_lines[1:]
  lines[insert_at:insert_at] = block_lines
  return lines


def _ensure_notes_block(lines: list[str]) -> list[str]:
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  if ingest_start is None:
    raise click.ClickException("No `ingest:` block in config.")
  notes_start, _ = _find_block_span(
    lines, top_level_key="notes", parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  if notes_start is not None:
    return lines
  block_lines = [
    "\n",
    "  notes:\n",
    "    enabled: false\n",
    "    vault_path: ~/Documents/Obsidian\n",
  ]
  insert_at = ingest_end if ingest_end is not None else len(lines)
  if insert_at > 0 and lines[insert_at - 1].strip() == "":
    block_lines = block_lines[1:]
  lines[insert_at:insert_at] = block_lines
  return lines


def _yaml_set_notes_vault_path(config_path: Path, value: str) -> None:
  lines = config_path.read_text().splitlines(keepends=True)
  lines = _ensure_notes_block(lines)
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  assert ingest_start is not None
  notes_start, notes_end = _find_block_span(
    lines, top_level_key="notes", parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  assert notes_start is not None and notes_end is not None
  vault_idx = _find_key_line(lines, notes_start, notes_end, "vault_path")
  new_line = f"    vault_path: {value}\n"
  if vault_idx is None:
    enabled_idx = _find_enabled_line(lines, notes_start, notes_end)
    insert_at = (enabled_idx + 1) if enabled_idx is not None else (notes_start + 1)
    lines.insert(insert_at, new_line)
  else:
    lines[vault_idx] = new_line
  config_path.write_text("".join(lines))


def _yaml_append_email_source(config_path: Path, src_type: str, path: str) -> None:
  text = config_path.read_text()
  lines = text.splitlines(keepends=True)
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  if ingest_start is None:
    raise click.ClickException(f"Could not find `ingest:` block in {config_path}")
  email_start, email_end = _find_block_span(
    lines, top_level_key="email", parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  if email_start is None or email_end is None:
    raise click.ClickException("No `ingest.email:` block in config.")
  sources_idx = _find_key_line(lines, email_start, email_end, "sources")
  insertion = f"      - type: {src_type}\n        path: {path}\n"
  if sources_idx is None:
    enabled_idx = _find_enabled_line(lines, email_start, email_end)
    insert_at = (enabled_idx + 1) if enabled_idx is not None else (email_start + 1)
    lines.insert(insert_at, f"    sources:\n{insertion}")
    config_path.write_text("".join(lines))
    return
  entries = _list_entry_spans(lines, sources_idx, email_end)
  insert_at = entries[-1][1] if entries else sources_idx + 1
  lines.insert(insert_at, insertion)
  config_path.write_text("".join(lines))


def _yaml_remove_email_source(config_path: Path, index: int) -> None:
  lines = config_path.read_text().splitlines(keepends=True)
  spans = _email_entry_spans(lines)
  if spans is None or not (0 <= index < len(spans)):
    return
  start, end = spans[index]
  del lines[start:end]
  config_path.write_text("".join(lines))


def _yaml_set_email_entry_enabled(config_path: Path, index: int, enabled: bool) -> None:
  lines = config_path.read_text().splitlines(keepends=True)
  spans = _email_entry_spans(lines)
  if spans is None or not (0 <= index < len(spans)):
    return
  start, end = spans[index]
  indent_match = re.match(r"^(\s+)-\s", lines[start])
  if indent_match is None:
    return
  child_indent = indent_match.group(1) + "  "
  enabled_idx: int | None = None
  for j in range(start, end):
    if re.match(r"\s+enabled\s*:\s*(true|false|True|False|yes|no)\b", lines[j]):
      enabled_idx = j
      break
  if enabled_idx is None:
    lines.insert(end, f"{child_indent}enabled: {'true' if enabled else 'false'}\n")
  else:
    lines[enabled_idx] = re.sub(
      r"(enabled\s*:\s*)(true|false|True|False|yes|no)\b",
      lambda m: m.group(1) + ("true" if enabled else "false"),
      lines[enabled_idx],
      count=1,
    )
  config_path.write_text("".join(lines))


def _email_entry_spans(lines: list[str]) -> list[tuple[int, int]] | None:
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  if ingest_start is None:
    return None
  email_start, email_end = _find_block_span(
    lines, top_level_key="email", parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  if email_start is None or email_end is None:
    return None
  sources_idx = _find_key_line(lines, email_start, email_end, "sources")
  if sources_idx is None:
    return []
  return _list_entry_spans(lines, sources_idx, email_end)


def _yaml_set_profile_enabled(
  config_path: Path, source: str, profile: str, enabled: bool,
) -> None:
  """Add or remove `profile` from `ingest.<source>.profiles`."""
  if source not in PROFILE_AWARE:
    return
  lines = config_path.read_text().splitlines(keepends=True)
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  if ingest_start is None:
    raise click.ClickException("No `ingest:` block in config.")
  block_start, block_end = _find_block_span(
    lines, top_level_key=source, parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  if (block_start is None or block_end is None) and source in _M365_BLOCK_TEMPLATES:
    lines = _ensure_m365_block(lines, source)
    ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
    block_start, block_end = _find_block_span(
      lines, top_level_key=source, parent_indent=2,
      search_from=ingest_start, search_to=ingest_end,
    )
  if block_start is None or block_end is None:
    raise click.ClickException(f"No `ingest.{source}:` block in config.")
  profiles_idx = _find_key_line(lines, block_start, block_end, "profiles")
  if profiles_idx is None:
    if not enabled:
      return
    enabled_idx = _find_enabled_line(lines, block_start, block_end)
    insert_at = (enabled_idx + 1) if enabled_idx is not None else (block_start + 1)
    lines[insert_at:insert_at] = [
      "    profiles:\n",
      f"      - {profile}\n",
    ]
    config_path.write_text("".join(lines))
    return
  flow_match = re.match(
    r"^(\s+)profiles\s*:\s*\[(?P<body>.*)\]\s*$",
    lines[profiles_idx].rstrip("\n"),
  )
  if flow_match:
    indent = flow_match.group(1)
    body = flow_match.group("body").strip()
    items: list[str] = []
    if body:
      for raw in body.split(","):
        item = raw.strip().strip("'").strip('"')
        if item:
          items.append(item)
    if enabled and profile not in items:
      items.append(profile)
    elif not enabled and profile in items:
      items.remove(profile)
    else:
      return
    replacement = [f"{indent}profiles:\n"]
    for item in items:
      replacement.append(f"{indent}  - {item}\n")
    lines[profiles_idx:profiles_idx + 1] = replacement
    config_path.write_text("".join(lines))
    return
  entries = _list_entry_spans(lines, profiles_idx, block_end)
  existing_idx: int | None = None
  for i, (s, _e) in enumerate(entries):
    match = re.match(r"^\s+-\s+(.+?)\s*$", lines[s].rstrip("\n"))
    if match and match.group(1) == profile:
      existing_idx = i
      break
  if enabled:
    if existing_idx is not None:
      return
    insert_at = entries[-1][1] if entries else profiles_idx + 1
    lines.insert(insert_at, f"      - {profile}\n")
  else:
    if existing_idx is None:
      return
    s, e = entries[existing_idx]
    del lines[s:e]
  config_path.write_text("".join(lines))


def _list_entry_spans(
  lines: list[str], list_key_idx: int, block_end: int,
) -> list[tuple[int, int]]:
  entries: list[tuple[int, int]] = []
  current_start: int | None = None
  for i in range(list_key_idx + 1, block_end):
    line = lines[i]
    stripped = line.rstrip("\n").rstrip("\r")
    if not stripped.strip():
      continue
    leading = len(stripped) - len(stripped.lstrip(" "))
    if leading <= 4 and not stripped.startswith("#"):
      break
    if re.match(r"\s+-\s", line):
      if current_start is not None:
        entries.append((current_start, i))
      current_start = i
  if current_start is not None:
    end = current_start + 1
    for j in range(current_start + 1, block_end):
      stripped = lines[j].rstrip("\n").rstrip("\r")
      if not stripped.strip():
        end = j + 1
        continue
      leading = len(stripped) - len(stripped.lstrip(" "))
      if leading <= 4 and not stripped.startswith("#"):
        break
      if re.match(r"\s+-\s", lines[j]):
        break
      end = j + 1
    entries.append((current_start, end))
  return entries


def _find_block_span(
  lines: list[str],
  *,
  top_level_key: str,
  parent_indent: int = 0,
  search_from: int | None = None,
  search_to: int | None = None,
) -> tuple[int | None, int | None]:
  start = search_from or 0
  stop = search_to if search_to is not None else len(lines)
  pattern = re.compile(rf"^[ ]{{{parent_indent}}}{re.escape(top_level_key)}\s*:")
  block_start = None
  for i in range(start, stop):
    if pattern.match(lines[i]):
      block_start = i
      break
  if block_start is None:
    return None, None
  block_end = stop
  for j in range(block_start + 1, stop):
    stripped = lines[j].rstrip("\n").rstrip("\r")
    if not stripped.strip():
      continue
    leading = len(stripped) - len(stripped.lstrip(" "))
    if leading <= parent_indent and not stripped.startswith("#"):
      block_end = j
      break
  return block_start, block_end


def _find_enabled_line(lines: list[str], block_start: int, block_end: int) -> int | None:
  for i in range(block_start + 1, block_end):
    if re.match(r"\s+enabled\s*:\s*(true|false|True|False|yes|no)\b", lines[i]):
      return i
  return None


def _find_key_line(
  lines: list[str], block_start: int, block_end: int, key: str,
) -> int | None:
  pattern = re.compile(rf"^\s+{re.escape(key)}\s*:")
  for i in range(block_start + 1, block_end):
    if pattern.match(lines[i]):
      return i
  return None


@cli.command("sources")
@config_option
def sources_cmd(config_path: str) -> None:
  """Toggle which ingest sources are enabled. Interactive TUI."""
  _clear_profile_cache()
  resolved = resolve_config_path(config_path)
  cfg = load_config(resolved)
  rows = _build_rows(cfg)
  if not rows:
    click.echo("No toggleable sources found under `ingest:`.")
    return

  if not (sys.stdin.isatty() and sys.stdout.isatty()):
    click.echo("yaams sources requires an interactive TTY.")
    for row in rows:
      if isinstance(row, SourceRow):
        mark = "on " if row.enabled else "off"
        tail = f"  {row.summary}" if row.summary else ""
        click.echo(f"  [{mark}] {row.name}{tail}")
      else:
        mark = "on " if row.enabled else "off"
        click.echo(f"           [{mark}] {row.label}")
    return

  result = _interactive(rows, resolved)
  sys.stdout.write("\033[H\033[2J")
  sys.stdout.flush()
  if result is None:
    click.echo("No toggles applied.")
    return

  changed = _rewrite_enabled_flags(resolved, result)
  if not changed:
    click.echo("No toggle changes.")
    return
  click.echo(f"Updated {resolved}:")
  for name, value in sorted(changed.items()):
    click.echo(f"  {name}: {'enabled' if value else 'disabled'}")
