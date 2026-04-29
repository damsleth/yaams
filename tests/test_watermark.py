from __future__ import annotations

from datetime import UTC, datetime

from yaams.db import open_db
from yaams.schema import init_schema
from yaams.watermark import get_watermark, update_watermark


def test_watermark_round_trip(tmp_path):
  conn = open_db(tmp_path / "yaams.db")
  init_schema(conn, use_vec=False)
  timestamp = datetime(2026, 4, 29, 12, 30, tzinfo=UTC)

  assert get_watermark(conn, "email") is None

  with conn:
    update_watermark(conn, "email", timestamp)

  assert get_watermark(conn, "email") == timestamp

