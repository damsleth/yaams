# Fixture Strategy

Tests must use synthetic data only.

## Principles

- Do not commit real messages, emails, contacts, Mail exports, or database fragments.
- Keep fixtures small and human-readable when possible.
- Prefer programmatically created SQLite databases and email messages.
- Include Norwegian and English text where behavior depends on multilingual handling.

## Current Fixtures

The current tests build fixtures in temporary directories:

- synthetic `EmailMessage` objects
- synthetic `.emlx` files
- synthetic iMessage-compatible SQLite `chat.db` files
- in-memory entity dictionaries

## Future Fixtures

When Phase B starts, add synthetic retrieval fixtures with:

- multiple items sharing entities
- date-range examples
- first-occurrence examples
- conflicting source examples
- citation mapping examples

