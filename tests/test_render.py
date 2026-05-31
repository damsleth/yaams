from yaams.render import (
  render_consolidation_snippet,
  short_participants,
  short_sender,
)


def test_short_sender_strips_email_domain():
  assert short_sender("fredrik.nordmoen@rodekors.org") == "fredrik.nordmoen"
  assert short_sender("+4790819617") == "+4790819617"
  assert short_sender("me") == "me"
  assert short_sender("") == ""


def test_render_strips_header_and_per_line_prefixes():
  raw = (
    "teams_brkh session 2026-02-07 with fredrik.nordmoen@rodekors.org, carl.damsleth@rodekors.org:\n"
    "[2026-02-07 19:54] fredrik.nordmoen@rodekors.org: Takk for evalueringen\n"
    "[2026-02-07 19:55] carl.damsleth@rodekors.org: Bra, glad du fikk bruk for den"
  )
  out = render_consolidation_snippet(raw, multiline=True, max_chars=400)
  assert "session 2026" not in out
  assert "@rodekors.org" not in out
  assert "2026-02-07" not in out
  assert out == (
    "19:54 fredrik.nordmoen: Takk for evalueringen\n"
    "19:55 carl.damsleth: Bra, glad du fikk bruk for den"
  )


def test_render_folds_consecutive_same_sender():
  raw = (
    "imessage session 2026-05-01 with me, +4790802229:\n"
    "[2026-05-01 10:00] +4790802229: a\n"
    "[2026-05-01 10:01] +4790802229: b\n"
    "[2026-05-01 10:02] +4790802229: c\n"
    "[2026-05-01 10:03] me: ok"
  )
  out = render_consolidation_snippet(raw, multiline=True, max_chars=400)
  assert out == (
    "10:00 +4790802229: a · b · c\n"
    "10:03 me: ok"
  )


def test_render_truncates_with_ellipsis():
  raw = (
    "x session 2026-01-01 with a@b.c:\n"
    "[2026-01-01 09:00] a@b.c: " + "word " * 200
  )
  out = render_consolidation_snippet(raw, multiline=True, max_chars=80)
  assert len(out) <= 80
  assert out.endswith("…")


def test_render_handles_date_range_header():
  raw = (
    "teams_crayon session 2026-01-15 to 2026-01-16 with a@b.c, d@e.f:\n"
    "[2026-01-15 12:10] a@b.c: hi"
  )
  out = render_consolidation_snippet(raw, multiline=True)
  assert "session" not in out
  assert "12:10 a: hi" == out


def test_render_flat_joins_with_spaces():
  raw = (
    "x session 2026-01-01 with a@b.c, d@e.f:\n"
    "[2026-01-01 09:00] a@b.c: hello\n"
    "[2026-01-01 09:01] d@e.f: world"
  )
  out = render_consolidation_snippet(raw, multiline=False)
  assert "\n" not in out
  assert out == "09:00 a: hello 09:01 d: world"


def test_render_empty_summary():
  assert render_consolidation_snippet("") == ""
  assert render_consolidation_snippet(None) == ""  # type: ignore[arg-type]


def test_render_passes_unparseable_lines_through():
  raw = (
    "x session 2026-01-01 with a@b.c:\n"
    "this line has no bracket prefix\n"
    "[2026-01-01 09:00] a@b.c: normal"
  )
  out = render_consolidation_snippet(raw, multiline=True)
  assert "this line has no bracket prefix" in out
  assert "09:00 a: normal" in out


def test_short_participants_caps_and_overflows():
  assert short_participants(["a@b.c", "d@e.f"]) == "a, d"
  assert short_participants([]) == ""
  result = short_participants(
    ["a@b.c", "d@e.f", "g@h.i", "j@k.l", "m@n.o", "p@q.r", "s@t.u"], limit=5
  )
  assert result == "a, d, g, j, m +2"
