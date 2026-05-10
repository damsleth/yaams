# Security

YAAMS reads and stores sensitive personal data: chat history, email, calendar, GitHub activity, curated notes. This document describes the threat model, the guarantees YAAMS makes (and does not make), and how to report vulnerabilities.

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Use [GitHub Security Advisories](https://github.com/damsleth/YAAMS/security/advisories/new) to file a private report. Include:

- A description of the vulnerability and its impact.
- Steps to reproduce or proof-of-concept code.
- The affected version (`yaams --version`).
- Your suggested fix or mitigation, if any.

You can expect an acknowledgement within seven days. There is no bug bounty - this is a personal project - but credit in the changelog is offered on request.

## Supported versions

Only the latest `0.x` release receives security fixes. Pre-`1.0` releases may break API and on-disk schema between minor versions; security backports to older minors are not provided. Pin to a specific version if you need stability.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | yes       |
| < 0.1   | no        |

## Threat model

### What YAAMS protects

- **Local-only compute.** All extraction, NER, embedding, retrieval, and LLM synthesis run on the same machine. The default LLM backends (`ollama`, `claude` CLI, `codex` CLI) are explicit user choices; no data leaves the box unless you point them at a hosted backend.
- **Append-only items.** Raw ingested records are immutable. Edits become new items with `supersedes:` links, so destructive bugs cannot silently rewrite history.
- **Source read-only.** Adapters open source databases (iMessage `chat.db`, Signal SQLCipher store) read-only and do not modify them.
- **Idempotent ingest.** Re-running an adapter against the same source produces the same item IDs, so retries cannot duplicate or corrupt records.

### What YAAMS does not protect

- **At-rest encryption.** The YAAMS SQLite database is stored in plaintext at `db_path`. If your disk is unencrypted, your YAAMS data is unencrypted. Use FileVault (macOS), LUKS (Linux), or BitLocker (Windows).
- **OS-level access controls.** Any process running as your user can read the database. Lock your screen.
- **The LLM backend.** If you configure a hosted LLM (`claude` CLI, `ollama` pointed at a remote host, a `subprocess` adapter calling a network tool), the synthesis prompt - including grounded snippets from your data - is sent to that backend. Local backends (local `ollama`, `dummy`) keep everything in-process.
- **Hugging Face downloads.** First-run model fetches go to `huggingface.co`. After the cache is populated, embedding runs offline.
- **GitHub token scope.** The `github` adapter uses your `gh auth token`. Whatever scopes that token has, YAAMS can use - including reading private repos. Audit `gh auth status` if you are unsure.
- **`owa-piggy` tokens.** The `teams` and `calendar` adapters borrow Microsoft Graph tokens from `owa-piggy`. Token security is delegated to `owa-piggy`'s threat model.

## Data classification

| Data | Where it lives | Encryption | Notes |
|---|---|---|---|
| Item bodies | `db_path` (SQLite) | filesystem only | text of messages, emails, notes |
| Embeddings | `db_path` (sqlite-vec) | filesystem only | derived from item bodies |
| Entity dictionary | `config.yaml` | filesystem only | names, aliases, projects |
| Signals (query log) | `db_path` | filesystem only | what you searched and which results were marked relevant |
| Hugging Face cache | `~/.cache/huggingface/` | filesystem only | public model weights, not sensitive |
| GitHub token | not stored by YAAMS | n/a | retrieved per-call from `gh auth token` |
| Microsoft Graph token | not stored by YAAMS | n/a | borrowed per-call from `owa-piggy` |

## Hardening checklist

- Enable full-disk encryption on the host.
- Run the YAAMS user account as a standard (non-admin) user.
- Set `db_path` outside synced folders (iCloud, Dropbox, OneDrive) unless that is intentional.
- Keep `config.yaml` (entity dictionary, source paths) out of git - the supplied `.gitignore` already excludes it.
- Prefer local LLM backends (`dummy`, local `ollama`) over hosted ones when synthesizing answers from sensitive data.
- Review what scopes your `gh auth token` carries before enabling the `github` adapter.
- Use `python -m yaams.cli ingest --dry-run` first against any new source.

## Known limitations

- No process-level isolation between adapters. A bug in any one adapter (e.g. a malformed `.emlx` file crashing `email_mbox.py`) can abort the whole ingest run, though the changes from completed sources commit independently.
- spaCy NER and sentence-transformers run untrusted model files. The defaults (`xx_ent_wiki_sm`, `BAAI/bge-m3`) are widely-used public models; if you swap them, you accept that risk.
- The `claude`, `codex`, and `subprocess` LLM backends spawn external processes. The list-argv form prevents shell injection from prompt content, but they inherit the calling shell's environment.
