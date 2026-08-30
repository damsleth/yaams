"""Contract fixture test for the YAAMS -> ledger candidate bundle (contract v1).

cognitive-ledger owns the schema (its docs/contracts/); this repo keeps a
byte-identical copy under docs/contracts/ as the contract fixture, per the
no-cross-repo-imports rule. If the contract evolves, update both copies in the
same change set.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

CONTRACTS = Path(__file__).resolve().parent.parent / "docs" / "contracts"
SCHEMA = json.loads((CONTRACTS / "candidate_bundle.schema.json").read_text())
EXAMPLE = json.loads((CONTRACTS / "candidate_bundle.example.json").read_text())


def _validate(instance) -> None:
  Draft202012Validator(SCHEMA).validate(instance)


def test_schema_is_valid_draft_2020_12():
  Draft202012Validator.check_schema(SCHEMA)


def test_example_bundle_validates():
  _validate(EXAMPLE)


def test_unknown_fields_are_tolerated():
  bundle = json.loads(json.dumps(EXAMPLE))
  bundle["future_field"] = {"anything": True}
  bundle["candidates"][0]["future_axis"] = 0.5
  _validate(bundle)


@pytest.mark.parametrize(
  "mutation",
  [
    lambda b: b.pop("run_id"),
    lambda b: b.__setitem__("bundle_schema_version", 2),
    lambda b: b["candidates"][0].__setitem__("proposed_action", "DELETE"),
    lambda b: b["candidates"][0].__setitem__("source_item_ids", []),
    lambda b: b["candidates"][0].pop("note"),
    lambda b: b["candidates"][1].pop("target_path"),
  ],
)
def test_invalid_bundles_fail_closed(mutation):
  bundle = json.loads(json.dumps(EXAMPLE))
  mutation(bundle)
  with pytest.raises(ValidationError):
    _validate(bundle)
