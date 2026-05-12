"""Interactive enable/disable TUI for ingest sources.

Reads the active config.yaml, lists every `ingest.<source>` block that has an
`enabled:` key, lets the user toggle them with arrow keys + space, apply with
enter. For path-list sources (folders, email) the user can also add and
remove paths inline with `a` and `d`. On apply, the YAML file is rewritten
in place so comments, indentation, and unrelated keys survive untouched.

The `folders` row is always shown, even if the user has no `ingest.folders`
block in config — pressing `a` creates the block on first add.
"""

from __future__ import annotations

import re
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


# `ingest.<key>` blocks that are containers for sub-sources (per-profile teams,
# calendars). The top-level `enabled:` flag still gates them, but the meaning
# is "process the listed profiles" rather than "process this single source".
PROFILE_AWARE = {"teams", "calendar"}

# Sources whose entries are a list of paths the user can manage from the TUI.
PATH_LIST_SOURCES = {"email", "folders"}


@dataclass
class SourceRow:
  kind: Literal["source"]
  name: str
  enabled: bool
  summary: str
  synthetic: bool = False  # True when no config block exists yet


@dataclass
class SubPathRow:
  kind: Literal["subpath"]
  parent: str
  index: int
  label: str


Row = SourceRow | SubPathRow


def _build_rows(cfg: dict) -> list[Row]:
  """Enumerate every toggleable source plus its sub-paths.

  `email.sources` and `folders.paths` entries appear as indented child rows
  under their parent so the cursor can land on them for removal. `folders`
  is always emitted even when missing from config so the user can add the
  first path interactively.
  """
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
        rows.append(SubPathRow(
          kind="subpath", parent=key, index=i, label=f"{t}: {p}",
        ))
    elif key == "folders":
      for i, path in enumerate(block.get("paths") or []):
        rows.append(SubPathRow(
          kind="subpath", parent=key, index=i, label=str(path),
        ))

  if not any(isinstance(r, SourceRow) and r.name == "folders" for r in rows):
    rows.append(SourceRow(
      kind="source",
      name="folders",
      enabled=False,
      summary="0 source(s)  (not configured — press `a` to add)",
      synthetic=True,
    ))
  return rows


def _summary_for(key: str, block: dict) -> str:
  if key == "imessage":
    return str(block.get("chat_db_path", ""))
  if key == "signal":
    return str(block.get("signal_dir", ""))
  if key == "email":
    count = len(block.get("sources") or [])
    return f"{count} source(s)"
  if key == "folders":
    count = len(block.get("paths") or [])
    return f"{count} source(s)"
  if key == "notes":
    return str(block.get("vault_path", ""))
  if key == "tier2_ledger":
    return str(block.get("notes_path", ""))
  if key == "github":
    user = block.get("username", "?")
    return f"user={user}"
  if key in PROFILE_AWARE:
    profiles = block.get("profiles") or []
    return f"profiles={','.join(profiles) or '(none)'}"
  return ""


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
      sys.stdout.write(f"{cursor_mark}     {DIM}{BULLET}{RESET}  {DIM}{row.label}{RESET}\n")

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
    sys.stdout.write(f"  {DIM}No pending toggles{RESET}\n")
  if message:
    sys.stdout.write(f"\n  {YELLOW}{message}{RESET}\n")
  sys.stdout.flush()


def _prompt(question: str, default: str = "") -> str | None:
  """Restore cooked mode, prompt with click, return None on KeyboardInterrupt."""
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
  """Returns the final desired-state dict for source enables, or None to quit.

  Path adds/removes happen immediately (write through to disk + reload rows).
  This function does not return them; only the toggle state is returned.
  """
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
          if row.synthetic:
            message = "Add a path first (`a`)."
            continue
          current = selected.get(row.name, row.enabled)
          selected[row.name] = not current
        else:
          message = "Toggle only applies to source rows."
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


def _add_path(row: Row, config_path: Path) -> tuple[list[Row] | None, str | None]:
  """Prompt + write a new path for the cursor row. Returns (new_rows, message)."""
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
  return None, f"Adding paths is not supported for `{parent}`."


def _remove_path(row: Row, config_path: Path) -> tuple[list[Row] | None, str | None]:
  """Remove the path at the cursor. Returns (new_rows, message)."""
  if isinstance(row, SubPathRow):
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
# YAML in-place editors. Line-based so comments and unrelated keys survive.
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
  """Append a path under ingest.folders.paths. Creates the block if missing."""
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
  # If `paths: []` inline, convert to block style first.
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
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  if ingest_start is None:
    return
  folders_start, folders_end = _find_block_span(
    lines, top_level_key="folders", parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  if folders_start is None or folders_end is None:
    return
  paths_idx = _find_key_line(lines, folders_start, folders_end, "paths")
  if paths_idx is None:
    return
  entries = _list_entry_spans(lines, paths_idx, folders_end)
  if not (0 <= index < len(entries)):
    return
  start, end = entries[index]
  del lines[start:end]
  config_path.write_text("".join(lines))


def _ensure_folders_block(lines: list[str]) -> list[str]:
  """Insert `folders: enabled: false / paths: []` block if missing."""
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
  text = config_path.read_text()
  lines = text.splitlines(keepends=True)
  ingest_start, ingest_end = _find_block_span(lines, top_level_key="ingest")
  if ingest_start is None:
    return
  email_start, email_end = _find_block_span(
    lines, top_level_key="email", parent_indent=2,
    search_from=ingest_start, search_to=ingest_end,
  )
  if email_start is None or email_end is None:
    return
  sources_idx = _find_key_line(lines, email_start, email_end, "sources")
  if sources_idx is None:
    return
  entries = _list_entry_spans(lines, sources_idx, email_end)
  if not (0 <= index < len(entries)):
    return
  start, end = entries[index]
  del lines[start:end]
  config_path.write_text("".join(lines))


def _list_entry_spans(
  lines: list[str], list_key_idx: int, block_end: int,
) -> list[tuple[int, int]]:
  """Return [(start, end_exclusive)] line ranges for each `- ` item under a key."""
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
        click.echo(f"           - {row.label}")
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
