"""Import macOS Contacts into the entity dictionary.

Contacts are *reference entities*, not timestamped events, so like the M365
people in :mod:`yaams.people_import` they belong in the entity dictionary that
``seed_entities`` builds and ``EntityTagger`` reads, not in the ``items``
firehose. This module is that module's local-address-book sibling and reuses
its merge layer verbatim; only the fetch and the mapper differ.

Why it exists: iMessage rows land in ``items.sender`` as bare E.164 numbers
(``+4748445855``) or bare addresses. With no dictionary entry, the tagger has
nothing to resolve them to, and a downstream summariser trying to be helpful
will guess a name from surrounding chatter. That is not hypothetical: on
2026-08-27 a consolidation run pulled "Sylvia" from an unrelated thread onto
Kim's daughter's unnamed number and wrote a false open loop off the back of it.
Seeding the address book closes that gap at the source.

Reading strategy: the AddressBook sqlite stores are opened read-only via
``immutable=1``. AppleScript (``tell application "Contacts"``) is the blessed
API but it launches the app, blocks on a TCC prompt, and took over two minutes
to return a bare count on a ~700-contact book, so it is unusable for a batch
import. The schema below is private to macOS and can change between releases;
:func:`fetch_contacts` therefore degrades to a warning per unreadable store
rather than failing the run.

Phone numbers are normalised to E.164 because that is exactly what iMessage
ingest writes. A number that cannot be normalised confidently is dropped, not
guessed: a wrong alias is worse than a missing one, since the tagger's alias
index is last-write-wins and a bad entry silently misroutes every message from
that number.
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3

# Default AddressBook stores: the local one plus every synced account source.
DEFAULT_DB_GLOBS = (
  "~/Library/Application Support/AddressBook/AddressBook-v22.abcddb",
  "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb",
)

# One row per (contact, identifier). Names are assembled here rather than in
# Python so the whole book comes back in a single scan per store.
_QUERY = """
SELECT r.ZFIRSTNAME, r.ZMIDDLENAME, r.ZLASTNAME, r.ZNICKNAME, r.ZORGANIZATION,
       p.ZFULLNUMBER, e.ZADDRESS
