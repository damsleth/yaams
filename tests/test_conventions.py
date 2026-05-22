"""Tests for the hugr CLI contract helpers in yaams/conventions.py."""
from __future__ import annotations

import io
import json

from yaams import __version__
from yaams.conventions import (
  EXIT_OK,
  EXIT_PARTIAL,
  EXIT_USER_ERROR,
  DoctorFinding,
  DoctorPayload,
  action_envelope,
  data_error,
  emit_action,
  emit_data_error,
  redact,
  stream_progress,
  stream_result,
  stream_warning,
)


def test_redact_jwt_like():
  jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
  out = redact(f"token={jwt}")
  assert jwt not in out
  assert "<redacted-jwt>" in out


def test_redact_bearer():
  out = redact("Authorization: Bearer abc123def456")
  assert "abc123def456" not in out
  assert "Bearer <redacted>" in out


def test_redact_token_field():
  payload = '{"access_token":"xyz","refresh_token":"qrs","other":"keep"}'
  out = redact(payload)
  assert "xyz" not in out
  assert "qrs" not in out
  assert "keep" in out


def test_redact_body_field():
  payload = '{"body":"secret content here","subject":"ok"}'
  out = redact(payload)
  assert "secret content here" not in out
  assert '"body":"<redacted>"' in out
  assert "ok" in out


def test_redact_handles_non_string():
  out = redact(None)
  assert out == ""


def test_redact_is_idempotent():
  jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
  once = redact(f"x={jwt}")
  twice = redact(once)
  assert once == twice


def test_redaction_sentinel_does_not_leak():
  """Canary sentinel must NEVER appear in redacted output."""
  jwt = "eyJfake." + "CANARY_SECRET_xxxx" + "." + "padding1234"
  out = redact(f"oops Authorization: Bearer {jwt}")
  assert "CANARY_SECRET_xxxx" not in out


def test_action_envelope_shape_success():
  env = action_envelope(command="ingest", ok=True, stats={"items": 42}, duration_ms=123.4)
  assert env["tool"] == "yaams"
  assert env["version"] == __version__
  assert env["command"] == "ingest"
  assert env["ok"] is True
  assert env["duration_ms"] == 123.4
  assert env["stats"] == {"items": 42}
  assert env["warnings"] == []
  assert env["error"] is None


def test_action_envelope_shape_failure():
  env = action_envelope(
    command="ingest",
    ok=False,
    error={"code": "auth_expired", "message": "M365 token expired", "hint": "hugr auth setup"},
  )
  assert env["ok"] is False
  assert env["error"]["code"] == "auth_expired"


def test_emit_action_writes_one_line():
  buf = io.StringIO()
  emit_action(action_envelope(command="init-db", ok=True), stream=buf)
  out = buf.getvalue()
  assert out.endswith("\n")
  assert out.count("\n") == 1
  payload = json.loads(out)
  assert payload["command"] == "init-db"


def test_data_error_shape():
  err = data_error(command="events", code="auth_expired", message="M365 token expired", hint="run setup")
  assert err["tool"] == "yaams"
  assert err["ok"] is False
  assert err["error"]["code"] == "auth_expired"
  assert err["error"]["hint"] == "run setup"
  # No top-level `stats` on data-class envelope.
  assert "stats" not in err


def test_data_error_no_top_level_ok_on_success_documents():
  """Reserved-key contract sanity check.

  This isn't enforcing it on real data documents (that's per-command),
  but the failure envelope MUST itself have `ok` at the top level
  (consumer relies on it as the discriminator).
  """
  err = data_error(command="x", code="c", message="m")
  assert "ok" in err and err["ok"] is False


def test_emit_data_error_one_line():
  buf = io.StringIO()
  emit_data_error(data_error(command="x", code="c", message="m"), stream=buf)
  payload = json.loads(buf.getvalue())
  assert payload["ok"] is False


def test_stream_progress_schema():
  buf = io.StringIO()
  stream_progress(source="imessage", stage="fetch", done=10, total=100, stream=buf)
  payload = json.loads(buf.getvalue())
  assert payload["type"] == "progress"
  assert payload["source"] == "imessage"
  assert payload["stage"] == "fetch"
  assert payload["done"] == 10
  assert payload["total"] == 100
  assert "ts" in payload


def test_stream_warning_schema_and_redaction():
  buf = io.StringIO()
  stream_warning("auth header: Bearer secrettoken", source="signal", stream=buf)
  payload = json.loads(buf.getvalue())
  assert payload["type"] == "warning"
  assert payload["source"] == "signal"
  assert "secrettoken" not in payload["message"]


def test_stream_result_carries_envelope():
  buf = io.StringIO()
  env = action_envelope(command="ingest", ok=True, stats={"items": 5})
  stream_result(env, stream=buf)
  payload = json.loads(buf.getvalue())
  assert payload["type"] == "result"
  assert payload["command"] == "ingest"
  assert payload["stats"]["items"] == 5


def test_doctor_payload_to_dict_minimal():
  payload = DoctorPayload().to_dict()
  assert payload["tool"] == "yaams"
  assert payload["version"] == __version__
  assert payload["findings"] == []


def test_doctor_payload_with_findings():
  d = DoctorPayload(
    config_path="/etc/yaams/config.yaml",
    data_path="/data/yaams.db",
    findings=[
      DoctorFinding(id="spacy_missing", severity="error", message="spaCy NER not installed", hint="yaams setup"),
      DoctorFinding(id="legacy_format", severity="warning", message="config uses legacy key"),
    ],
  )
  payload = d.to_dict()
  assert payload["config_path"] == "/etc/yaams/config.yaml"
  assert len(payload["findings"]) == 2
  assert payload["findings"][0]["severity"] == "error"
  assert payload["findings"][0]["hint"] == "yaams setup"
  assert "hint" not in payload["findings"][1]


def test_doctor_exit_code_error():
  d = DoctorPayload(findings=[DoctorFinding(id="x", severity="error", message="bad")])
  assert d.exit_code() == EXIT_USER_ERROR


def test_doctor_exit_code_clean():
  d = DoctorPayload()
  assert d.exit_code() == EXIT_OK


def test_exit_code_constants_match_spec():
  # Quick sanity that the constants haven't drifted from the spec.
  assert EXIT_OK == 0
  assert EXIT_USER_ERROR == 1
  assert EXIT_PARTIAL == 5
