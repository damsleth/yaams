from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Iterator, Optional, Protocol

from yaams.conventions import EXIT_AUTH
from yaams.time import utc_now

logger = logging.getLogger("yaams.ingest")

# owa-tools' owa_core.errors.ExitCode.AUTH_EXPIRED. owa-piggy itself exits
# EXIT_AUTH (3) for the same condition.
RC_OWA_AUTH_EXPIRED = 11

# Profiles whose broker session is dead for the rest of this ingest run. Once
# owa-piggy says interactive sign-in is needed, every further call for that
# profile is another Edge launch that fails the same way, and sources fetch
# concurrently, so adapters check this before every subprocess call rather than
# the runner checking once per source. Plain set ops are atomic under the GIL.
_dead_profiles: set[str] = set()


class ProfileAuthDead(RuntimeError):
  """A profile's owa-piggy session needs interactive sign-in; stop using it."""

  def __init__(self, profile: str, detail: str = ""):
    msg = (
      f"owa-piggy profile '{profile}' needs interactive sign-in "
      f"(run: owa-piggy setup --profile {profile}); skipped for the rest of this run"
    )
    super().__init__(f"{msg}: {detail}" if detail else msg)
    self.profile = profile


def reset_dead_profiles() -> None:
  _dead_profiles.clear()


def check_profile_alive(profile: str) -> None:
  if profile in _dead_profiles:
    raise ProfileAuthDead(profile, "marked dead earlier this run")


def raise_if_auth_dead(profile: str, tool: str, returncode: int, stderr: str = "") -> None:
  """Mark `profile` dead and raise when `tool`'s exit code means dead auth.

  An auth failure is never a legitimate empty answer, so it must not be
  swallowed into ``[]``: the run summary would report a successful 0-item fetch
  and the real cause would only show up as a log warning.
  """
  dead_rc = EXIT_AUTH if tool == "owa-piggy" else RC_OWA_AUTH_EXPIRED
  if returncode != dead_rc:
    return
  exc = ProfileAuthDead(profile, f"{tool} rc={returncode}: {stderr.strip() or 'no stderr'}")
  if profile not in _dead_profiles:
    _dead_profiles.add(profile)
    logger.error("%s", exc)
  raise exc


@dataclass(frozen=True)
class Item:
  id: str
  source: str
  source_id: str
  timestamp: datetime
  sender: str
  recipients: list[str]
  content: str
  subject: Optional[str] = None
  thread_id: Optional[str] = None
  lang: Optional[str] = None
  raw_metadata: dict = field(default_factory=dict)
  ingested_at: datetime = field(default_factory=utc_now)
  # True when ``timestamp`` was a fallback (e.g. file mtime) rather than a real
  # date signal from the content. Recency-sorted retrieval excludes these so
  # undated items don't masquerade as the freshest in the corpus.
  timestamp_inferred: bool = False


class Adapter(Protocol):
  def extract(self, since: datetime) -> Iterator[Item]:
    ...


def hash_id(source: str, source_id: str) -> str:
  # Deterministic, content-stable id: the same logical item always hashes the
  # same, and mutable sources encode their revision in source_id so a change is
  # a new id, not a rewrite. See AGENTS.md "Raw-store invariants".
  return sha256(f"{source}:{source_id}".encode("utf-8")).hexdigest()

