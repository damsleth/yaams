from __future__ import annotations

import pytest

from yaams.config import load_config


def _write(tmp_path, body: str):
  cfg = tmp_path / "config.yaml"
  cfg.write_text(body)
  return cfg


def test_valid_numeric_knobs_pass(tmp_path):
  cfg = _write(
    tmp_path,
    "db_path: ~/yaams/data.db\n"
    "embed:\n"
    "  batch_size: 32\n"
    "  dimension: 1024\n"
    "synth:\n"
    "  timeout: 90.0\n"
    "ingest:\n"
    "  mail:\n"
    "    chunk_days: 30\n",
  )
  data = load_config(cfg)
  assert data["embed"]["batch_size"] == 32


def test_non_numeric_knob_rejected(tmp_path):
  cfg = _write(
    tmp_path,
    "db_path: ~/yaams/data.db\nembed:\n  batch_size: fast\n",
  )
  with pytest.raises(ValueError, match="embed.batch_size"):
    load_config(cfg)


def test_non_positive_knob_rejected(tmp_path):
  cfg = _write(
    tmp_path,
    "db_path: ~/yaams/data.db\ningest:\n  mail:\n    chunk_days: 0\n",
  )
  with pytest.raises(ValueError, match="must be positive"):
    load_config(cfg)


def test_bool_knob_rejected(tmp_path):
  cfg = _write(
    tmp_path,
    "db_path: ~/yaams/data.db\nembed:\n  dimension: true\n",
  )
  with pytest.raises(ValueError, match="embed.dimension"):
    load_config(cfg)


def test_section_must_be_mapping(tmp_path):
  cfg = _write(tmp_path, "db_path: ~/yaams/data.db\nembed: not-a-mapping\n")
  with pytest.raises(ValueError, match="must be a mapping"):
    load_config(cfg)


def test_missing_sections_are_fine(tmp_path):
  cfg = _write(tmp_path, "db_path: ~/yaams/data.db\n")
  data = load_config(cfg)
  assert data["db_path"] == "~/yaams/data.db"
