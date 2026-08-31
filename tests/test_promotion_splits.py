"""Split assignment must be deterministic, exactly-one, and thread-coherent —
a drifting holdout would silently contaminate every promotion eval."""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from promotion_splits import SPLITS, group_key, split_of


def test_split_is_deterministic_and_valid():
  for i in range(200):
    s = split_of("teams", f"thread-{i}", str(i))
    assert s in SPLITS
    assert s == split_of("teams", f"thread-{i}", str(i))


def test_thread_members_share_a_split():
  # Any item in the same (source, thread) must land in the same split,
  # regardless of its own id — that's the leakage guard.
  splits = {split_of("imessage", "chat42", str(item_id)) for item_id in range(50)}
  assert len(splits) == 1


def test_threadless_items_split_independently():
  splits = {split_of("mail", None, str(i)) for i in range(200)}
  assert splits == set(SPLITS)  # enough ids to hit all three


def test_split_fractions_roughly_80_10_10():
  counts = Counter(split_of("mail", None, str(i)) for i in range(10_000))
  assert 0.75 < counts["train"] / 10_000 < 0.85
  assert 0.07 < counts["dev"] / 10_000 < 0.13
  assert 0.07 < counts["holdout"] / 10_000 < 0.13


def test_group_key_shapes():
  assert group_key("teams", "t1", "9") == "teams:t1"
  assert group_key("mail", None, "9") == "item:9"
  assert group_key("mail", "", "9") == "item:9"
