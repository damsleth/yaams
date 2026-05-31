"""Display-time formatting helpers for retrieval results.

Consolidation summaries (`consolidations.summary`) are stored in a verbose
format that restates the source, date, and participants in a header line and
re-prints those on every message line:

  {source} session {YYYY-MM-DD} with {p1, p2, ...}:
  [YYYY-MM-DD HH:MM] sender@long.email: content
  [YYYY-MM-DD HH:MM] sender@long.email: more content
  ...

That's redundant when the caller already prints source/date/participants in a
metadata line above the snippet. These helpers strip that redundancy at
display time without touching stored data.
"""

from __future__ import annotations

import re

_HEADER_RE = re.compile(
  r"^\s*[\w_]+ session [\d\-]+ (?:to [\d\-]+ )?with [^\n:]+:\s*",
  re.MULTILINE,
)
_LINE_RE = re.compile(
  r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\] ([^:]+): (.*)$"
)

DEFAULT_SNIPPET_CHARS = 480


def short_sender(raw: str) -> str:
  """`fredrik.nordmoen@rodekors.org` → `fredrik.nordmoen`. Phone numbers
  and bare names pass through unchanged."""
  if not raw:
    return ""
  at = raw.find("@")
  return raw[:at] if at > 0 else raw


def render_consolidation_snippet(
  summary: str,
  *,
  multiline: bool = True,
  max_chars: int = DEFAULT_SNIPPET_CHARS,
  include_time: bool = True,
) -> str:
  """Strip the redundant header and per-line prefixes from a consolidation
  summary.

  Args:
    summary: raw `consolidations.summary` content.
    multiline: if True, return one message per line (`HH:MM sender: text`);
      if False, space-join everything (matches the historic look).
    max_chars: truncate the final string with an ellipsis.
    include_time: include the `HH:MM` prefix per message.
  """
  if not summary:
    return ""

  body = _HEADER_RE.sub("", summary, count=1)

  messages: list[tuple[str, str, str]] = []  # (time, sender, content)
  for raw_line in body.splitlines():
    line = raw_line.strip()
    if not line:
      continue
    m = _LINE_RE.match(line)
    if m is None:
      messages.append(("", "", line))
      continue
    _date, time, sender, content = m.groups()
    messages.append((time, short_sender(sender.strip()), content.strip()))

  folded: list[tuple[str, str, str]] = []
  for time, sender, content in messages:
    if folded and sender and folded[-1][1] == sender:
      prev_time, prev_sender, prev_content = folded[-1]
      folded[-1] = (prev_time, prev_sender, f"{prev_content} · {content}")
    else:
      folded.append((time, sender, content))

  if multiline:
    rendered_lines = [
      _format_message_line(t, s, c, include_time=include_time)
      for t, s, c in folded
    ]
    out = "\n".join(rendered_lines)
  else:
    parts = [
      _format_message_line(t, s, c, include_time=include_time)
      for t, s, c in folded
    ]
    out = " ".join(parts)

  if len(out) > max_chars:
    out = out[: max_chars - 1].rstrip() + "…"
  return out


def _format_message_line(
  time: str, sender: str, content: str, *, include_time: bool
) -> str:
  prefix_parts: list[str] = []
  if include_time and time:
    prefix_parts.append(time)
  if sender:
    prefix_parts.append(f"{sender}:")
  if not prefix_parts:
    return content
  return f"{' '.join(prefix_parts)} {content}".rstrip()


def short_participants(
  participants: list[str], *, limit: int = 5
) -> str:
  """Format a participants list for a one-line header: shortens emails,
  drops the user's own address when it would just say "with me", caps at
  `limit` entries with a `+N` overflow tag."""
  if not participants:
    return ""
  shortened = [short_sender(p) for p in participants if p]
  if not shortened:
    return ""
  head = shortened[:limit]
  out = ", ".join(head)
  extra = len(shortened) - len(head)
  if extra > 0:
    out += f" +{extra}"
  return out
