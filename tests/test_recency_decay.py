from datetime import datetime, timedelta, timezone

from yaams.retrieve.hybrid import _recency_factor

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)


def test_missing_timestamp_is_neutral():
  assert _recency_factor(None, NOW, 90.0, 0.9) == 1.0


def test_fresh_item_near_one():
  assert _recency_factor(NOW, NOW, 90.0, 0.9) == 1.0


def test_future_timestamp_clamps_to_one():
  assert _recency_factor(NOW + timedelta(days=5), NOW, 90.0, 0.9) == 1.0


def test_old_item_hits_floor():
  assert _recency_factor(NOW - timedelta(days=3650), NOW, 90.0, 0.9) == 0.9


def test_monotonic_decrease_until_floor():
  ages = [0, 1, 5, 9, 30]
  factors = [_recency_factor(NOW - timedelta(days=a), NOW, 90.0, 0.0) for a in ages]
  assert factors == sorted(factors, reverse=True)
  assert len(set(factors)) == len(factors)


def test_config_knob_is_opt_in_and_parsed():
  from yaams.cli.query import apply_recency_decay_config
  from yaams.retrieve.hybrid import HybridQueryConfig

  qcfg = HybridQueryConfig()
  apply_recency_decay_config(qcfg, {"retrieve": {}})
  assert qcfg.recency_decay_tau_days == 0.0  # default off

  apply_recency_decay_config(qcfg, {"retrieve": {"recency_decay": {"tau_days": 90, "floor": 0.5}}})
  assert (qcfg.recency_decay_tau_days, qcfg.recency_decay_floor) == (90.0, 0.5)