FROM ZABCDRECORD r
LEFT JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
LEFT JOIN ZABCDEMAILADDRESS e ON e.ZOWNER = r.Z_PK
"""

_DIGITS = re.compile(r"[^\d+]")


def normalize_phone(raw: str, *, default_cc: str = "+47") -> str | None:
  """Normalise an address-book phone string to E.164, or None if unsure.

  ``default_cc`` is applied only to bare national numbers of the expected
  length for that country, so a 5-digit short code or a truncated entry is
  dropped instead of being turned into a plausible-looking wrong number.
  """
  if not raw:
    return None
  value = _DIGITS.sub("", raw.strip())
  if not value:
    return None
  if value.startswith("00"):
    value = "+" + value[2:]
  if value.startswith("+"):
    # A '+' anywhere but the front is a data-entry artefact, not a number.
    digits = value[1:]
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
      return None
    return "+" + digits
  if not value.isdigit():
    return None
  # National form. Only accept the length the default country actually uses;
  # NO is 8 digits. Anything else (short codes, extensions) stays out.
  national_len = {"+47": 8}.get(default_cc)
  if national_len is not None and len(value) == national_len:
    return default_cc + value
  return None


def _full_name(first: str | None, middle: str | None, last: str | None) -> str:
  return " ".join(part.strip() for part in (first, middle, last) if part and part.strip())


def fetch_contacts(db_globs: tuple[str, ...] = DEFAULT_DB_GLOBS) -> tuple[list[dict], list[str]]:
  """Read every AddressBook store, returning (contacts, warnings).

  Each contact is ``{"name", "nickname", "org", "phones", "emails"}``. The same
  person appearing in several stores (iCloud plus a local copy, say) comes back
  more than once; :func:`contacts_to_entries` folds them by name.
  """
  contacts: dict[tuple, dict] = {}
  warnings: list[str] = []
  paths: list[str] = []
  for pattern in db_globs:
    paths.extend(sorted(glob.glob(os.path.expanduser(pattern))))
  if not paths:
    warnings.append("no AddressBook store found (is this macOS?)")
    return [], warnings

  for path in paths:
    try:
      conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    except sqlite3.Error as exc:
      warnings.append(f"{os.path.basename(os.path.dirname(path))}: cannot open ({exc})")
      continue
    try:
      rows = conn.execute(_QUERY).fetchall()
    except sqlite3.Error as exc:
      # Private schema: a macOS upgrade renaming a column lands here.
      warnings.append(f"{os.path.basename(os.path.dirname(path))}: query failed ({exc})")
      continue
    finally:
      conn.close()

    for first, middle, last, nick, org, phone, email in rows:
      name = _full_name(first, middle, last)
      # A company card has no person name; the org becomes the entity.
      canonical = name or (org or "").strip()
      if not canonical:
        continue
      key = (canonical.casefold(), bool(name))
      entry = contacts.setdefault(
        key,
        {"name": canonical, "nickname": (nick or "").strip(),
         "org": (org or "").strip() if name else "", "phones": [], "emails": [],
         "is_person": bool(name), "has_last": bool(last and last.strip())},
      )
      if phone:
        entry["phones"].append(phone)
      if email:
        entry["emails"].append(email)
  return list(contacts.values()), warnings


def _card_rank(contact: dict) -> tuple:
  """Sort key deciding which card owns a shared identifier. Lower sorts first.

  Address books accumulate a real card plus nickname cards for the same person
  ("Nina Cathrine Damsleth" and "Mamma" on one number). Whichever card is seen
  first is arbitrary, and picking arbitrarily is how a number ends up resolving
  to a joke name. Rank instead, most reliable signal first:

  1. a structured surname beats a first-name-only card ("Mamma", "Farfar");
  2. a richer card (more phones and addresses) beats a sparse one;
  3. more name parts, so "Emilie Athene Damsleth" beats "Neke Nekeman";
  4. the name itself, purely so the result is deterministic.

  This is a heuristic and it will occasionally lose: a descriptive card like
  "Cecilie Emilie Sin Venn" outscores "Cecilie Westfal-Larsen" on part count.
  Every such fold is reported, and `yaams entities merge` is the fix.
  """
  identifiers = len(contact.get("phones") or []) + len(contact.get("emails") or [])
  parts = len(contact["name"].split())
  return (not contact.get("has_last", False), -identifiers, -parts, contact["name"].casefold())


def contacts_to_entries(
  contacts: list[dict], *, etype: str = "person", org_type: str = "org",
  default_cc: str = "+47",
) -> tuple[list[dict], list[str]]:
  """Map contacts to ``{canonical, type, aliases}`` entries.

  Aliases are normalised phone numbers and lowercased emails, which is exactly
  the form iMessage ingest writes into ``items.sender``.

  Cards sharing an identifier are the same person, so the lower-ranked card is
  folded into the winner **with its name kept as an alias** rather than left as
  a rival entity. That is the point of the fold: "Mamma" becomes a way of
  referring to Nina instead of a second person who owns her phone number.
  Returns ``(entries, notes)``, where notes describe every fold performed.
  """
  entries: list[dict] = []
  by_name: dict[str, dict] = {}
  owner: dict[str, str] = {}
  notes: list[str] = []

  def identifiers_of(contact: dict) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in contact.get("phones") or []:
      number = normalize_phone(raw, default_cc=default_cc)
      if number and number not in seen:
        seen.add(number)
        out.append(number)
    for raw in contact.get("emails") or []:
      address = (raw or "").strip().casefold()
      if address and "@" in address and address not in seen:
        seen.add(address)
        out.append(address)
    return out

  for contact in sorted(contacts, key=_card_rank):
    canonical = contact["name"].strip()
    if not canonical:
      continue
    ids = identifiers_of(contact)
    # Does this card already belong to someone who owns one of its identifiers?
    held = next((owner[i.casefold()] for i in ids if i.casefold() in owner), None)

    if held and held.casefold() != canonical.casefold():
      target = by_name[held.casefold()]
      aliases = target.setdefault("aliases", [])
      taken = {a.casefold() for a in aliases} | {held.casefold()}
      added = []
      for value in [canonical, *ids]:
        if value.casefold() in taken:
          continue
        if value.casefold() in owner:
          continue
        taken.add(value.casefold())
        owner[value.casefold()] = held
        aliases.append(value)
        added.append(value)
      notes.append(f"folded '{canonical}' into '{held}' (shared identifier)"
                   + (f"; +{len(added)} alias(es)" if added else ""))
      continue

    entry: dict = {
      "canonical": canonical,
      "type": etype if contact.get("is_person", True) else org_type,
    }
    aliases: list[str] = []
    taken = {canonical.casefold()}
    nickname = (contact.get("nickname") or "").strip()
    for value in [*ids, nickname]:
      if not value or value.casefold() in taken or value.casefold() in owner:
        continue
      taken.add(value.casefold())
      owner[value.casefold()] = canonical
      aliases.append(value)
    if aliases:
      entry["aliases"] = aliases
    entries.append(entry)
    by_name[canonical.casefold()] = entry
  return entries, notes
