# Preserved skill proposals

One file per proposal against `yaams/retrieve/*`, written by
`docs/experiments/wiki.py`: metadata, verdict, and the full diff. Files are
named `NNNN-<key>.md` (NNNN is a monotonically increasing sequence number)
and are immutable once written - a proposal that is later reverted gets a new
entry, the old one is not edited.

Rejected proposals are preserved on purpose (the WikiSkill audit trail,
arXiv 2608.27454): the diff of a failed attempt is what lets a later
proposal account for it instead of repeating it.
