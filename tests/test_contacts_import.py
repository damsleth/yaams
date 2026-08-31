"""Tests for the macOS Contacts importer.

The mapping layer is pure, so it is tested directly. The fetch layer is
exercised against a synthetic sqlite store built to the AddressBook shape,
which also pins the column names the private schema has to keep providing.
"""

from __future__ import annotations

import sqlite3

from yaams.contacts_import import contacts_to_entries, fetch_contacts, normalize_phone


def test_normalize_phone_accepts_e164_and_national():
  # Already E.164, in the format iMessage ingest writes.
  assert normalize_phone("+4748445855") == "+4748445855"
  # Address-book spacing is cosmetic.
  assert normalize_phone("+47 913 27 328") == "+4791327328"
  # Bare Norwegian national number gains the default country code.
  assert normalize_phone("484 45 855") == "+4748445855"
  assert normalize_phone("3377 4567") == "+4733774567"
  # International prefix form.
  assert normalize_phone("004748445855") == "+4748445855"


def test_normalize_phone_drops_what_it_cannot_be_sure_of():
  """A wrong alias is worse than a missing one: the tagger's alias index is
  last-write-wins, so a guessed number silently misroutes a whole thread."""
  assert normalize_phone("1989") is None       # short code, not a number
  assert normalize_phone("12345") is None      # too short for +47
  assert normalize_phone("") is None
  assert normalize_phone("   ") is None
  assert normalize_phone("AkershusRK") is None
  assert normalize_phone("+47") is None        # country code alone
  # Non-NO default has no national length configured, so bare digits stay out.
  assert normalize_phone("5551234", default_cc="+1") is None


def test_contacts_to_entries_builds_aliases_in_sender_format():
  contacts = [{
    "name": "Emilie Athene Damsleth", "nickname": "", "org": "",
    "phones": ["+4748445855", "484 45 855"],       # same number, two formats
    "emails": ["Emilie.Athene@icloud.com"],
    "is_person": True,
  }]
  entries, collisions = contacts_to_entries(contacts)
  assert collisions == []
  assert entries[0]["canonical"] == "Emilie Athene Damsleth"
  assert entries[0]["type"] == "person"
  # Deduped to one number, email lowercased to match items.sender.
  assert entries[0]["aliases"] == ["+4748445855", "emilie.athene@icloud.com"]


def test_shared_number_folds_the_weaker_card_into_the_better_one():
  """The exact failure this importer exists to prevent. Two cards on one
  number are one person, so the weaker name becomes an alias of the stronger
  rather than a rival entity that could claim the number."""
  contacts = [
    {"name": "Neke Nekeman", "phones": ["484 45 855"], "emails": [], "is_person": True},
    {"name": "Emilie Athene Damsleth", "phones": ["+4748445855"], "emails": [], "is_person": True},
  ]
  entries, notes = contacts_to_entries(contacts)
  # One person, not two, and the real name won despite being listed second.
  assert len(entries) == 1
  assert entries[0]["canonical"] == "Emilie Athene Damsleth"
  assert entries[0]["aliases"] == ["+4748445855", "Neke Nekeman"]
  assert len(notes) == 1 and "folded 'Neke Nekeman' into 'Emilie Athene Damsleth'" in notes[0]


def test_surname_beats_a_first_name_only_nickname_card():
  """'Mamma' and 'Nina Cathrine Damsleth' share a number. Ranking on the
  structured surname stops the nickname owning it, and keeps 'Mamma' usable
  as a way of referring to her."""
  contacts = [
    {"name": "Mamma", "phones": ["+4794324297"], "emails": [], "is_person": True, "has_last": False},
    {"name": "Nina Cathrine Damsleth", "phones": ["+4794324297"], "emails": [],
     "is_person": True, "has_last": True},
  ]
  entries, notes = contacts_to_entries(contacts)
  assert len(entries) == 1
  assert entries[0]["canonical"] == "Nina Cathrine Damsleth"
  assert "Mamma" in entries[0]["aliases"]
  assert notes


def test_company_card_becomes_org_entity():
  contacts = [{"name": "HRS Sor", "org": "", "phones": ["+4751646000"],
               "emails": [], "is_person": False}]
  entries, _ = contacts_to_entries(contacts)
  assert entries[0]["type"] == "org"


def _make_store(path):
  conn = sqlite3.connect(path)
  conn.executescript(
    """
    CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT,
      ZMIDDLENAME TEXT, ZLASTNAME TEXT, ZNICKNAME TEXT, ZORGANIZATION TEXT);
    CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
      ZFULLNUMBER TEXT);
    CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
      ZADDRESS TEXT);
    INSERT INTO ZABCDRECORD VALUES (1,'Emilie','Athene','Damsleth',NULL,NULL);
    INSERT INTO ZABCDRECORD VALUES (2,NULL,NULL,NULL,NULL,'HRS Sor');
    INSERT INTO ZABCDPHONENUMBER VALUES (1,1,'+4748445855');
    INSERT INTO ZABCDPHONENUMBER VALUES (2,2,'+4751646000');
    INSERT INTO ZABCDEMAILADDRESS VALUES (1,1,'emilie.athene@icloud.com');
    """
  )
  conn.commit()
  conn.close()


def test_fetch_contacts_reads_a_store(tmp_path):
  db = tmp_path / "AddressBook-v22.abcddb"
  _make_store(db)
  contacts, warnings = fetch_contacts((str(db),))
  assert warnings == []
  by_name = {c["name"]: c for c in contacts}
  assert by_name["Emilie Athene Damsleth"]["phones"] == ["+4748445855"]
  assert by_name["Emilie Athene Damsleth"]["is_person"] is True
  # A card with only an organization is not a person.
  assert by_name["HRS Sor"]["is_person"] is False


def test_missing_store_warns_instead_of_raising(tmp_path):
  contacts, warnings = fetch_contacts((str(tmp_path / "nope-*.abcddb"),))
  assert contacts == []
  assert warnings and "no AddressBook store" in warnings[0]


def test_unreadable_store_warns_and_keeps_going(tmp_path):
  """The AddressBook schema is private to macOS; an OS upgrade that renames a
  column must degrade to a warning, not take the whole import down."""
  good = tmp_path / "AddressBook-v22.abcddb"
  _make_store(good)
  bad = tmp_path / "broken" / "AddressBook-v22.abcddb"
  bad.parent.mkdir()
  sqlite3.connect(bad).close()  # valid sqlite, no AddressBook tables
  contacts, warnings = fetch_contacts((str(good), str(bad)))
  assert [c["name"] for c in contacts if c["name"].startswith("Emilie")]
  assert any("query failed" in w for w in warnings)
