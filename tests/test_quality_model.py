"""Model junk pass on ingest: band + gold protection + threshold, rendering contract,
and that a missing model server never fails the ingest."""
from __future__ import annotations

from test_quality import T0, _item, _open_db, _reason, _store

from yaams import jev, quality
from yaams.quality import MODEL_REASON, annotate_model, annotate_on_ingest, junk_block


def _rows(conn):
  items = [
    _item("imessage", 1, "Møte med Anders kl 14 torsdag", ts=T0),
    _item("imessage", 2, "ok takk, høres bra ut 👍", ts=T0.replace(minute=1)),
    _item("imessage", 3, "gold answer row, keep me", ts=T0.replace(minute=2)),
    _item("imessage", 4, "this message is longer than the thirty-nine char band", ts=T0.replace(minute=3)),
    _item("calendar", 5, "ok takk, høres bra ut 👍", ts=T0.replace(minute=4)),
  ]
  _store(conn, items)
  conn.execute("PRAGMA foreign_keys = OFF")
  conn.execute("INSERT INTO query_feedback (query_id, kind, result_id, ts) VALUES ('q', 'hit', ?, 'x')",
               (items[2].id,))
  return items


def test_labels_band_rows_above_threshold_and_never_gold(monkeypatch):
  conn = _open_db()
  meeting, ack, gold, long_, cal = _rows(conn)
  seen = {}

  def fake_noul(state, texts, criterion, **kw):
    seen.update(texts=texts, kw=kw)
    target = {i: t.split("\nTARGET: ", 1)[1].split("\n", 1)[0] for i, t in texts.items()}
    return {i: (0.9 if "takk" in tg or "gold" in tg else 0.1) for i, tg in target.items()}

  monkeypatch.setattr(quality, "_serve", lambda url, cmd: None)
  monkeypatch.setattr(jev, "noul", fake_noul)
  stats = annotate_model(conn, {"threshold": 0.73})
  assert set(seen["texts"]) == {meeting.id, ack.id}  # band + messaging only, gold excluded
  assert seen["kw"]["cache_model"] == "jeff-junk-v2"
  assert stats[MODEL_REASON] == 1
  assert _reason(conn, ack) == MODEL_REASON
  assert _reason(conn, meeting) is None and _reason(conn, gold) is None
  assert _reason(conn, long_) is None and _reason(conn, cal) is None


def test_rendering_is_the_training_contract():
  conn = _open_db()
  meeting, ack, *_ = _rows(conn)
  row = dict(conn.execute("SELECT id, source, thread_id, timestamp, content FROM items WHERE id=?",
                          (ack.id,)).fetchone())
  assert junk_block(conn, row) == (
    "[imessage] prev: 'Møte med Anders kl 14 torsdag'\n"
    "TARGET: 'ok takk, høres bra ut 👍'\n"
    "next: 'gold answer row, keep me'"
  )


def test_missing_server_is_a_note_not_a_failure():
  conn = _open_db()
  _rows(conn)
  out = annotate_on_ingest(conn, {"quality": {"annotate_on_ingest": True, "junk_model": {
    "enabled": True, "url": "http://127.0.0.1:9/v1/systemone"}}})
  assert out["ran"] and "skipped" in out["model"]["note"]


def test_server_starts_only_for_uncached_rows(monkeypatch):
  conn = _open_db()
  _rows(conn)
  served = []
  cache: dict[str, float] = {}

  def fake_noul(state, texts, criterion, cache_only=False, **kw):
    if cache_only:
      return {i: cache[i] for i in texts if i in cache}
    cache.update({i: 0.1 for i in texts})
    return dict(cache)

  monkeypatch.setattr(quality, "_serve", lambda url, cmd: served.append(1))
  monkeypatch.setattr(jev, "noul", fake_noul)
  first = annotate_model(conn, {"threshold": 0.73})
  second = annotate_model(conn, {"threshold": 0.73})
  assert len(served) == 1  # the second run is all cache hits: no server
  assert first["cached"] == 0 and second["cached"] == second["scored"] == 2
  assert second["server_started"] is False


def test_off_by_default():
  assert annotate_on_ingest(_open_db(), {}) is None
