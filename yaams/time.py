from __future__ import annotations

from datetime import UTC, datetime


def ensure_utc(value: datetime) -> datetime:
  if value.tzinfo is None:
    return value.replace(tzinfo=UTC)
  return value.astimezone(UTC)


def parse_iso_datetime(value: str) -> datetime:
  normalized = value.strip()
  if normalized.endswith("Z"):
    normalized = normalized[:-1] + "+00:00"
  return ensure_utc(datetime.fromisoformat(normalized))


def utc_now() -> datetime:
  return datetime.now(UTC)


def to_local(value: datetime) -> datetime:
  if value.tzinfo is None:
    value = value.replace(tzinfo=UTC)
  return value.astimezone()


def format_local(value: datetime, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
  return to_local(value).strftime(fmt)

