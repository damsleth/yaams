"""Interactive enable/disable TUI for ingest sources.

Reads the active config.yaml, lists every `ingest.<source>` block that has an
`enabled:` key, lets the user toggle them with arrow keys + space, and on
apply rewrites only those `enabled:` lines in-place so comments, indentation,
and unrelated keys survive untouched.
"""

from __future__ import annotations

import re
import sys
import termios
import tty
from pathlib import Path

import click

from yaams.cli._root import cli
from yaams.cli._shared import config_option
from yaams.config import load_config, resolve_config_path

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"
CHECK = "◉"
EMPTY = "◯"
ARROW = "▸"


# `ingest.<key>` blocks that are containers for sub-sources (per-profile teams,
# calendars). The top-level `enabled:` flag still gates them, but the meaning
# is "process the listed profiles" rather than "process this single source".
PROFILE_AWARE = {"teams", "calendar"}


def _enumerate_sources(cfg: dict) -> list[tuple[str, bool, str]]:
  """Return (name, enabled, summary) for each toggleable ingest source.

  `name` is the config key under `ingest.` (e.g. "imessage", "github").
  `enabled` reflects the current YAML value. `summary` is a short hint shown
  next to the source for context.
  """
  ingest = cfg.get("ingest") or {}
  out: list[tuple[str, bool, str]] = []
  for key, block in ingest.items():
    if not isinstance(block, dict):
      continue
    if "enabled" not in block:
      continue
    enabled = bool(block.get("enabled"))
    summary = _summary_for(key, block)
    out.append((key, enabled, summary))
  out.sort(key=lambda row: row[0])
  return out


def _summary_for(key: str, block: dict) -> str:
  if key == "imessage":
    return str(block.get("chat_db_path", ""))
  if key == "signal":
    return str(block.get("signal_dir", ""))
  if key == "email":
    src_paths = []
    for entry in block.get("sources") or []:
      if isinstance(entry, dict):
        src_paths.append(entry.get("path", ""))
    return ", ".join(p for p in src_paths if p) or "(no sources)"
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


def _rewrite_enabled_flags(config_path: Path, target_state: dict[str, bool]) -> dict[str, bool]:
  """Patch only the `enabled:` lines for the given top-level ingest keys.

  Returns the dict of keys actually changed (key -> new value). Leaves the
  file untouched on disk if no flag actually needs to flip.
  """
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
    if block_start is None:
      continue
    if block_end is None:
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


def _find_block_span(
  lines: list[str],
  *,
  top_level_key: str,
  parent_indent: int = 0,
  search_from: int | None = None,
  search_to: int | None = None,
) -> tuple[int | None, int | None]:
  """Find the inclusive line span for a key:value block in YAML.

  Returns (start_idx, end_idx) where start is the key's own line and end is the
  index past the last child line. None if not found.
  """
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


def _render(rows: list[tuple[str, bool, str]], selected: list[bool], cursor: int, config_path: Path) -> None:
  sys.stdout.write("\033[H\033[2J")
  sys.stdout.write(f"{BOLD}YAAMS Sources{RESET}  {DIM}({config_path}){RESET}\n")
  sys.stdout.write(f"{DIM}up/down or j/k navigate  ·  space toggle  ·  enter apply  ·  q quit{RESET}\n\n")
  for i, ((name, current, summary), desired) in enumerate(zip(rows, selected)):
    cursor_mark = f"{CYAN}{ARROW}{RESET} " if i == cursor else "  "
    box = f"{GREEN}{CHECK}{RESET}" if desired else f"{DIM}{EMPTY}{RESET}"
    indicator = ""
    if desired and not current:
      indicator = f"  {GREEN}← will enable{RESET}"
    elif not desired and current:
      indicator = f"  {RED}← will disable{RESET}"
    summary_text = f"  {DIM}{summary}{RESET}" if summary else ""
    sys.stdout.write(f"{cursor_mark}{box}  {BOLD}{name}{RESET}{indicator}{summary_text}\n")
  pending_on = sum(1 for (_, cur, _), d in zip(rows, selected) if d and not cur)
  pending_off = sum(1 for (_, cur, _), d in zip(rows, selected) if not d and cur)
  sys.stdout.write("\n")
  if pending_on or pending_off:
    parts = []
    if pending_on:
      parts.append(f"{GREEN}{pending_on} to enable{RESET}")
    if pending_off:
      parts.append(f"{RED}{pending_off} to disable{RESET}")
    sys.stdout.write(f"  {', '.join(parts)}  {DIM}— press enter to apply{RESET}\n")
  else:
    sys.stdout.write(f"  {DIM}No changes{RESET}\n")
  sys.stdout.flush()


def _interactive(rows: list[tuple[str, bool, str]], config_path: Path) -> dict[str, bool] | None:
  selected = [r[1] for r in rows]
  cursor = 0
  sys.stdout.write("\033[?25l")
  sys.stdout.flush()
  try:
    while True:
      _render(rows, selected, cursor, config_path)
      key = _read_key()
      if key in ("q", "\x03"):
        return None
      if key in ("k", "\x1b[A"):
        cursor = max(0, cursor - 1)
      elif key in ("j", "\x1b[B"):
        cursor = min(len(rows) - 1, cursor + 1)
      elif key == " ":
        selected[cursor] = not selected[cursor]
      elif key in ("\r", "\n"):
        return {name: desired for (name, _, _), desired in zip(rows, selected)}
  finally:
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


@cli.command("sources")
@config_option
def sources_cmd(config_path: str) -> None:
  """Toggle which ingest sources are enabled. Interactive TUI."""
  resolved = resolve_config_path(config_path)
  cfg = load_config(resolved)
  rows = _enumerate_sources(cfg)
  if not rows:
    click.echo("No toggleable sources found under `ingest:`.")
    return

  if not (sys.stdin.isatty() and sys.stdout.isatty()):
    click.echo("yaams sources requires an interactive TTY.")
    for name, enabled, summary in rows:
      mark = "on " if enabled else "off"
      tail = f"  {summary}" if summary else ""
      click.echo(f"  [{mark}] {name}{tail}")
    return

  result = _interactive(rows, resolved)
  sys.stdout.write("\033[H\033[2J")
  sys.stdout.flush()
  if result is None:
    click.echo("No changes made.")
    return

  changed = _rewrite_enabled_flags(resolved, result)
  if not changed:
    click.echo("No changes made.")
    return
  click.echo(f"Updated {resolved}:")
  for name, value in sorted(changed.items()):
    click.echo(f"  {name}: {'enabled' if value else 'disabled'}")
