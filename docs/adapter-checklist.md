# Adapter Checklist

Use this checklist for each ingest adapter.

## Identity

- Produce deterministic `source_id` values.
- Use `hash_id(source, source_id)` for item IDs.
- Preserve native IDs in `source_id`.
- Store source-specific details in `raw_metadata`.

## Time

- Convert timestamps to timezone-aware UTC datetimes.
- Respect the configured `ingest.since` cutoff.
- Respect existing source watermarks.
- Update watermarks only after successful non-dry-run processing.

## Content

- Normalize to plain text.
- Skip empty content.
- Truncate pathological single items when required by the phase plan.
- Leave quoted email text in place for Phase A.
- Do not ingest attachments in Phase A.

## Safety

- Read live databases through a copied file or read-only connection.
- Keep transactions short.
- Do not hold locks around embedding or NER work.
- Make dry-run avoid writes and heavy model loading.

## Tests

- Include synthetic fixture coverage.
- Test idempotency.
- Test cutoff behavior.
- Test timezone conversion.
- Test source-specific threading hints.

